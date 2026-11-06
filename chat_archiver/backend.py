"""Contract between the UI and Telegram: data types, errors, access rules.

The UI only talks to a Backend. TelethonBackend is the real one; DemoBackend fakes
Telegram and the owner's approval so the UI can be shown and tested without accounts.
"""
from __future__ import annotations

import abc
import ctypes
import sys
from dataclasses import dataclass
from datetime import date
from enum import Enum
from pathlib import Path
from typing import Callable

from telethon import errors

from approval import protocol
from tg_export.scraper import UserError


class AccessState(Enum):
    NOT_CONFIGURED = "not_configured"      # build has no owner config
    NEEDS_ACTIVATION = "needs_activation"
    ACTIVE = "active"


@dataclass(frozen=True)
class ChatInfo:
    id: int
    title: str
    kind: str                 # "channel" | "group"
    username: str | None = None
    protected: bool = False   # "restrict saving content" is on
    forum: bool = False       # group with topics


@dataclass(frozen=True)
class Topic:
    id: int
    title: str


@dataclass(frozen=True)
class Resolved:
    chat: ChatInfo | None
    topic_id: int | None = None
    needs_join: bool = False
    invite_title: str | None = None


@dataclass(frozen=True)
class Account:
    id: int
    name: str
    username: str | None


@dataclass(frozen=True)
class AccountEntry:
    """A Telegram account approved on this computer."""
    user_id: int
    name: str | None          # known after the first sign-in
    username: str | None
    signed_in: bool           # has a saved session

    @property
    def label(self) -> str:
        return self.name or f"Telegram ID {self.user_id}"


@dataclass
class ExportOptions:
    folder: Path
    media: bool = True
    max_media_mb: float | None = 50
    since: date | None = None
    formats: tuple[str, ...] = ("html", "csv", "json")


@dataclass(frozen=True)
class Progress:
    stage: str                # "messages" | "media" | "files" | "done"
    done: int = 0
    total: int | None = None


@dataclass(frozen=True)
class ExportResult:
    folder: Path
    html: Path | None
    messages: int
    media_ok: int = 0
    media_failed: int = 0


class UserFacingError(Exception):
    """Message is safe and meant to be shown to the user as is."""


class ActivationError(UserFacingError):
    pass


class AccessDenied(UserFacingError):
    def __init__(self, reason: str, message: str):
        super().__init__(message)
        self.reason = reason


ProgressFn = Callable[[Progress], None]


class Backend(abc.ABC):
    # activation (before login: the app has no API keys until the owner approves)
    @abc.abstractmethod
    def access_state(self) -> AccessState: ...
    @abc.abstractmethod
    def request_url(self) -> str: ...
    @abc.abstractmethod
    def try_activate(self, text: str | None) -> bool: ...
    async def poll_activation(self) -> str | None:
        return None

    # accounts: every Telegram account is approved separately; this computer remembers them all
    @abc.abstractmethod
    def accounts(self) -> list[AccountEntry]: ...
    @abc.abstractmethod
    def current_account(self) -> AccountEntry | None: ...
    @abc.abstractmethod
    async def select_account(self, user_id: int) -> None: ...
    @abc.abstractmethod
    async def remove_account(self, user_id: int) -> None: ...   # forget approval and session

    # login
    @abc.abstractmethod
    async def connect(self) -> None: ...
    @abc.abstractmethod
    async def is_authorized(self) -> bool: ...
    @abc.abstractmethod
    async def qr_start(self) -> str: ...
    @abc.abstractmethod
    async def qr_wait(self, timeout: float) -> str: ...      # "ok" | "password" | "timeout"
    @abc.abstractmethod
    async def send_code(self, phone: str) -> None: ...
    @abc.abstractmethod
    async def sign_in_code(self, code: str) -> str: ...      # "ok" | "password"
    @abc.abstractmethod
    async def sign_in_password(self, password: str) -> None: ...
    @abc.abstractmethod
    async def account(self) -> Account: ...
    @abc.abstractmethod
    async def verify_access(self) -> None: ...

    # chats and export
    @abc.abstractmethod
    async def chats(self) -> list[ChatInfo]: ...
    @abc.abstractmethod
    async def resolve(self, link: str) -> Resolved: ...
    @abc.abstractmethod
    async def join(self, link: str) -> ChatInfo: ...
    @abc.abstractmethod
    async def topics(self, chat_id: int) -> list[Topic]: ...
    @abc.abstractmethod
    async def export(self, chat_id: int, topic_id: int | None, options: ExportOptions,
                     on_progress: ProgressFn) -> ExportResult: ...
    @abc.abstractmethod
    async def logout(self) -> None: ...                         # signs the current account out, keeps its approval
    async def close(self) -> None:
        return None


