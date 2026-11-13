"""Public, no-auth first-paint branding for Web Galerías.

HTML includes only public operator-owned design values, never Telegram IDs,
subscriptions, accounts, access grants, tokens or gallery metadata. Entry
remains disabled until the existing signed bootstrap finishes.
"""
import hashlib
import html
import json
import math
import re

from .core import DEFAULTS

_HEX = re.compile(r"#[0-9a-fA-F]{6}\Z")


def safe_color(value, fallback):
    value = str(value or "")
    return value if _HEX.fullmatch(value) else fallback


def safe_height(value, lo, hi, fallback):
    return max(lo, min(hi, int(value))) if type(value) is int else fallback


def _stamp(value):
    """Public asset revision only; no content, credentials or account IDs."""
    try:
        number=float(value)
        if not math.isfinite(number) or number<=0 or number>=10**12:
            return ""
        return str(int(number*1000))
    except (TypeError,ValueError,OverflowError):
        return ""


def _asset_url(name, origin, versions):
    stamp=_stamp((versions or {}).get(origin))
    return "/web-galerias/assets/"+name+("?v="+stamp if stamp else "")


def fast_intro_html(source, design, asset_keys, asset_versions=None, hero_revision=""):
    """Pure builder; preserve all other Web Galerías DOM and checkout controls."""
    if not isinstance(design, dict):
        return source
    d = dict(DEFAULTS, **design)
    title = str(d.get("title") or DEFAULTS["title"])[:160]
    button_label = str(d.get("button_label") or DEFAULTS["button_label"])[:160]
    title_safe = html.escape(title, quote=True)
    button_safe = html.escape(button_label, quote=True)
    asset_keys = set(asset_keys or ())
    asset_versions = asset_versions or {}
    header_url = _asset_url("header-fast","header",asset_versions)
    button_url = _asset_url("button-label-fast","button-label-image",asset_versions)
    title_image = d.get("header_mode") == "image" and "header" in asset_keys
    button_image = (d.get("button_mode") == "image"
                    and "button-label-image" in asset_keys)
    size = safe_height(d.get("title_image_height"), 40, 220, 120)
    button_size = safe_height(d.get("button_image_height"), 12, 48, 30)


    # The collection lives behind the intro. Pre-render its three operator-owned
    # elements too: no placeholder flash before the signed user bootstrap.
    library_specs = (
        ("library_brand","brand-name","strong","",24,140,60),
        ("library_eyebrow","collection-eyebrow","div",' class="eyebrow"',16,100,44),
        ("library_heading","collection-heading","h1","",28,200,100),
    )
    original_library = (
        '<strong id="brand-name" aria-label="Web Galerías">Web Galerías</strong>',
        '<div class="eyebrow" id="collection-eyebrow" aria-label="MI COLECCIÓN">MI COLECCIÓN</div>',
        '<h1 id="collection-heading" aria-label="Mis galerías">Mis galerías</h1>',
    )
    library_replacements = []
    for original, (field, element_id, tag, additional, lo, hi, fallback) in zip(
            original_library, library_specs):
        text = str(d.get(field) or DEFAULTS[field])[:160]
        escaped = html.escape(text, quote=True)
        asset_key = field.replace("_","-")
        if d.get(field+"_mode") == "image" and asset_key in asset_keys:
            height = safe_height(d.get(field+"_image_height"), lo, hi, fallback)
            presentation = (
                f'<{tag} id="{element_id}" class="{("eyebrow " if additional else "")}library-slot-image" '
                f'aria-label="{escaped}">'
                f'<img data-library-image="{field}" '
                f'src="{_asset_url(asset_key,asset_key,asset_versions)}" '
                f'loading="lazy" decoding="async" alt="{escaped}" '
                f'height="{height}"></{tag}>'
            )
        else:
            presentation = (
                f'<{tag}{additional} id="{element_id}" '
                f'aria-label="{escaped}">{escaped}</{tag}>'
            )
        library_replacements.append((original, presentation))

    title_html = (
        f'<div id="intro-headline" class="intro-headline" '
        f'data-fast-title-label="{title_safe}" data-fast-title-height="{size}">'
        f'<img src="{header_url}" data-fast-title '
        f'loading="eager" decoding="async" fetchpriority="high" '
        f'alt="{title_safe}" height="{size}"></div>'
        if title_image else
        f'<div id="intro-headline" class="intro-headline">{title_safe}</div>'
    )
    if button_image:
        button_html = (
            f'<button id="enter" class="primary enter-image-label" disabled '
            f'aria-label="{button_safe}" data-fast-button-label="{button_safe}" '
            f'data-fast-button-height="{button_size}">'
            f'<img class="enter-label-image" data-fast-button '
            f'loading="eager" decoding="async" fetchpriority="high" '
            f'alt="" height="{button_size}" '
            f'src="{button_url}"></button>'
        )
    else:
        button_html = (
            f'<button id="enter" class="primary" disabled '
            f'aria-label="{button_safe}">{button_safe}</button>'
        )
    fit_class = " intro-fit-contain" if d.get("intro_fit")=="contain" else ""
    # Same revision on the preloaded poster, image and video poster prevents
    # iOS from showing the previous saved promo after a new editor upload.
    clean_revision = str(hero_revision or "")
    clean_revision = clean_revision if re.fullmatch(r"[a-f0-9]{12}",clean_revision) else ""
    hero_attr = (' data-hero-revision="'+clean_revision+'"') if clean_revision else ""
    exact = [
        ('<section id="intro" class="intro screen has-intro-video intro-loading" aria-busy="true">',
         '<section id="intro" class="intro screen has-intro-video intro-loading fast-identity'
         +fit_class+'" aria-busy="true"'+hero_attr+'>'),
        ('<div id="intro-headline" class="intro-headline">Web Galerías</div>',
         title_html),
        ('<p id="intro-subtitle">Explora nuestras galerías privadas</p>',
         f'<p id="intro-subtitle">{html.escape(str(d.get("intro_caption") or "")[:160])}</p>'),
        ('<span class="intro-placeholder-label">GALERÍAS PRIVADAS</span>',
         f'<span class="intro-placeholder-label">{html.escape(str(d.get("intro_label") or "")[:160])}</span>'),
        ('<title>Web Galerías</title>',f'<title>{title_safe}</title>'),
        ('<button id="enter" class="primary">ENTRAR A LA GALERÍA</button>',
         button_html),
    ]
    exact.extend(library_replacements)
    if (source.count('<html lang="es"><head>') != 1 or
            any(source.count(old) != 1 for old, _ in exact)):
        return source  # No partial mutation or incorrect presentation.
    for old, new in exact:
        source = source.replace(old, new, 1)
    # Every public editor color is already current on first paint, with a
    # single exact CSP-hashed inline style (never 'unsafe-inline').
    colors = (
        ("--button","button_color"),("--accent","accent"),
        ("--bg","background"),("--text","text_color"))
    style = ";".join(name+":"+safe_color(d.get(field),DEFAULTS[field])
                     for name,field in colors)
    source = source.replace(
        '<html lang="es"><head>',
        f'<html lang="es" style="{style}"><head>', 1)
    if clean_revision:
        source = source.replace(
            '/web-galerias/assets/intro-poster"',
            '/web-galerias/assets/intro-poster?v='+clean_revision+'"')
    # One optional preload per chosen asset: avoid a guaranteed 404 for a
    # text-only button. Both use the *identical* URL as the on-screen image.
    selected = []
    if title_image:
        selected.append('<link rel="preload" as="image" fetchpriority="high" '
                        f'href="{header_url}">')
    if button_image:
        selected.append('<link rel="preload" as="image" fetchpriority="high" '
                        f'href="{button_url}">')
    source = source.replace("</head>", "\n".join(selected) + "\n</head>", 1)
    return source


