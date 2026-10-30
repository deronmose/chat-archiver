"""Parsing of chat references: invite links, public links, @usernames, IDs, topic links."""
from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import parse_qs

_HOST = r"(?:https?://)?(?:www\.)?(?:t\.me|telegram\.me|telegram\.dog)/"
_USERNAME = r"[A-Za-z][A-Za-z0-9_]{3,31}"
_TAIL = r"((?:/\d+)*)/?(?:\?(.*))?$"

_INVITE = re.compile(_HOST + r"(?:\+|joinchat/)([A-Za-z0-9_-]+)/?$")
_TG_INVITE = re.compile(r"tg://join\?invite=([A-Za-z0-9_-]+)$")
_PRIVATE_POST = re.compile(_HOST + r"c/(\d+)" + _TAIL)
_PUBLIC = re.compile(_HOST + r"(?:s/)?(" + _USERNAME + r")" + _TAIL)
_AT_USERNAME = re.compile(r"@?(" + _USERNAME + r")$")
_NUMERIC = re.compile(r"-?\d+$")


@dataclass(frozen=True)
class ChatRef:
    kind: str  # "invite" | "username" | "id"
    value: object
    topic_hint: int | None = None   # forum topic named explicitly by the link
    post_id: int | None = None      # message id from the link (may itself be a topic id)


def _numbers(path: str, query: str | None) -> tuple[int | None, int | None]:
    nums = [int(n) for n in path.split("/") if n]
    topic = post = None
    if len(nums) >= 2:
        topic, post = nums[0], nums[1]
    elif nums:
        post = nums[0]
    params = parse_qs(query or "")
    for key in ("thread", "topic"):
        if params.get(key, [""])[0].isdigit():
            topic = int(params[key][0])
    return topic, post


def parse_chat_ref(text: str) -> ChatRef:
    s = text.strip()
    for pattern in (_INVITE, _TG_INVITE):
        m = pattern.match(s)
        if m:
            return ChatRef("invite", m.group(1))
    m = _PRIVATE_POST.match(s)
    if m:
        return ChatRef("id", int("-100" + m.group(1)), *_numbers(m.group(2), m.group(3)))
    m = _PUBLIC.match(s)
    if m:
        return ChatRef("username", m.group(1), *_numbers(m.group(2), m.group(3)))
    if _NUMERIC.match(s):
        return ChatRef("id", int(s))
    m = _AT_USERNAME.match(s)
    if m:
        return ChatRef("username", m.group(1))
    raise ValueError(
        f"Не понимаю ссылку на чат: {text!r}. Подходит t.me/+приглашение, "
        "t.me/имя, @имя, t.me/c/123/45 или числовой ID из команды chats"
    )
