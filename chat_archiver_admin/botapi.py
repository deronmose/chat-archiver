"""Minimal async Telegram Bot API client (httpx).

The bot token is a secret. It is part of every request URL, and httpx puts the URL
into its exception messages and INFO logs. This module therefore:
- never formats an httpx exception into a message (only its class name),
- raises every error ``from None`` so no chained exception carries the URL,
- redacts anything that looks like a token from every message it produces,
- silences the httpx/httpcore loggers and adds a redacting filter to them.
"""
from __future__ import annotations

import logging
import re
from typing import Any

import httpx

API_BASE = "https://api.telegram.org"
LONG_POLL_TIMEOUT = 25

_TOKEN_RE = re.compile(r"\d{3,}:[A-Za-z0-9_-]{10,}")


def redact(text: str, token: str | None = None) -> str:
    """Remove a bot token (the given one, and anything token-shaped) from text."""
    text = str(text)
    if token:
        text = text.replace(token, "***")
    return _TOKEN_RE.sub("***", text)


class _RedactFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        try:
            msg = record.getMessage()
        except Exception:
            return True
        record.msg, record.args = redact(msg), ()
        return True


for _name in ("httpx", "httpcore"):
    _log = logging.getLogger(_name)
    _log.setLevel(logging.WARNING)
    if not any(isinstance(f, _RedactFilter) for f in _log.filters):
        _log.addFilter(_RedactFilter())


class BotApiError(Exception):
    """A Bot API call failed. The message never contains the bot token."""

    def __init__(self, message: str, *, error_code: int | None = None,
                 retry_after: int | None = None, network: bool = False):
        super().__init__(redact(message))
        self.error_code = error_code
        self.retry_after = retry_after
        self.network = network

    def __str__(self) -> str:
        return redact(super().__str__())


def _describe(error_code: int | None, description: str) -> str:
    if error_code == 401:
        return "Telegram rejected the bot token (Unauthorized). Check the token from @BotFather."
    if error_code == 404:
        return "Telegram does not know this bot token (Not Found). Check the token from @BotFather."
    if error_code == 409:
        return ("Another program is already receiving updates for this bot (Conflict). "
                "Close it, or remove its webhook, and try again.")
    if error_code == 403:
        return "The user has blocked the bot or never started it (Forbidden)."
    if error_code == 429:
        return "Too many requests to Telegram. Waiting a bit before retrying."
    return f"Telegram error {error_code}: {description}" if error_code else description


class BotApi:
    """Bot API over HTTPS. Only talks to https://api.telegram.org."""

    def __init__(self, token: str, *, client: httpx.AsyncClient | None = None):
        token = (token or "").strip()
        if not re.fullmatch(r"\d+:[A-Za-z0-9_-]+", token):
            raise BotApiError("This does not look like a bot token. It should look like 123456789:AA...")
        self._token = token
        self._client = client
        self._own_client = client is None

    def __repr__(self) -> str:                      # never show the token
        return "BotApi(token=***)"

    def _http(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                base_url=API_BASE,
                timeout=httpx.Timeout(15.0, read=LONG_POLL_TIMEOUT + 15.0),
                follow_redirects=False,
            )
        return self._client

    async def aclose(self) -> None:
        if self._client is not None and self._own_client:
            client, self._client = self._client, None
            try:
                await client.aclose()
            except Exception:
                pass

    async def _call(self, method: str, payload: dict[str, Any] | None = None) -> Any:
        url = f"/bot{self._token}/{method}"
        try:
            resp = await self._http().post(url, json=payload or {})
        except httpx.TimeoutException:
            raise BotApiError("Telegram did not answer in time (timeout).", network=True) from None
        except httpx.HTTPError as e:
            raise BotApiError(f"Cannot reach Telegram ({type(e).__name__}). Check the internet connection.",
                              network=True) from None
        try:
            data = resp.json()
        except ValueError:
            raise BotApiError(f"Unexpected answer from Telegram (HTTP {resp.status_code}).",
                              error_code=resp.status_code, network=resp.status_code >= 500) from None
        if not isinstance(data, dict) or not data.get("ok"):
            data = data if isinstance(data, dict) else {}
            code = data.get("error_code") or resp.status_code
            params = data.get("parameters") or {}
            raise BotApiError(_describe(code, redact(str(data.get("description") or "request failed"), self._token)),
                              error_code=code, retry_after=params.get("retry_after"),
                              network=code >= 500 or code == 429) from None
        return data.get("result")

    # ---- methods used by the Admin ----
    async def get_me(self) -> dict:
        return await self._call("getMe")

    async def delete_webhook(self) -> bool:
        return bool(await self._call("deleteWebhook", {"drop_pending_updates": False}))

    async def get_updates(self, offset: int | None = None, timeout: int = LONG_POLL_TIMEOUT) -> list[dict]:
        payload: dict[str, Any] = {"timeout": int(timeout), "allowed_updates": ["message"]}
        if offset is not None:
            payload["offset"] = int(offset)
        return list(await self._call("getUpdates", payload) or [])

    async def send_message(self, chat_id: int, text: str, reply_markup: dict | None = None) -> dict:
        """Send an HTML-formatted message (callers must HTML-escape user-provided text)."""
        payload: dict[str, Any] = {"chat_id": chat_id, "text": text, "parse_mode": "HTML",
                                   "link_preview_options": {"is_disabled": True}}
        if reply_markup is not None:
            payload["reply_markup"] = reply_markup
        return await self._call("sendMessage", payload)
