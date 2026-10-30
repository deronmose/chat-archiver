"""Demo backend: fake Telegram and fake owner approval, for showing and testing the UI."""
from __future__ import annotations

import asyncio
import io
import random
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import png

from approval import protocol
from chat_archiver.backend import (AccessState, Account, ActivationError, Backend, ChatInfo, ExportOptions,
                                   ExportResult, Progress, ProgressFn, Resolved, Topic, UserFacingError)
from tg_export.exporters import export_all
from tg_export.refs import parse_chat_ref
from tg_export.scraper import GENERAL_TOPIC, chat_folder_name, sanitize_filename
from tg_export.store import Archive

DEMO_USER = Account(1000, "Alex Demo", "alex_demo")
_FORUM_TOPICS = {GENERAL_TOPIC: "General", 3: "Announcements", 5: "Design", 8: "Bugs & feedback"}
_CHATS = [
    (ChatInfo(-1001000000003, "Product Team", "group", "product_team", forum=True), 240),
    (ChatInfo(-1001000000004, "Crypto News Daily", "channel", "crypto_news_daily", protected=True), 120),
    (ChatInfo(-1001000000005, "Family", "group"), 80),
    (ChatInfo(-1001000000006, "Big Archive", "group"), 1200),
    (ChatInfo(-1001000000007, "Weekend Hikers", "group", "weekend_hikers"), 60),
]
_INVITE = ("demoinvite", ChatInfo(-1001000000009, "Secret Club", "group"), 45)
_NAMES = ["Anna", "Max", "Olena", "Dmytro", "Sofia", "Ivan"]
_TEXTS = ["Morning everyone!", "Here is the latest update.", "Can someone review this?", "Looks good to me 👍",
          "Meeting moved to 3 pm.", "Thanks!", "I'll take care of it.", "See the attached file."]


def _png(seed: int) -> bytes:
    rnd = random.Random(seed)
    color = [rnd.randrange(60, 220) for _ in range(3)]
    rows = [[c for _ in range(64) for c in color] for _ in range(48)]
    buf = io.BytesIO()
    png.Writer(64, 48, greyscale=False).write(buf, rows)
    return buf.getvalue()


