"""Real backend: Telethon through the read-only guard, keys delivered by activation code."""
from __future__ import annotations

import asyncio
import platform
import re
import time
from datetime import datetime, timezone
from pathlib import Path

from telethon import errors, types, utils
from telethon.sessions import StringSession
from telethon.tl import functions

from approval import protocol
from chat_archiver import __version__
from chat_archiver.backend import (ACCESS_MESSAGES, AccessDenied, AccessState, Account, ActivationError, Backend,
                                   ChatInfo, ExportOptions, ExportResult, Progress, ProgressFn, Resolved, Topic,
                                   UserFacingError, evaluate_access, friendly_error)
from chat_archiver.config import BuildConfig
from tg_export.exporters import export_all
from tg_export.guard import ReadOnlyClient
from tg_export.refs import parse_chat_ref
from tg_export.scraper import (GENERAL_TOPIC, chat_folder_name, download_media, dump_messages, fetch_topics,
                               resolve_chat, sanitize_filename)
from tg_export.store import Archive
from tg_export.vault import Vault

_PHONE = re.compile(r"^\+?\d{7,15}$")


def _chat_info(entity) -> ChatInfo:
    is_channel = isinstance(entity, types.Channel)
    kind = "channel" if is_channel and entity.broadcast else "group"
    return ChatInfo(id=utils.get_peer_id(entity), title=getattr(entity, "title", "") or "Untitled", kind=kind,
                    username=getattr(entity, "username", None), protected=bool(getattr(entity, "noforwards", False)),
                    forum=bool(getattr(entity, "forum", False)))


