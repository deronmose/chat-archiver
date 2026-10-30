"""Demo mode: a simulated bot that produces access requests by itself.

Nothing is sent to Telegram. Requests carry real device keys, so approving them
produces real (but useless) activation codes.
"""
from __future__ import annotations

import asyncio
import random
import tempfile
import time
from pathlib import Path

from approval import protocol as p
from tg_export.vault import Vault

from .botapi import BotApiError

DEMO_TOKEN = "123456789:DEMO-not-a-real-token"
DEMO_API_ID = "1234567"
DEMO_API_HASH = "0123456789abcdef0123456789abcdef"

_PEOPLE = [
    ("Anna", "Kovalenko", "anna_k"), ("Mark", "Peters", "markp"), ("Olena", "Shevchuk", None),
    ("David", "Kim", "dkim"), ("Sofia", "Rossi", "sofia_r"), ("Taras", "Melnyk", "taras_m"),
    ("Emma", "Wilson", None), ("Lucas", "Garcia", "lucas_g"), ("Iryna", "Bondar", "iryna_b"),
    ("Noah", "Schmidt", "noah_s"),
]
_DEVICES = ["DESKTOP-7Q2K", "LAPTOP-HOME", "WORK-PC", "HOME-PC", "MSI-GF63", "THINKPAD-X1",
            "OFFICE-12", "SURFACE-PRO", "DELL-XPS", "PC"]


class DemoBot:
    """Same interface as BotApi; generates a /start request every `interval` seconds."""

    def __init__(self, token: str, *, interval: float = 8.0, first_after: float = 2.0,
                 max_requests: int = 30, seed: int | None = None):
        if not token or ":" not in token or not token.split(":", 1)[0].isdigit():
            raise BotApiError("This does not look like a bot token. It should look like 123456789:AA...")
        self._token = token
        self._interval = interval
        self._first_after = first_after
        self._max = max_requests
        self._rng = random.Random(seed)
        self._queue: list[dict] = []
        self._next_update_id = 1000
        self._next_at: float | None = None
        self._made = 0
        self._last: tuple[dict, str] | None = None
        self._people: list[int] = []
        self.sent: list[dict] = []

    def __repr__(self) -> str:
        return "DemoBot(token=***)"

    async def get_me(self) -> dict:
        if "bad" in self._token.lower():
            raise BotApiError("Telegram rejected the bot token (Unauthorized). Check the token from @BotFather.",
                              error_code=401)
        return {"id": 7000000001, "is_bot": True, "first_name": "Archiver Demo",
                "username": "chat_archiver_demo_bot"}

    async def delete_webhook(self) -> bool:
        return True

    def _make_update(self) -> dict:
        self._made += 1
        uid = self._next_update_id
        self._next_update_id += 1
        roll = self._rng.random()
        if self._last is not None and roll < 0.12:   # same person presses Start again
            sender, text = self._last
        else:
            if not self._people:
                self._people = list(range(len(_PEOPLE)))
                self._rng.shuffle(self._people)
            idx = self._people.pop()
            first, last, username = _PEOPLE[idx]
            sender = {"id": 5_100_000 + idx * 7919, "is_bot": False,
                      "first_name": first, "last_name": last}
            if username:
                sender["username"] = username
            if roll < 0.2:                           # someone opens the bot without the app
                text = "/start"
            else:
                device = p.new_device_key()
                text = "/start " + p.make_start_payload(device.public, self._rng.choice(_DEVICES))
                self._last = (sender, text)
        msg = {"message_id": uid, "date": int(time.time()), "from": sender,
               "chat": {"id": sender["id"], "type": "private"}, "text": text}
        return {"update_id": uid, "message": msg}

    def _generate_due(self) -> None:
        now = time.monotonic()
        if self._next_at is None:
            self._next_at = now + self._first_after
        while self._made < self._max and now >= self._next_at:
            self._queue.append(self._make_update())
            self._next_at += self._interval * self._rng.uniform(0.75, 1.25)

    async def get_updates(self, offset: int | None = None, timeout: int = 0) -> list[dict]:
        if offset is not None:
            self._queue = [u for u in self._queue if u["update_id"] >= offset]
        deadline = time.monotonic() + max(0, timeout)
        while True:
            self._generate_due()
            if self._queue or time.monotonic() >= deadline:
                return list(self._queue)
            await asyncio.sleep(0.25)

    async def send_message(self, chat_id: int, text: str, reply_markup: dict | None = None) -> dict:
        self.sent.append({"chat_id": chat_id, "text": text, "reply_markup": reply_markup})
        return {"message_id": len(self.sent)}


def demo_vault() -> tuple[Vault, tempfile.TemporaryDirectory]:
    """A throwaway encrypted vault in a temp folder (deleted on exit)."""
    tmp = tempfile.TemporaryDirectory(prefix="chat-archiver-admin-demo-")
    return Vault(Path(tmp.name) / "admin.bin"), tmp


def demo_bot_factory(token: str) -> DemoBot:
    return DemoBot(token)
