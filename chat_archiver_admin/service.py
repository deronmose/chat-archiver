"""Admin service: receives access requests from the owner's bot and answers them.

All state (bot token, API keys, the owner's Ed25519 private key, bot identity,
update offset, requests, approvals, history) lives in one DPAPI-encrypted Vault.
The UI is a thin layer over this class; everything here is testable with a fake bot.
"""
from __future__ import annotations

import asyncio
import html
import json
import os
import re
import secrets
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable, Protocol

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import ChaCha20Poly1305
from cryptography.hazmat.primitives.kdf.scrypt import Scrypt

from approval import protocol as p
from approval.protocol import Grant, ProtocolError, VALIDITY_DAYS, _b64d, _b64e

from .botapi import BotApiError, redact

DAY = 86400
HISTORY_LIMIT = 500
MAX_PENDING = 300            # spam guard: total pending requests kept
MAX_PENDING_PER_USER = 3     # spam guard: pending devices per Telegram user
DECIDED_KEEP = 1000          # decided requests kept in storage (history keeps the log)
STATE_VERSION = 1

_START_RE = re.compile(r"^/start(?:@\w+)?(?:\s+(\S+))?\s*$")
_API_HASH_RE = re.compile(r"[0-9a-fA-F]{32}")
_TOKEN_RE = re.compile(r"\d+:[A-Za-z0-9_-]+")

_BACKUP_PREFIX = "CAOK1:"
_BACKUP_AAD = b"chat-archiver/owner-key-backup/v1"
_SCRYPT_N, _SCRYPT_R, _SCRYPT_P = 2 ** 15, 8, 1
MIN_BACKUP_PASSWORD = 8


class AdminError(Exception):
    """A user-facing error with a plain-English message (never contains secrets)."""


class Bot(Protocol):
    async def get_me(self) -> dict: ...
    async def delete_webhook(self) -> bool: ...
    async def get_updates(self, offset: int | None = None, timeout: int = 0) -> list[dict]: ...
    async def send_message(self, chat_id: int, text: str, reply_markup: dict | None = None) -> dict: ...


# ---------- records ----------

@dataclass
class Request:
    id: str
    user_id: int
    chat_id: int
    username: str
    name: str
    device_label: str
    device_pub: bytes
    created_at: int
    status: str = "pending"            # pending | approved | denied | dismissed
    decided_at: int | None = None

    @property
    def display_name(self) -> str:
        return self.name or (f"@{self.username}" if self.username else f"User {self.user_id}")

    def to_json(self) -> dict:
        d = asdict(self)
        d["device_pub"] = _b64e(self.device_pub)
        return d

    @classmethod
    def from_json(cls, d: dict) -> "Request":
        d = dict(d)
        d["device_pub"] = _b64d(d["device_pub"])
        return cls(**d)


@dataclass
class Approval:
    id: str
    request_id: str
    user_id: int
    chat_id: int
    username: str
    name: str
    device_label: str
    device_pub: bytes
    approved_at: int
    expires_at: int
    status: str = "active"             # active | revoked
    revoked_at: int | None = None

    @property
    def display_name(self) -> str:
        return self.name or (f"@{self.username}" if self.username else f"User {self.user_id}")

    def is_expired(self, now: float) -> bool:
        return now > self.expires_at

    def to_json(self) -> dict:
        d = asdict(self)
        d["device_pub"] = _b64e(self.device_pub)
        return d

    @classmethod
    def from_json(cls, d: dict) -> "Approval":
        d = dict(d)
        d["device_pub"] = _b64d(d["device_pub"])
        return cls(**d)


@dataclass
class HistoryEvent:
    ts: int
    kind: str                          # request | approved | denied | revoked | dismissed | setup | key | error | info
    text: str
    user_id: int | None = None
    extra: dict = field(default_factory=dict)


# ---------- message texts (HTML) ----------

def _esc(s: str) -> str:
    return html.escape(s or "", quote=False)


def msg_received(device: str) -> str:
    return (f"✅ <b>Request received</b>, waiting for approval.\n\n"
            f"Device: <b>{_esc(device)}</b>\n"
            "You will get a message here as soon as the owner decides. "
            "Keep Chat Archiver open on that computer.")