class DemoBackend(Backend):
    def __init__(self, home: Path, fast: bool = False):
        self.home = Path(home)
        self.fast = fast
        self.owner = protocol.new_owner_key()
        self.device = protocol.new_device_key()
        self.grant: protocol.Grant | None = None
        self.authorized = False
        self._polls = 0
        self._requested = False
        self._phone_ok = False
        self.chats_list = [c for c, _ in _CHATS]
        self.counts = {c.id: n for c, n in _CHATS}

    async def _pause(self, seconds: float) -> None:
        await asyncio.sleep(0 if self.fast else seconds)

    # ---------- activation ----------
    def access_state(self) -> AccessState:
        if self.grant and not protocol.is_expired(self.grant, time.time()):
            return AccessState.ACTIVE
        return AccessState.NEEDS_ACTIVATION

    def request_url(self) -> str:
        self._requested = True
        return "https://t.me/chat_archiver_demo_bot?start=" + protocol.make_start_payload(self.device.public, "DEMO-PC")

    async def poll_activation(self) -> str | None:
        """Pretends the owner approved the request a few seconds after it was sent."""
        if not self._requested:
            return None
        self._polls += 1
        if self._polls < (1 if self.fast else 4):
            return None
        now = int(time.time())
        return protocol.issue_code(self.owner, self.device.public, protocol.Grant(
            1234567, "0123456789abcdef0123456789abcdef", DEMO_USER.id, now, now + protocol.VALIDITY_DAYS * 86400))

    def try_activate(self, text: str | None) -> bool:
        code = protocol.find_code(text)
        if code is None:
            return False
        try:
            self.grant = protocol.open_code(code, self.device, self.owner.verify_key)
        except protocol.InvalidCode as e:
            raise ActivationError(str(e)) from e
        return True

    # ---------- login ----------
    async def connect(self) -> None:
        await self._pause(0.3)

    async def is_authorized(self) -> bool:
        return self.authorized

    async def qr_start(self) -> str:
        return "tg://login?token=DEMO" + protocol._b64e(random.randbytes(24))

    async def qr_wait(self, timeout: float) -> str:
        await self._pause(min(timeout, 4))
        self.authorized = True
        return "ok"

    async def send_code(self, phone: str) -> None:
        digits = "".join(ch for ch in phone if ch.isdigit())
        if len(digits) < 7:
            raise UserFacingError("Enter the phone number in international format, e.g. +380501234567.")
        await self._pause(0.6)
        self._phone_ok = True

    async def sign_in_code(self, code: str) -> str:
        await self._pause(0.4)
        if not self._phone_ok or code.strip() != "12345":
            raise UserFacingError("The code is incorrect. Check the code Telegram sent you. (Demo code: 12345)")
        return "password"

    async def sign_in_password(self, password: str) -> None:
        await self._pause(0.4)
        if password != "demo":
            raise UserFacingError("Wrong password. Enter your Telegram two-step verification password. (Demo: demo)")
        self.authorized = True

    async def account(self) -> Account:
        return DEMO_USER

    async def verify_access(self) -> None:
        await self._pause(0.3)

    # ---------- chats ----------
    async def chats(self) -> list[ChatInfo]:
        await self._pause(0.5)
        return list(self.chats_list)

    def _chat(self, chat_id: int) -> ChatInfo:
        for c in self.chats_list:
            if c.id == chat_id:
                return c
        raise UserFacingError("Chat not found among your chats.")

    async def resolve(self, link: str) -> Resolved:
        await self._pause(0.4)
        try:
            ref = parse_chat_ref(link)
        except ValueError:
            raise UserFacingError("This doesn't look like a Telegram chat link. "
                                  "Paste a link like t.me/name, t.me/+invite or @name.")
        if ref.kind == "invite":
            if ref.value != _INVITE[0]:
                raise UserFacingError("This invite link is not valid.")
            joined = any(c.id == _INVITE[1].id for c in self.chats_list)
            return Resolved(_INVITE[1]) if joined else Resolved(None, needs_join=True, invite_title=_INVITE[1].title)
        if ref.kind == "username":
            chat = next((c for c in self.chats_list if c.username == ref.value), None)
            if chat is None:
                raise UserFacingError("No chat with this username.")
        else:
            chat = next((c for c in self.chats_list if c.id == ref.value), None)
            if chat is None:
                raise UserFacingError("Chat not found among your chats.")
        topic = None
        if chat.forum:
            topic = ref.topic_hint or (ref.post_id if ref.post_id in _FORUM_TOPICS else None)
        return Resolved(chat, topic_id=topic)

    async def join(self, link: str) -> ChatInfo:
        await self._pause(0.6)
        chat = _INVITE[1]
        if chat not in self.chats_list:
            self.chats_list.append(chat)
            self.counts[chat.id] = _INVITE[2]
        return chat

    async def topics(self, chat_id: int) -> list[Topic]:
        await self._pause(0.3)
        if not self._chat(chat_id).forum:
            return []
        return [Topic(i, t) for i, t in _FORUM_TOPICS.items()]

    def demo_message_count(self, chat_id: int) -> int:
        return self.counts[chat_id]

    def _rows(self, chat: ChatInfo):
        start = datetime(2026, 1, 5, 9, 0, tzinfo=timezone.utc)
        topic_ids = list(_FORUM_TOPICS)
        for i in range(1, self.counts[chat.id] + 1):
            rnd = random.Random(chat.id * 7919 + i)
            has_photo = i % 9 == 0
            yield {
                "id": i, "date": (start + timedelta(minutes=37 * i)).isoformat(),
                "sender_id": 2000 + rnd.randrange(len(_NAMES)), "sender_name": rnd.choice(_NAMES),
                "text": rnd.choice(_TEXTS), "reply_to": i - 1 if i > 1 and i % 5 == 0 else None,
                "topic_id": topic_ids[i % len(topic_ids)] if chat.forum else None,
                "media_type": "MessageMediaPhoto" if has_photo else None,
                "media_name": None, "media_size": 900 if has_photo else None,
            }

    async def export(self, chat_id: int, topic_id: int | None, options: ExportOptions,
                     on_progress: ProgressFn) -> ExportResult:
        chat = self._chat(chat_id)
        name = chat_folder_name(chat.title, chat.id)
        if topic_id is not None:
            name += f"_topic{topic_id}_{sanitize_filename(_FORUM_TOPICS.get(topic_id, ''), 30)}"
        folder = Path(options.folder) / name
        rows = [r for r in self._rows(chat) if topic_id is None or r["topic_id"] == topic_id]
        with Archive(folder / "messages.db") as archive:
            if chat.forum:
                archive.set_topics(_FORUM_TOPICS)
            pending = [r for r in rows if r["id"] > archive.last_id()]
            for i in range(0, len(pending), 40):
                archive.upsert(pending[i:i + 40])
                on_progress(Progress("messages", archive.count(), len(rows)))
                await asyncio.sleep(0.01 if self.fast else 0.08)
            on_progress(Progress("messages", archive.count(), len(rows)))
            ok = 0
            if options.media:
                todo = archive.pending_media()
                (folder / "media").mkdir(exist_ok=True)
                for n, mid in enumerate(todo, 1):
                    path = folder / "media" / f"{mid}.png"
                    path.write_bytes(_png(mid))
                    archive.set_media(mid, f"media/{mid}.png")
                    ok += 1
                    on_progress(Progress("media", n, len(todo)))
                    await asyncio.sleep(0 if self.fast else 0.03)
            title = chat.title + (f" — {_FORUM_TOPICS[topic_id]}" if topic_id is not None else "")
            archive.set_meta(chat_id=chat.id, title=title, username=chat.username, forum=int(chat.forum),
                             exported_at=datetime.now(timezone.utc).isoformat(timespec="seconds"))
            on_progress(Progress("files"))
            export_all(archive, folder, list(options.formats))
            count = archive.count()
        on_progress(Progress("done", count, count))
        return ExportResult(folder, folder / "chat.html", count, ok, 0)

    async def logout(self) -> None:
        self.authorized = False