async def render_fast_intro(g, source):
    """One consistent DB snapshot for current public presentation metadata.

    The user-specific bootstrap still determines access, subscriptions and
    payments. This public snapshot contains no user or private gallery data.
    """
    if not getattr(g, "ready", False):
        return source
    try:
        db=g.db()
        keys=["header","button-label-image","library-brand",
              "library-eyebrow","library-heading",
              "intro-photo","intro","intro-legacy-poster"]
        row=await db.fetchrow("""
            SELECT
              (SELECT value FROM wg_config WHERE key='design') AS design,
              (SELECT value->>'updated' FROM wg_config
               WHERE key='intro_promo') AS promo_updated,
              (SELECT COALESCE(jsonb_object_agg(key,updated),'{}'::jsonb)
               FROM wg_assets WHERE key=ANY($1::text[])) AS assets
        """,keys)
        if row is None:
            return source
        raw=row["design"]
        if isinstance(raw,str):raw=json.loads(raw)
        design=raw if isinstance(raw,dict) else {}
        versions=row["assets"]
        if isinstance(versions,str):versions=json.loads(versions)
        if not isinstance(versions,dict):versions={}
        mode="photo" if design.get("intro_media") in ("photo","gif") else "video"
        chosen=(versions.get("intro-photo") if mode=="photo" else
                (row["promo_updated"] or versions.get("intro-legacy-poster") or
                 versions.get("intro")))
        # Only the public mode and revision enter the HTML; never R2 object
        # keys, credentials or private Telegram/account state.
        hero_revision=hashlib.sha256(
            (mode+":"+_stamp(chosen)).encode("ascii")).hexdigest()[:12]
        return fast_intro_html(
            source,design,versions.keys(),versions,hero_revision)
    except Exception:
        # Keep the previous safe hidden-until-bootstrap fallback; a DB hiccup
        # must never make the entry page or its paid checkout unavailable.
        return source


# In-memory derived assets are public presentation graphics only.
# Original operator uploads remain unchanged in the Gallery DB.
import asyncio
import io
from PIL import Image, ImageOps

_FAST_IMAGES = {}
_FAST_LOCK = asyncio.Lock()


def _compress_identity_image(data, mime, key):
    if mime not in ("image/png", "image/jpeg", "image/webp") or len(data)<120_000:
        return data, mime
    try:
        with Image.open(io.BytesIO(data)) as source:
            width, height = source.size
            if not (0 < width * height <= 14_000_000):
                return data, mime
            image = ImageOps.exif_transpose(source)
            target = (2048, 780) if key=="header" else (1536, 216)
            image.thumbnail(target, Image.Resampling.LANCZOS)
            if image.mode not in ("RGB", "RGBA"):
                image = image.convert("RGBA" if "A" in image.getbands() else "RGB")
            out = io.BytesIO()
            image.save(out, format="WEBP", quality=92, method=4)
            result = out.getvalue()
            if result and len(result)<len(data)*.85:
                return result, "image/webp"
    except (OSError, ValueError, TypeError, MemoryError):
        pass
    return data, mime


async def fast_identity_asset(key, revision, raw, mime):
    cache_key = (key, revision)
    record = _FAST_IMAGES.get(cache_key)
    if record is not None:
        return record
    async with _FAST_LOCK:
        record = _FAST_IMAGES.get(cache_key)
        if record is None:
            record = await asyncio.to_thread(
                _compress_identity_image, raw, mime, key)
            # Strictly bounded: the newest graphic for each identity.
            for existing in tuple(_FAST_IMAGES):
                if existing[0] == key and existing != cache_key:
                    del _FAST_IMAGES[existing]
            _FAST_IMAGES[cache_key] = record
    return record