def msg_already_waiting(device: str) -> str:
    return (f"⏳ Your request for <b>{_esc(device)}</b> is already waiting for approval. "
            "No need to send it again.")


def msg_not_a_request() -> str:
    return ("👋 This bot hands out access to <b>Chat Archiver</b>.\n\n"
            "To request access, open Chat Archiver on your computer and click "
            "<b>Request access</b>. It opens this chat with the right link.")


def msg_approved(device: str, code: str, days: int) -> str:
    return (f"🎉 <b>Access approved</b> for <b>{_esc(device)}</b>.\n\n"
            "Tap <b>Copy activation code</b> below. Chat Archiver notices the copied code "
            "by itself; if it does not, paste the code into its activation field.\n\n"
            f"The code works only on that computer and only with this Telegram account, "
            f"for {days} days.\n\n"
            f"<code>{_esc(code)}</code>")


def msg_denied(device: str) -> str:
    return (f"Sorry, your request for <b>{_esc(device)}</b> was not approved this time. "
            "If you think this is a mistake, please contact the owner directly.")


def msg_revoked(device: str, token: str) -> str:
    return (f"🔒 Access to Chat Archiver on <b>{_esc(device)}</b> has been revoked by the owner. "
            "The app will remove its keys the next time it starts. "
            "You can request access again from the app.\n\n"
            f"<code>{_esc(token)}</code>")


def _full_name(user: dict) -> str:
    return " ".join(x for x in (user.get("first_name"), user.get("last_name")) if x).strip()


def _new_id(prefix: str) -> str:
    return f"{prefix}_{secrets.token_hex(6)}"


# ---------- service ----------

