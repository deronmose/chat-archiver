"""Resolving a chat, reading its history into the archive and downloading media."""
from __future__ import annotations

import asyncio
import os
import re
import sys
import unicodedata
from datetime import datetime, timezone
from pathlib import Path

from telethon import errors, types, utils
from telethon.tl import functions

from tg_export.refs import ChatRef
from tg_export.store import DOWNLOADABLE, Archive

BATCH = 200
_BAD_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f\x7f]')
_RESERVED = re.compile(r"^(con|prn|aux|nul|com\d|lpt\d)(\..*)?$", re.IGNORECASE)


class UserError(Exception):
    """Problem the user can fix; printed without a traceback."""


# ---------- pure helpers (unit-tested) ----------

def sanitize_filename(name: str, max_len: int = 80) -> str:
    name = unicodedata.normalize("NFKC", name or "")
    name = re.split(r"[\\/]", name)[-1]              # drop any path components
    name = _BAD_CHARS.sub("_", name).strip(" .")
    if _RESERVED.match(name):
        name = "_" + name
    if len(name) > max_len:
        stem, dot, ext = name.rpartition(".")
        if dot and 0 < len(ext) <= 10:
            name = stem[: max_len - len(ext) - 1].rstrip(" .") + "." + ext
        else:
            name = name[:max_len].rstrip(" .")
    return name


def media_filename(msg_id: int, original_name: str | None, ext: str | None) -> str:
    base = sanitize_filename(original_name or "")
    ext = sanitize_filename(ext or "")
    if ext and not ext.startswith("."):
        ext = "." + ext
    if base:
        if ext and not base.lower().endswith(ext.lower()):
            base += ext
        return f"{msg_id}_{base}"
    return f"{msg_id}{ext or '.bin'}"


def chat_folder_name(title: str, chat_id: int) -> str:
    # a title is not a path: "/" in it is a character to replace, not a separator
    title = re.sub(r"[\\/]", "_", title or "")
    return f"{sanitize_filename(title, 60) or 'chat'}_{abs(chat_id)}"


def _iso(dt) -> str | None:
    return dt.astimezone(timezone.utc).isoformat() if dt else None


def _display_name(entity) -> str | None:
    if entity is None:
        return None
    if isinstance(entity, types.User):
        return " ".join(p for p in (entity.first_name, entity.last_name) if p) or None
    return getattr(entity, "title", None)


GENERAL_TOPIC = 1


def topic_of(msg, forum: bool) -> int | None:
    """Forum topic id of a message; messages outside any topic belong to General (1)."""
    if not forum:
        return None
    if isinstance(getattr(msg, "action", None), types.MessageActionTopicCreate):
        return msg.id
    reply = getattr(msg, "reply_to", None)
    if reply is not None and getattr(reply, "forum_topic", False):
        return reply.reply_to_top_id or reply.reply_to_msg_id
    return GENERAL_TOPIC


async def fetch_topics(client, entity) -> dict[int, str]:
    """All forum topics of a chat as {id: title}."""
    topics: dict[int, str] = {}
    offset_date, offset_id, offset_topic = None, 0, 0
    while True:
        res = await client(functions.messages.GetForumTopicsRequest(
            peer=entity, offset_date=offset_date, offset_id=offset_id, offset_topic=offset_topic, limit=100))
        page = [t for t in res.topics if isinstance(t, types.ForumTopic)]
        for t in page:
            topics[t.id] = t.title
        if not page or len(topics) >= res.count:
            break
        last = page[-1]
        top = next((m for m in res.messages if m.id == last.top_message), None)
        offset_date, offset_id, offset_topic = (top.date if top else None), last.top_message, last.id
    topics.setdefault(GENERAL_TOPIC, "General")
    return topics


def message_to_row(msg, include_raw: bool = False, forum: bool = False) -> dict:
    sender = getattr(msg, "_sender", None)          # filled by iter_messages, no extra request
    fwd = getattr(msg, "fwd_from", None)
    fwd_from = None
    if fwd is not None:
        fwd_from = fwd.from_name or (str(utils.get_peer_id(fwd.from_id)) if fwd.from_id else None)
    media = getattr(msg, "media", None)
    file = msg.file if media is not None else None
    reply = getattr(msg, "reply_to", None)
    action = getattr(msg, "action", None)
    return {
        "id": msg.id,
        "date": _iso(msg.date),
        "edit_date": _iso(getattr(msg, "edit_date", None)),
        "sender_id": msg.sender_id,
        "sender_name": _display_name(sender),
        "sender_username": getattr(sender, "username", None),
        "text": getattr(msg, "message", None) or "",
        "reply_to": getattr(reply, "reply_to_msg_id", None),
        "fwd_from": fwd_from,
        "views": getattr(msg, "views", None),
        "forwards": getattr(msg, "forwards", None),
        "post_author": getattr(msg, "post_author", None),
        "grouped_id": getattr(msg, "grouped_id", None),
        "topic_id": topic_of(msg, forum),
        "service": type(action).__name__ if action is not None else None,
        "media_type": type(media).__name__ if media is not None else None,
        "media_name": getattr(file, "name", None) if file else None,
        "media_size": getattr(file, "size", None) if file else None,
        "raw": msg.to_json() if include_raw else None,
    }


