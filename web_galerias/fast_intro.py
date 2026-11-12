"""Public, no-auth first-paint branding for Web Galerías.

HTML includes only public operator-owned design values, never Telegram IDs,
subscriptions, accounts, access grants, tokens or gallery metadata. Entry
remains disabled until the existing signed bootstrap finishes.
"""
import html
import re

from .core import DEFAULTS

_HEX = re.compile(r"#[0-9a-fA-F]{6}\Z")


def safe_color(value, fallback):
    value = str(value or "")
    return value if _HEX.fullmatch(value) else fallback


def safe_height(value, lo, hi, fallback):
    return max(lo, min(hi, int(value))) if type(value) is int else fallback


def fast_intro_html(source, design, asset_keys):
    """Pure builder; preserve all other Web Galerías DOM and checkout controls."""
    if not isinstance(design, dict):
        return source
    d = dict(DEFAULTS, **design)
    title = str(d.get("title") or DEFAULTS["title"])[:160]
    button_label = str(d.get("button_label") or DEFAULTS["button_label"])[:160]
    title_safe = html.escape(title, quote=True)
    button_safe = html.escape(button_label, quote=True)
    asset_keys = set(asset_keys or ())
    title_image = d.get("header_mode") == "image" and "header" in asset_keys
    button_image = (d.get("button_mode") == "image"
                    and "button-label-image" in asset_keys)
    size = safe_height(d.get("title_image_height"), 40, 220, 120)
    button_size = safe_height(d.get("button_image_height"), 12, 48, 30)

    title_html = (
        f'<div id="intro-headline" class="intro-headline" '
        f'data-fast-title-label="{title_safe}" data-fast-title-height="{size}">'
        f'<img src="/web-galerias/assets/header-fast" data-fast-title '
        f'loading="eager" decoding="async" fetchpriority="high" '
        f'alt="{title_safe}" style="height:{size}px;max-height:{size}px;'
        f'max-width:min(92vw,600px);width:auto;object-fit:contain"></div>'
        if title_image else
        f'<div id="intro-headline" class="intro-headline">{title_safe}</div>'
    )
    if button_image:
        button_html = (
            f'<button id="enter" class="primary enter-image-label" disabled '
            f'aria-label="{button_safe}" data-fast-button-label="{button_safe}" '
            f'style="--entry-label-image-height:{button_size}px">'
            f'<img class="enter-label-image" data-fast-button '
            f'loading="eager" decoding="async" fetchpriority="high" '
            f'alt="" src="/web-galerias/assets/button-label-fast"></button>'
        )
    else:
        button_html = (
            f'<button id="enter" class="primary" disabled '
            f'aria-label="{button_safe}">{button_safe}</button>'
        )
    styles = ";".join(f"{key}:{safe_color(d.get(field), fallback)}"
                      for key, field, fallback in (
                          ("--accent", "accent", DEFAULTS["accent"]),
                          ("--bg", "background", DEFAULTS["background"]),
                          ("--text", "text_color", DEFAULTS["text_color"]),
                          ("--button", "button_color", DEFAULTS["button_color"]),
                      ))
    exact = [
        ('<section id="intro" class="intro screen has-intro-video intro-loading" aria-busy="true">',
         '<section id="intro" class="intro screen has-intro-video intro-loading fast-identity" '
         f'aria-busy="true" style="{styles}">'),
        ('<div id="intro-headline" class="intro-headline">Web Galerías</div>',
         title_html),
        ('<p id="intro-subtitle">Explora nuestras galerías privadas</p>',
         f'<p id="intro-subtitle">{html.escape(str(d.get("intro_caption") or "")[:160])}</p>'),
        ('<button id="enter" class="primary">ENTRAR A LA GALERÍA</button>',
         button_html),
    ]
    if any(source.count(old) != 1 for old, _ in exact):
        return source  # No partial mutation or incorrect presentation.
    for old, new in exact:
        source = source.replace(old, new, 1)
    # One optional preload per chosen asset: avoid a guaranteed 404 for a
    # text-only button. Both use the *identical* URL as the on-screen image.
    selected = []
    if title_image:
        selected.append('<link rel="preload" as="image" fetchpriority="high" '
                        'href="/web-galerias/assets/header-fast">')
    if button_image:
        selected.append('<link rel="preload" as="image" fetchpriority="high" '
                        'href="/web-galerias/assets/button-label-fast">')
    source = source.replace("</head>", "\n".join(selected) + "\n</head>", 1)
    return source


async def render_fast_intro(g, source):
    """Two tiny metadata reads, independent of the slow user bootstrap."""
    if not getattr(g, "ready", False):
        return source
    try:
        design = await g.design()
        rows = await g.db().fetch(
            "SELECT key FROM wg_assets WHERE key=ANY($1::text[])",
            ["header", "button-label-image"])
        return fast_intro_html(source, design, [r["key"] for r in rows])
    except Exception:
        # A temporary DB failure must not break the entry document; the
        # existing authenticated bootstrap remains the source of truth.
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
