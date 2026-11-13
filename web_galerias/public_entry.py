"""Disposable public Telegram Mini App entry for Web Galerías.

Only GALERIAS_PUBLIC_BOT_TOKEN changes on future replacements. The bot's
username is obtained from Telegram getMe, never entered manually. This class
does not poll Telegram, write to databases or touch paid-media operations.
"""
import asyncio
import logging
import os
import re
import time

from aiohttp import ClientSession, ClientTimeout

log = logging.getLogger("web-galerias-public-bot")
TOKEN_RE = re.compile(r"^[0-9]{5,15}:[A-Za-z0-9_-]{30,}$")
BOT_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]{4,31}$")
LINK_RE = re.compile(r"^https://t[.]me/[A-Za-z][A-Za-z0-9_]{4,31}[?]startapp$")


class PublicGalleryEntry:
    """Read-only discovery of a disposable public Main Mini App bot."""

    def __init__(self, token=None):
        raw = os.getenv("GALERIAS_PUBLIC_BOT_TOKEN", "") if token is None else token
        self.token = raw.strip() if TOKEN_RE.fullmatch(raw.strip()) else ""
        self._configured = bool(str(raw).strip())
        self._url = None
        self._checked_at = 0.0
        self._lock = asyncio.Lock()

    def authorized_token(self, protected_token="", secondary_tokens=""):
        """Prevent accidentally reusing the protected DESCARGAS bot as public."""
        if not self.token or self.token == protected_token:
            return ""
        if self.token in {value.strip() for value in secondary_tokens.split(",")}:
            return ""
        return self.token

    async def link(self, protected_token="", secondary_tokens=""):
        token = self.authorized_token(protected_token, secondary_tokens)
        if not token:
            return None
        now = time.monotonic()
        if now < self._checked_at + (180 if self._url else 15):
            return self._url
        async with self._lock:
            now = time.monotonic()
            if now < self._checked_at + (180 if self._url else 15):
                return self._url
            try:
                # aiohttp does not log an INFO-level URL containing the bot token.
                # No redirect, no environment proxy and no token in exceptions.
                async with ClientSession(
                    timeout=ClientTimeout(total=5), trust_env=False
                ) as session:
                    async with session.get(
                        "https://api.telegram.org/bot" + token + "/getMe",
                        allow_redirects=False,
                    ) as response:
                        if response.status != 200:
                            raise ValueError("getMe rejected")
                        data = await response.json()
                user = data.get("result") if data.get("ok") is True else None
                name = user.get("username") if isinstance(user, dict) else None
                if not (isinstance(name, str) and BOT_RE.fullmatch(name)
                        and user.get("is_bot") is True):
                    raise ValueError("Invalid public bot identity")
                # BotFather must have configured the *Main* Mini App. Without
                # this flag a ?startapp link may open the bot's DM instead.
                if user.get("has_main_web_app") is not True:
                    raise ValueError("Main Mini App not configured in BotFather")
                self._url = "https://t.me/" + name + "?startapp"
            except (Exception,) as exc:
                log.warning("Public gallery bot not ready (%s)", type(exc).__name__)
                self._url = None
            self._checked_at = time.monotonic()
            return self._url

    async def details(self, protected_token="", secondary_tokens=""):
        url = await self.link(protected_token, secondary_tokens)
        if url:
            return {"ready": True, "url": url}
        return {"ready": False, "url": None}