# ---------- Telegram side ----------

async def resolve_chat(client, ref: ChatRef, allow_join: bool):
    if ref.kind == "invite":
        info = await client(functions.messages.CheckChatInviteRequest(ref.value))
        if isinstance(info, (types.ChatInviteAlready, types.ChatInvitePeek)):
            return info.chat
        if not allow_join:
            raise UserError(
                f"Вы не состоите в «{info.title}». Вступите в группу в Telegram и повторите, "
                "или запустите с флагом --join, чтобы вступить по этой ссылке."
            )
        try:
            updates = await client(functions.messages.ImportChatInviteRequest(ref.value))
        except errors.InviteRequestSentError:
            raise UserError("Заявка на вступление отправлена; дождитесь одобрения админом и повторите.")
        return updates.chats[0]
    if ref.kind == "username":
        try:
            return await client.get_entity(ref.value)
        except (ValueError, errors.UsernameNotOccupiedError, errors.UsernameInvalidError):
            raise UserError(f"Чат @{ref.value} не найден.")
    wanted = int(ref.value)
    async for dialog in client.iter_dialogs():
        if dialog.id == wanted or dialog.entity.id == wanted:
            return dialog.entity
    raise UserError(f"Чат с ID {wanted} не найден среди ваших диалогов (команда chats покажет ID).")


def _progress(text: str) -> None:
    sys.stdout.write("\r" + text.ljust(70))
    sys.stdout.flush()


async def dump_messages(client, entity, archive: Archive, *, limit: int | None,
                        since: datetime | None, include_raw: bool, topic_id: int | None = None,
                        forum: bool = False, on_progress=None) -> int:
    """Append new messages to the archive. With topic_id only that forum topic is read
    (General has no thread of its own, so it is filtered from the full history)."""
    def report(done, total):
        if on_progress:
            on_progress(done, total)
        else:
            _progress(f"  сообщений: {done} из ~{total}")

    thread = topic_id if topic_id not in (None, GENERAL_TOPIC) else None
    kwargs = {"reply_to": thread} if thread else {}
    start_after = archive.last_id()
    total = (await client.get_messages(entity, limit=0, **kwargs)).total
    batch, added = [], 0
    async for msg in client.iter_messages(entity, reverse=True, min_id=start_after,
                                          offset_date=since, limit=limit, **kwargs):
        row = message_to_row(msg, include_raw, forum)
        if topic_id == GENERAL_TOPIC and row["topic_id"] != GENERAL_TOPIC:
            continue
        batch.append(row)
        if len(batch) >= BATCH:
            archive.upsert(batch)
            added += len(batch)
            batch.clear()
            report(archive.count(), total)
    archive.upsert(batch)
    added += len(batch)
    report(archive.count(), total)
    if not on_progress:
        print()
    return added


async def download_media(client, entity, archive: Archive, media_dir: Path, *,
                         max_bytes: int | None, workers: int = 3, retry_failed: bool = False,
                         on_progress=None) -> tuple[int, int]:
    ids = archive.pending_media(retry_failed)
    if not ids:
        return 0, 0
    media_dir.mkdir(parents=True, exist_ok=True)
    root = media_dir.resolve()
    sem = asyncio.Semaphore(workers)
    ok = failed = 0

    async def one(msg):
        nonlocal ok, failed
        if msg is None or not isinstance(msg.media, (types.MessageMediaPhoto, types.MessageMediaDocument)):
            return
        size = msg.file.size if msg.file else None
        if max_bytes is not None and size and size > max_bytes:
            archive.set_media(msg.id, None, f"skipped: {size} bytes > limit")
            return
        target = (root / media_filename(msg.id, msg.file.name, msg.file.ext)).resolve()
        if target.parent != root:                    # defence in depth against path tricks
            archive.set_media(msg.id, None, "unsafe file name")
            failed += 1
            return
        part = target.with_name(target.name + ".part")
        async with sem:
            try:
                await client.download_media(msg, file=str(part))
                os.replace(part, target)
            except Exception as e:                   # one bad file must not stop the archive
                part.unlink(missing_ok=True)
                archive.set_media(msg.id, None, f"{type(e).__name__}: {e}"[:300])
                failed += 1
                return
        archive.set_media(msg.id, str(Path(media_dir.name) / target.name), None)
        ok += 1
        if on_progress:
            on_progress(ok + failed, len(ids))
        else:
            _progress(f"  медиа: {ok + failed} из {len(ids)}")

    for i in range(0, len(ids), 100):
        chunk = ids[i:i + 100]
        messages = await client.get_messages(entity, ids=chunk)
        await asyncio.gather(*(one(m) for m in messages))
    if not on_progress:
        print()
    return ok, failed


__all__ = ["DOWNLOADABLE", "UserError", "chat_folder_name", "download_media", "dump_messages",
           "media_filename", "message_to_row", "resolve_chat", "sanitize_filename"]