class TelethonBackend(Backend):
    def __init__(self, home: Path, config: BuildConfig | None):
        self.config = config
        self.vault = Vault(Path(home) / "vault.bin")
        self.data = self.vault.load()
        if "device_key" not in self.data:
            self.data["device_key"] = protocol.new_device_key().private.hex()
            self.vault.save(self.data)
        self.device = protocol.device_key_from_private(bytes.fromhex(self.data["device_key"]))
        self._client: ReadOnlyClient | None = None
        self._qr = None
        self._phone: str | None = None
        self._entities: dict[int, object] = {}

    # ---------- activation ----------
    def _grant(self) -> protocol.Grant | None:
        g = self.data.get("grant")
        return protocol.Grant(**g) if g else None

    def _save(self) -> None:
        if self._client is not None:
            self.data["session"] = self._client.session.save()
        self.vault.save(self.data)

    def reset_access(self) -> None:
        self._drop_grant()

    def _drop_grant(self) -> None:
        self.data.pop("grant", None)
        self._save()

    def access_state(self) -> AccessState:
        if self.config is None:
            return AccessState.NOT_CONFIGURED
        grant = self._grant()
        if grant is None:
            return AccessState.NEEDS_ACTIVATION
        if protocol.is_expired(grant, time.time()):
            self._drop_grant()
            return AccessState.NEEDS_ACTIVATION
        return AccessState.ACTIVE

    def request_url(self) -> str:
        payload = protocol.make_start_payload(self.device.public, protocol.device_label(platform.node()))
        return f"https://t.me/{self.config.bot_username}?start={payload}"

    def try_activate(self, text: str | None) -> bool:
        code = protocol.find_code(text)
        if code is None:
            return False
        try:
            grant = protocol.open_code(code, self.device, self.config.owner_verify_key)
        except protocol.InvalidCode as e:
            raise ActivationError(str(e)) from e
        if protocol.is_expired(grant, time.time()):
            raise ActivationError("This activation code has already expired. Please request access again.")
        self.data["grant"] = grant.__dict__.copy()
        self._save()
        return True

    # ---------- login ----------
    def make_client(self) -> ReadOnlyClient:
        grant = self._grant()
        if grant is None or self.config is None:
            raise ActivationError("This computer is not activated yet.")
        return ReadOnlyClient(
            StringSession(self.data.get("session") or ""), grant.api_id, grant.api_hash,
            receive_updates=True,              # QR login is confirmed through an update
            flood_sleep_threshold=3600,
            device_model="Chat Archiver", system_version=platform.system(), app_version=__version__,
        )

    async def _call(self, coro):
        try:
            return await coro
        except (UserFacingError, asyncio.CancelledError):
            raise
        except errors.SessionPasswordNeededError:
            raise
        except Exception as e:
            raise UserFacingError(friendly_error(e)) from e

    @property
    def client(self) -> ReadOnlyClient:
        if self._client is None:
            raise UserFacingError("Not connected.")
        return self._client

    async def connect(self) -> None:
        if self._client is None:
            self._client = self.make_client()
        if not self._client.is_connected():
            await self._call(self._client.connect())

    async def is_authorized(self) -> bool:
        return await self._call(self.client.is_user_authorized())

    async def qr_start(self) -> str:
        await self.connect()                   # a logout (e.g. wrong account) drops the old client
        self._qr = await self._call(self.client.qr_login())
        return self._qr.url

    async def qr_wait(self, timeout: float) -> str:
        try:
            await self._qr.wait(timeout=timeout)
        except asyncio.TimeoutError:
            return "timeout"
        except errors.SessionPasswordNeededError:
            return "password"
        except Exception as e:
            raise UserFacingError(friendly_error(e)) from e
        self._save()
        return "ok"

    async def send_code(self, phone: str) -> None:
        phone = re.sub(r"[\s()-]", "", phone or "")
        if not _PHONE.match(phone):
            raise UserFacingError("Enter the phone number in international format, e.g. +380501234567.")
        await self.connect()
        await self._call(self.client.send_code_request(phone))
        self._phone = phone

    async def sign_in_code(self, code: str) -> str:
        try:
            await self._call(self.client.sign_in(phone=self._phone, code=(code or "").strip()))
        except errors.SessionPasswordNeededError:
            return "password"
        self._save()
        return "ok"

    async def sign_in_password(self, password: str) -> None:
        await self._call(self.client.sign_in(password=password))
        self._save()

    async def account(self) -> Account:
        me = await self._call(self.client.get_me())
        name = " ".join(p for p in (me.first_name, me.last_name) if p) or (me.username or str(me.id))
        return Account(me.id, name, me.username)

    async def verify_access(self) -> None:
        grant = self._grant()
        if grant is None:
            raise AccessDenied("expired", ACCESS_MESSAGES["expired"])
        me = await self._call(self.client.get_me())
        config = await self._call(self.client(functions.help.GetConfigRequest()))
        server_now = config.date.timestamp()
        # Revoke notices: every message *from the bot* since this grant was issued, however many
        # messages the user wrote in between. Fail closed: if the chat can't be read, no access.
        texts: list[str] = []
        try:
            bot = await self.client.get_entity(self.config.bot_username)
            issued = datetime.fromtimestamp(grant.issued_at, timezone.utc)
            async for m in self.client.iter_messages(bot, limit=None):
                if m.date and m.date < issued:
                    break                      # older than the grant: a revoke there can't apply
                if m.sender_id == self.config.bot_id and m.message:
                    texts.append(m.message)
        except (errors.UsernameNotOccupiedError, errors.UsernameInvalidError):
            pass                               # owner's bot was deleted: expiry still applies
        except Exception as e:
            raise UserFacingError("Couldn't check your access with the owner's bot. "
                                  "Check your internet connection and try again.") from e
        reason = evaluate_access(grant, me.id, server_now, texts, self.device.public, self.config.owner_verify_key)
        if reason is None:
            self._save()
            return
        if reason == "wrong_account":
            await self.logout()
        else:
            self._drop_grant()
        raise AccessDenied(reason, ACCESS_MESSAGES[reason])

    # ---------- chats ----------
    async def chats(self) -> list[ChatInfo]:
        out = []
        async for d in self.client.iter_dialogs():
            if d.is_user:
                continue
            self._entities[d.id] = d.entity
            out.append(_chat_info(d.entity))
        self._save()
        return out

    async def _entity(self, chat_id: int):
        if chat_id not in self._entities:
            async for d in self.client.iter_dialogs():
                self._entities[d.id] = d.entity
        if chat_id not in self._entities:
            raise UserFacingError("Chat not found among your chats.")
        return self._entities[chat_id]

    async def resolve(self, link: str) -> Resolved:
        try:
            ref = parse_chat_ref(link)
        except ValueError:
            raise UserFacingError("This doesn't look like a Telegram chat link. "
                                  "Paste a link like t.me/name, t.me/+invite or @name.")
        if ref.kind == "invite":
            info = await self._call(self.client(functions.messages.CheckChatInviteRequest(ref.value)))
            if not isinstance(info, (types.ChatInviteAlready, types.ChatInvitePeek)):
                return Resolved(None, needs_join=True, invite_title=info.title)
            entity = info.chat
        else:
            entity = await self._call(resolve_chat(self.client, ref, allow_join=False))
        if isinstance(entity, types.User):
            raise UserFacingError("This is a person, not a group or channel.")
        chat = _chat_info(entity)
        self._entities[chat.id] = entity
        topic = None
        if chat.forum:
            if ref.topic_hint:
                topic = ref.topic_hint
            elif ref.post_id:
                ids = {t.id for t in await self.topics(chat.id)}
                topic = ref.post_id if ref.post_id in ids else None
        return Resolved(chat, topic_id=topic)

    async def join(self, link: str) -> ChatInfo:
        ref = parse_chat_ref(link)
        if ref.kind != "invite":
            raise UserFacingError("Only invite links can be joined.")
        self.client._allow_join = True         # the user confirmed joining in a dialog
        try:
            updates = await self._call(self.client(functions.messages.ImportChatInviteRequest(ref.value)))
        finally:
            self.client._allow_join = False
        chat = _chat_info(updates.chats[0])
        self._entities[chat.id] = updates.chats[0]
        return chat

    async def topics(self, chat_id: int) -> list[Topic]:
        entity = await self._entity(chat_id)
        found = await self._call(fetch_topics(self.client, entity))
        return sorted((Topic(i, t) for i, t in found.items()), key=lambda t: (t.id != GENERAL_TOPIC, t.title.lower()))

    async def export(self, chat_id: int, topic_id: int | None, options: ExportOptions,
                     on_progress: ProgressFn) -> ExportResult:
        entity = await self._entity(chat_id)
        chat = _chat_info(entity)
        topics = {t.id: t.title for t in await self.topics(chat_id)} if chat.forum else {}
        name = chat_folder_name(chat.title, chat.id)
        if topic_id is not None:
            name += f"_topic{topic_id}_{sanitize_filename(topics.get(topic_id, ''), 30)}".rstrip("_")
        folder = Path(options.folder) / name
        since = datetime(options.since.year, options.since.month, options.since.day, tzinfo=timezone.utc) \
            if options.since else None
        with Archive(folder / "messages.db") as archive:
            if topics:
                archive.set_topics(topics)
            await self._call(dump_messages(self.client, entity, archive, limit=None, since=since, include_raw=False,
                                           topic_id=topic_id, forum=chat.forum,
                                           on_progress=lambda d, t: on_progress(Progress("messages", d, t))))
            ok = failed = 0
            if options.media:
                on_progress(Progress("media", 0, None))
                max_bytes = int(options.max_media_mb * 1024 * 1024) if options.max_media_mb else None
                ok, failed = await self._call(download_media(
                    self.client, entity, archive, folder / "media", max_bytes=max_bytes,
                    on_progress=lambda d, t: on_progress(Progress("media", d, t))))
            title = chat.title + (f" — {topics.get(topic_id, 'topic')}" if topic_id is not None else "")
            archive.set_meta(chat_id=chat.id, title=title, username=chat.username, forum=int(chat.forum),
                             exported_at=datetime.now(timezone.utc).isoformat(timespec="seconds"))
            on_progress(Progress("files"))
            export_all(archive, folder, list(options.formats))
            count = archive.count()
        self._save()
        on_progress(Progress("done", count, count))
        html = folder / "chat.html"
        return ExportResult(folder, html if html.exists() else None, count, ok, failed)

    async def logout(self) -> None:
        if self._client is not None:
            try:
                await self._client.log_out()
            except Exception:
                pass
            self._client = None
        self.data.pop("session", None)
        self.vault.save(self.data)

    async def close(self) -> None:
        if self._client is not None:
            self._save()
            await self._client.disconnect()