class AdminService:
    def __init__(self, vault, bot_factory: Callable[[str], Bot], clock: Callable[[], float]):
        self._vault = vault
        self._bot_factory = bot_factory
        self._clock = clock
        self._bot: Bot | None = None
        self._bot_token_for: str | None = None
        self._busy: set[str] = set()
        self._state = self._load()

    # ---- persistence ----
    def _load(self) -> dict:
        try:
            raw = self._vault.load() or {}
        except Exception as e:  # damaged file or another Windows user
            raise AdminError("Cannot read the Admin data file. It may belong to another Windows user "
                             f"or be damaged ({type(e).__name__}).") from None
        state = {"v": STATE_VERSION, "offset": None, "requests": [], "approvals": [], "history": []}
        state.update(raw)
        return state

    def _save(self) -> None:
        self._vault.save(self._state)

    def _now(self) -> int:
        return int(self._clock())

    def _log(self, kind: str, text: str, user_id: int | None = None, **extra: Any) -> None:
        ev = {"ts": self._now(), "kind": kind, "text": text, "user_id": user_id, "extra": extra}
        hist = self._state.setdefault("history", [])
        hist.append(ev)
        del hist[:-HISTORY_LIMIT]

    # ---- configuration ----
    @property
    def is_configured(self) -> bool:
        s = self._state
        return bool(s.get("bot_token") and s.get("api_id") and s.get("api_hash")
                    and s.get("owner_private") and s.get("bot_username"))

    @property
    def bot_username(self) -> str | None:
        return self._state.get("bot_username")

    @property
    def bot_id(self) -> int | None:
        return self._state.get("bot_id")

    @property
    def api_id(self) -> int | None:
        return self._state.get("api_id")

    @property
    def has_owner_key(self) -> bool:
        return bool(self._state.get("owner_private"))

    @property
    def owner_fingerprint(self) -> str | None:
        """Short, non-secret fingerprint of the owner verify key (for display)."""
        if not self.has_owner_key:
            return None
        vk = _b64e(self._owner().verify_key)
        return f"{vk[:6]}…{vk[-6:]}"

    @staticmethod
    def validate_api_keys(api_id: Any, api_hash: str) -> tuple[int, str]:
        try:
            api_id_int = int(str(api_id).strip())
        except (TypeError, ValueError):
            raise AdminError("api_id must be a number (from my.telegram.org).") from None
        if api_id_int <= 0 or api_id_int >= 2 ** 63:
            raise AdminError("api_id must be a positive number (from my.telegram.org).")
        api_hash = (api_hash or "").strip()
        if not _API_HASH_RE.fullmatch(api_hash):
            raise AdminError("api_hash must be exactly 32 characters 0-9 and a-f (from my.telegram.org).")
        return api_id_int, api_hash.lower()

    def _make_bot(self, token: str) -> Bot:
        try:
            return self._bot_factory(token)
        except BotApiError as e:
            raise AdminError(str(e)) from None

    async def _check_token(self, token: str) -> tuple[Bot, dict]:
        token = (token or "").strip()
        if not _TOKEN_RE.fullmatch(token):
            raise AdminError("This does not look like a bot token. Copy it from @BotFather; "
                             "it looks like 123456789:AA...")
        bot = self._make_bot(token)
        try:
            me = await bot.get_me()
        except BotApiError as e:
            raise AdminError(f"Telegram did not accept this bot token. {redact(str(e), token)}") from None
        if not isinstance(me, dict) or not me.get("is_bot") or not me.get("username"):
            raise AdminError("This token does not belong to a bot.")
        return bot, me

    async def setup(self, bot_token: str, api_id: Any, api_hash: str) -> None:
        """Validate and store the bot token and API keys. Creates the owner key once."""
        api_id_int, api_hash_norm = self.validate_api_keys(api_id, api_hash)
        token = (bot_token or "").strip()
        bot, me = await self._check_token(token)
        await self._apply(token, bot, me, api_id_int, api_hash_norm)

    async def update_settings(self, bot_token: str | None = None, api_id: Any = None,
                              api_hash: str | None = None) -> None:
        """Change some settings; empty values keep the current ones."""
        s = self._state
        token = (bot_token or "").strip() or s.get("bot_token") or ""
        api_id_int, api_hash_norm = self.validate_api_keys(
            api_id if str(api_id or "").strip() else s.get("api_id"),
            (api_hash or "").strip() or s.get("api_hash") or "")
        if not token:
            raise AdminError("Enter the bot token from @BotFather.")
        if token == s.get("bot_token") and self.is_configured:
            s["api_id"], s["api_hash"] = api_id_int, api_hash_norm
            self._log("setup", "API keys updated.")
            self._save()
            return
        bot, me = await self._check_token(token)
        await self._apply(token, bot, me, api_id_int, api_hash_norm)

    async def _apply(self, token: str, bot: Bot, me: dict, api_id: int, api_hash: str) -> None:
        s = self._state
        old_bot_id = s.get("bot_id")
        if self._bot is not None and self._bot is not bot:
            await self._close_bot()
        s.update(bot_token=token, api_id=api_id, api_hash=api_hash,
                 bot_id=int(me["id"]), bot_username=str(me["username"]))
        if old_bot_id is not None and old_bot_id != s["bot_id"]:
            s["offset"] = None                  # offsets belong to one bot
        if not s.get("owner_private"):
            s["owner_private"] = _b64e(p.new_owner_key().private)
            self._log("key", "Owner signing key created.")
        self._bot, self._bot_token_for = bot, token
        self._log("setup", f"Connected to bot @{s['bot_username']}.")
        self._save()

    def build_config(self) -> dict:
        """Public data baked into the Chat Archiver build. No secrets."""
        if not self.is_configured:
            raise AdminError("Set up the bot first.")
        return {"bot_username": self._state["bot_username"], "bot_id": int(self._state["bot_id"]),
                "owner_verify_key": _b64e(self._owner().verify_key)}

    def export_build_config(self, path: str | os.PathLike) -> Path:
        cfg = self.build_config()
        path = Path(path)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(cfg, indent=2) + "\n", encoding="utf-8")
        except OSError as e:
            raise AdminError(f"Cannot write {path}: {e.strerror or type(e).__name__}.") from None
        self._log("info", f"Build config exported to {path}.")
        self._save()
        return path

    # ---- owner key backup ----
    def _owner(self) -> p.OwnerKey:
        priv = self._state.get("owner_private")
        if not priv:
            raise AdminError("There is no owner key yet. Finish the setup first.")
        return p.owner_key_from_private(_b64d(priv))

    @staticmethod
    def _kdf(password: str, salt: bytes) -> bytes:
        return Scrypt(salt=salt, length=32, n=_SCRYPT_N, r=_SCRYPT_R, p=_SCRYPT_P).derive(password.encode("utf-8"))

    def export_owner_backup(self, password: str) -> str:
        """Owner private key encrypted with a password (scrypt + ChaCha20-Poly1305)."""
        if len(password or "") < MIN_BACKUP_PASSWORD:
            raise AdminError(f"Use a password of at least {MIN_BACKUP_PASSWORD} characters.")
        owner = self._owner()
        salt, nonce = os.urandom(16), os.urandom(12)
        ct = ChaCha20Poly1305(self._kdf(password, salt)).encrypt(nonce, owner.private, _BACKUP_AAD)
        self._log("key", "Owner key backup exported.")
        self._save()
        return _BACKUP_PREFIX + _b64e(salt + nonce + ct)

    def import_owner_backup(self, text: str, password: str) -> None:
        text = re.sub(r"\s+", "", text or "")
        if not text.startswith(_BACKUP_PREFIX):
            raise AdminError("This is not a Chat Archiver owner key backup.")
        try:
            blob = _b64d(text[len(_BACKUP_PREFIX):])
        except ValueError:
            raise AdminError("The backup text is damaged.") from None
        if len(blob) != 16 + 12 + 32 + 16:
            raise AdminError("The backup text is damaged.")
        salt, nonce, ct = blob[:16], blob[16:28], blob[28:]
        try:
            private = ChaCha20Poly1305(self._kdf(password or "", salt)).decrypt(nonce, ct, _BACKUP_AAD)
            p.owner_key_from_private(private)
        except (InvalidTag, ValueError):
            raise AdminError("Wrong password, or the backup is damaged.") from None
        self._state["owner_private"] = _b64e(private)
        self._log("key", "Owner key restored from backup.")
        self._save()

    # ---- bot ----
    def _get_bot(self) -> Bot:
        token = self._state.get("bot_token")
        if not token:
            raise AdminError("Set up the bot first.")
        if self._bot is None or self._bot_token_for != token:
            self._bot, self._bot_token_for = self._make_bot(token), token
        return self._bot

    async def _close_bot(self) -> None:
        bot, self._bot = self._bot, None
        close = getattr(bot, "aclose", None)
        if close is not None:
            try:
                await close()
            except Exception:
                pass

    async def close(self) -> None:
        await self._close_bot()

    async def prepare_listening(self) -> None:
        """Remove a webhook if one is set (getUpdates does not work while it exists)."""
        try:
            await self._get_bot().delete_webhook()
        except BotApiError as e:
            raise AdminError(str(e)) from None

    # ---- queries ----
    def _requests(self) -> list[Request]:
        return [Request.from_json(d) for d in self._state.get("requests", [])]

    def _approvals(self) -> list[Approval]:
        return [Approval.from_json(d) for d in self._state.get("approvals", [])]

    def pending(self) -> list[Request]:
        return sorted((r for r in self._requests() if r.status == "pending"), key=lambda r: r.created_at)

    def approved(self) -> list[Approval]:
        """Approvals that were not revoked (including expired ones, check is_expired)."""
        return sorted((a for a in self._approvals() if a.status == "active"),
                      key=lambda a: a.approved_at, reverse=True)

    def history(self, limit: int | None = None) -> list[HistoryEvent]:
        events = [HistoryEvent(**e) for e in reversed(self._state.get("history", []))]
        return events[:limit] if limit else events

    def _find_request(self, request_id: str) -> tuple[int, Request]:
        for i, d in enumerate(self._state.get("requests", [])):
            if d["id"] == request_id:
                return i, Request.from_json(d)
        raise AdminError("This request no longer exists.")

    def _find_approval(self, approval_id: str) -> tuple[int, Approval]:
        for i, d in enumerate(self._state.get("approvals", [])):
            if d["id"] == approval_id:
                return i, Approval.from_json(d)
        raise AdminError("This approval no longer exists.")

    # ---- polling ----
    def _parse_request(self, update: dict) -> Request | str | None:
        """Return a Request, "hint" for a bare /start in a private chat, or None to ignore."""
        msg = update.get("message")
        if not isinstance(msg, dict):
            return None
        user, chat, text = msg.get("from") or {}, msg.get("chat") or {}, msg.get("text")
        if chat.get("type") != "private" or user.get("is_bot") or not isinstance(text, str):
            return None
        m = _START_RE.match(text.strip())
        if not m or not isinstance(user.get("id"), int):
            return None
        try:
            device_pub, label = p.parse_start_payload(m.group(1) or "")
        except ProtocolError:
            return "hint"
        return Request(id=_new_id("req"), user_id=int(user["id"]), chat_id=int(chat.get("id", user["id"])),
                       username=str(user.get("username") or ""), name=_full_name(user),
                       device_label=label or "PC", device_pub=device_pub,
                       created_at=int(msg.get("date") or self._now()))

    async def poll_once(self, timeout: int = 0) -> list[Request]:
        """One getUpdates round. Returns new pending requests."""
        if not self.is_configured:
            raise AdminError("Set up the bot first.")
        bot = self._get_bot()
        updates = await bot.get_updates(offset=self._state.get("offset"), timeout=timeout)
        new: list[Request] = []
        replies: list[tuple[int, str]] = []
        offset = self._state.get("offset")
        for upd in updates or []:
            uid = upd.get("update_id")
            if not isinstance(uid, int):
                continue
            if offset is not None and uid < offset:
                continue                         # already processed
            offset = uid + 1
            parsed = self._parse_request(upd)
            if parsed is None:
                continue
            if parsed == "hint":
                chat_id = upd["message"]["chat"]["id"]
                replies.append((chat_id, msg_not_a_request()))
                continue
            req: Request = parsed
            pend = [r for r in self._requests() if r.status == "pending"]   # includes ones added above
            if any(r.user_id == req.user_id and r.device_pub == req.device_pub for r in pend):
                replies.append((req.chat_id, msg_already_waiting(req.device_label)))
                continue
            if (sum(1 for r in pend if r.user_id == req.user_id) >= MAX_PENDING_PER_USER
                    or len(pend) >= MAX_PENDING):
                continue                         # spam guard: silently ignore
            self._state.setdefault("requests", []).append(req.to_json())
            self._log("request", f"{req.display_name} asked for access on {req.device_label}.",
                      req.user_id, request_id=req.id, username=req.username, device=req.device_label)
            new.append(req)
            replies.append((req.chat_id, msg_received(req.device_label)))
        self._state["offset"] = offset
        self._prune_requests()
        self._save()                             # offset first: never process an update twice
        for chat_id, text in replies:
            try:
                await bot.send_message(chat_id, text)
            except BotApiError as e:
                self._log("error", f"Could not reply to user {chat_id}: {e}", chat_id)
                self._save()
        return new

    def _prune_requests(self) -> None:
        """Keep every pending request but only the latest decided ones (approvals are kept apart)."""
        reqs = self._state.get("requests", [])
        decided = [d for d in reqs if d.get("status") != "pending"]
        if len(decided) > DECIDED_KEEP:
            drop = {id(d) for d in decided[:-DECIDED_KEEP]}
            self._state["requests"] = [d for d in reqs if id(d) not in drop]

    # ---- decisions ----
    def _claim(self, key: str) -> None:
        if key in self._busy:
            raise AdminError("This action is already in progress.")
        self._busy.add(key)

    async def _send(self, chat_id: int, text: str, reply_markup: dict | None = None) -> None:
        try:
            await self._get_bot().send_message(chat_id, text, reply_markup=reply_markup)
        except BotApiError as e:
            raise AdminError(f"The bot could not send the message: {e}") from None

    async def approve(self, request_id: str) -> Approval:
        self._claim(request_id)
        try:
            _, req = self._find_request(request_id)
            if req.status != "pending":
                raise AdminError("This request was already handled.")
            now = self._now()
            grant = Grant(api_id=int(self._state["api_id"]), api_hash=self._state["api_hash"],
                          user_id=req.user_id, issued_at=now, expires_at=now + VALIDITY_DAYS * DAY)
            code = p.issue_code(self._owner(), req.device_pub, grant)
            markup = {"inline_keyboard": [[{"text": "📋 Copy activation code", "copy_text": {"text": code}}]]}
            await self._send(req.chat_id, msg_approved(req.device_label, code, VALIDITY_DAYS), markup)
            # The message is out: record the decision.
            i, req = self._find_request(request_id)
            req.status, req.decided_at = "approved", now
            self._state["requests"][i] = req.to_json()
            appr = Approval(id=_new_id("apr"), request_id=req.id, user_id=req.user_id, chat_id=req.chat_id,
                            username=req.username, name=req.name, device_label=req.device_label,
                            device_pub=req.device_pub, approved_at=now, expires_at=grant.expires_at)
            self._state.setdefault("approvals", []).append(appr.to_json())
            self._log("approved", f"Approved {req.display_name} on {req.device_label} for {VALIDITY_DAYS} days.",
                      req.user_id, approval_id=appr.id, device=req.device_label)
            self._save()
            return appr
        finally:
            self._busy.discard(request_id)

    async def deny(self, request_id: str) -> None:
        self._claim(request_id)
        try:
            _, req = self._find_request(request_id)
            if req.status != "pending":
                raise AdminError("This request was already handled.")
            await self._send(req.chat_id, msg_denied(req.device_label))
            i, req = self._find_request(request_id)
            req.status, req.decided_at = "denied", self._now()
            self._state["requests"][i] = req.to_json()
            self._log("denied", f"Denied {req.display_name} on {req.device_label}.", req.user_id,
                      device=req.device_label)
            self._save()
        finally:
            self._busy.discard(request_id)

    async def revoke(self, approval_id: str) -> None:
        self._claim(approval_id)
        try:
            _, appr = self._find_approval(approval_id)
            if appr.status != "active":
                raise AdminError("This access was already revoked.")
            now = self._now()
            token = p.issue_revoke(self._owner(), appr.device_pub, now)
            await self._send(appr.chat_id, msg_revoked(appr.device_label, token))
            i, appr = self._find_approval(approval_id)
            appr.status, appr.revoked_at = "revoked", now
            self._state["approvals"][i] = appr.to_json()
            self._log("revoked", f"Revoked {appr.display_name} on {appr.device_label}.", appr.user_id,
                      device=appr.device_label)
            self._save()
        finally:
            self._busy.discard(approval_id)

    def dismiss_all_pending(self) -> int:
        """Spam guard: drop all pending requests without replying."""
        n = 0
        for d in self._state.get("requests", []):
            if d.get("status") == "pending":
                d["status"], d["decided_at"] = "dismissed", self._now()
                n += 1
        if n:
            self._log("dismissed", f"Cleared {n} pending request(s) without replying.")
            self._save()
        return n

    def clear_history(self) -> None:
        self._state["history"] = []
        self._log("info", "History cleared.")
        self._save()