# ---------- rules shared by both backends ----------

ACCESS_MESSAGES = {
    "wrong_account": "This Telegram account isn't approved on this computer yet. Sign in with an approved "
                     "account, or request access for this one.",
    "expired": "Your access has ended. Please request access again.",
    "revoked": "Your access has ended. Please request access again.",
}


def evaluate_access(grant: protocol.Grant, me_id: int, server_now: float, bot_texts: list[str],
                    device_pub: bytes, owner_verify_key: bytes) -> str | None:
    """None if access is valid, otherwise the reason it is not."""
    if me_id != grant.user_id:
        return "wrong_account"
    if protocol.is_expired(grant, server_now):
        return "expired"
    for text in bot_texts:
        revoked_at = protocol.check_revoke(text, device_pub, owner_verify_key)
        if revoked_at is not None and revoked_at >= grant.issued_at:
            return "revoked"
    return None


def friendly_error(exc: BaseException) -> str:
    if isinstance(exc, UserFacingError):
        return str(exc)
    if isinstance(exc, errors.FloodWaitError):
        s = int(exc.seconds)
        wait = f"{s} seconds" if s < 60 else f"{round(s / 60)} minutes"
        return f"Telegram asks to wait {wait} before trying again."
    table = [
        (errors.PhoneNumberInvalidError, "This phone number is not valid. Use the international format, e.g. +380501234567."),
        (errors.PhoneNumberBannedError, "This phone number is banned by Telegram."),
        (errors.PhoneCodeInvalidError, "The code is incorrect. Check the code Telegram sent you."),
        (errors.PhoneCodeExpiredError, "The code has expired. Request a new one."),
        (errors.PasswordHashInvalidError, "Wrong password. Enter your Telegram two-step verification password."),
        (errors.InviteHashExpiredError, "This invite link has expired."),
        (errors.InviteHashInvalidError, "This invite link is not valid."),
        (errors.InviteRequestSentError, "Join request sent. Wait until an admin approves it, then try again."),
        (errors.ChannelPrivateError, "This chat is private, or you are no longer a member."),
        (errors.UsernameNotOccupiedError, "No chat with this username."),
        (errors.UsernameInvalidError, "No chat with this username."),
        (errors.AuthKeyUnregisteredError, "You were logged out. Please sign in again."),
    ]
    for cls, text in table:
        if isinstance(exc, cls):
            return text
    if isinstance(exc, UserError):
        return str(exc)
    if isinstance(exc, (ConnectionError, OSError, TimeoutError)):
        return "Can't reach Telegram. Check your internet connection and try again."
    if isinstance(exc, errors.RPCError):
        return f"Telegram returned an error: {exc.message}"
    return f"Something went wrong: {type(exc).__name__}"


def default_export_folder() -> Path:
    if sys.platform == "win32":
        buf = ctypes.create_unicode_buffer(260)
        if ctypes.windll.shell32.SHGetFolderPathW(None, 5, None, 0, buf) == 0:   # CSIDL_PERSONAL
            return Path(buf.value) / "Chat Archiver"
    return Path.home() / "Documents" / "Chat Archiver"