# ---------- listen loop (used by the UI) ----------

async def listen_loop(svc: AdminService,
                      on_status: Callable[[str, str | None], Awaitable[None] | None],
                      on_new: Callable[[list[Request]], Awaitable[None] | None],
                      *, timeout: int = 25, retry_delays: tuple[int, ...] = (3, 5, 10, 20, 30)) -> None:
    """Long-poll until cancelled. Status: "starting", "listening", "error" (with message)."""
    async def call(fn, *args):
        res = fn(*args)
        if asyncio.iscoroutine(res):
            await res

    failures = 0
    prepared = False
    await call(on_status, "starting", None)
    while True:
        await asyncio.sleep(0)                   # always let the UI run between polls
        try:
            if not prepared:
                await svc.prepare_listening()
                prepared = True
                await call(on_status, "listening", None)
            new = await svc.poll_once(timeout=timeout)
            if failures:
                failures = 0
                await call(on_status, "listening", None)
            if new:
                await call(on_new, new)
        except asyncio.CancelledError:
            raise
        except (AdminError, BotApiError) as e:
            delay = retry_delays[min(failures, len(retry_delays) - 1)]
            retry_after = getattr(e, "retry_after", None)
            if retry_after:
                delay = max(delay, int(retry_after))
            failures += 1
            await call(on_status, "error", f"{e} Retrying in {delay} s…")
            await asyncio.sleep(delay)
        except Exception as e:  # unexpected: show it, keep the window alive
            delay = retry_delays[-1]
            failures += 1
            await call(on_status, "error", f"Unexpected error ({type(e).__name__}). Retrying in {delay} s…")
            await asyncio.sleep(delay)
