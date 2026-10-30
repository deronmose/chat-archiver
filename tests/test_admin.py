"""Contract tests for Chat Archiver Admin's service layer (K5). Fake Bot API, no network."""
from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path

from approval import protocol as p
from tg_export import vault as vault_mod
from tg_export.vault import Vault

from chat_archiver_admin.botapi import BotApiError
from chat_archiver_admin.service import AdminError, AdminService

NOW = 1_800_000_000
API_HASH = "0123456789abcdef0123456789abcdef"


class FakeBot:
    def __init__(self):
        self.updates: list[dict] = []
        self.sent: list[dict] = []
        self.offsets: list[int | None] = []
        self.fail_get_me = False

    async def get_me(self):
        if self.fail_get_me:
            raise BotApiError("Unauthorized")
        return {"id": 999, "is_bot": True, "username": "my_archiver_bot", "first_name": "Archiver"}

    async def delete_webhook(self):
        return True

    async def get_updates(self, offset=None, timeout=0):
        self.offsets.append(offset)
        out = [u for u in self.updates if offset is None or u["update_id"] >= offset]
        return out

    async def send_message(self, chat_id, text, reply_markup=None):
        self.sent.append({"chat_id": chat_id, "text": text, "reply_markup": reply_markup})
        return {"message_id": len(self.sent)}


def _start(update_id, user_id, text, *, is_bot=False, chat_type="private", username="ann"):
    return {"update_id": update_id, "message": {
        "message_id": update_id, "date": NOW,
        "from": {"id": user_id, "is_bot": is_bot, "first_name": "Ann", "last_name": "Lee", "username": username},
        "chat": {"id": user_id, "type": chat_type}, "text": text}}


class AdminServiceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.vault = Vault(Path(self.tmp.name) / "admin.bin")
        self.bot = FakeBot()
        self.svc = self._service()
        self.device = p.new_device_key()
        self.payload = p.make_start_payload(self.device.public, "DESKTOP-ANN")

    def tearDown(self):
        self.tmp.cleanup()

    def _service(self):
        return AdminService(vault=self.vault, bot_factory=lambda token: self.bot, clock=lambda: NOW)

    def run_(self, coro):
        return asyncio.run(coro)

    def _setup(self):
        self.run_(self.svc.setup(bot_token="123:SECRET-TOKEN", api_id=1234567, api_hash=API_HASH))

    # --- setup ---
    def test_setup_and_build_config_has_no_secrets(self):
        self.assertFalse(self.svc.is_configured)
        self._setup()
        self.assertTrue(self.svc.is_configured)
        self.assertEqual((self.svc.bot_username, self.svc.bot_id), ("my_archiver_bot", 999))
        cfg = self.svc.build_config()
        self.assertEqual(set(cfg), {"bot_username", "bot_id", "owner_verify_key"})
        flat = repr(cfg)
        for secret in ("SECRET-TOKEN", API_HASH, "1234567"):
            self.assertNotIn(secret, flat)

    def test_setup_rejects_bad_token_and_bad_keys(self):
        self.bot.fail_get_me = True
        with self.assertRaises(AdminError):
            self.run_(self.svc.setup(bot_token="bad", api_id=1, api_hash=API_HASH))
        self.bot.fail_get_me = False
        with self.assertRaises(AdminError):
            self.run_(self.svc.setup(bot_token="123:x", api_id=1, api_hash="nothex"))
        self.assertFalse(self.svc.is_configured)

    # --- requests ---
    def test_poll_parses_requests_and_ignores_junk(self):
        self._setup()
        self.bot.updates = [
            _start(1, 42, f"/start {self.payload}"),
            _start(2, 43, "/start hello"),
            _start(3, 44, "hi"),
            _start(4, 45, f"/start {self.payload}", is_bot=True),
            _start(5, 46, f"/start {self.payload}", chat_type="group"),
            {"update_id": 6, "callback_query": {"id": "x"}},
        ]
        new = self.run_(self.svc.poll_once())
        self.assertEqual(len(new), 1)
        r = new[0]
        self.assertEqual((r.user_id, r.username, r.name, r.device_label, r.status),
                         (42, "ann", "Ann Lee", "DESKTOP-ANN", "pending"))
        self.assertEqual(r.device_pub, self.device.public)
        self.assertEqual([x.id for x in self.svc.pending()], [r.id])
        self.assertTrue(any(m["chat_id"] == 42 for m in self.bot.sent))      # "request received" reply
        self.run_(self.svc.poll_once())
        self.assertEqual(self.bot.offsets[-1], 7)                             # offset advanced past junk

    def test_duplicate_request_is_not_duplicated(self):
        self._setup()
        self.bot.updates = [_start(1, 42, f"/start {self.payload}")]
        self.run_(self.svc.poll_once())
        self.bot.updates = [_start(2, 42, f"/start {self.payload}")]
        self.assertEqual(self.run_(self.svc.poll_once()), [])
        self.assertEqual(len(self.svc.pending()), 1)

    # --- decisions ---
    def test_approve_sends_device_bound_code_in_copy_button(self):
        self._setup()
        self.bot.updates = [_start(1, 42, f"/start {self.payload}")]
        req = self.run_(self.svc.poll_once())[0]
        appr = self.run_(self.svc.approve(req.id))
        self.assertEqual(appr.expires_at, NOW + 14 * 86400)
        msg = self.bot.sent[-1]
        self.assertEqual(msg["chat_id"], 42)
        code = msg["reply_markup"]["inline_keyboard"][0][0]["copy_text"]["text"]
        grant = p.open_code(code, self.device, self._verify_key())
        self.assertEqual((grant.api_id, grant.api_hash, grant.user_id), (1234567, API_HASH, 42))
        self.assertEqual(self.svc.pending(), [])
        self.assertEqual([a.user_id for a in self.svc.approved()], [42])

    def _verify_key(self):
        return p._b64d(self.svc.build_config()["owner_verify_key"])

    def test_deny(self):
        self._setup()
        self.bot.updates = [_start(1, 42, f"/start {self.payload}")]
        req = self.run_(self.svc.poll_once())[0]
        self.run_(self.svc.deny(req.id))
        self.assertEqual(self.svc.pending(), [])
        self.assertEqual(self.bot.sent[-1]["chat_id"], 42)
        self.assertIsNone(p.find_code(self.bot.sent[-1]["text"]))

    def test_revoke_sends_valid_notice(self):
        self._setup()
        self.bot.updates = [_start(1, 42, f"/start {self.payload}")]
        req = self.run_(self.svc.poll_once())[0]
        appr = self.run_(self.svc.approve(req.id))
        self.run_(self.svc.revoke(appr.id))
        text = self.bot.sent[-1]["text"]
        self.assertIsNotNone(p.check_revoke(text, self.device.public, self._verify_key()))
        self.assertEqual(self.svc.approved(), [])

    # --- persistence ---
    def test_state_survives_restart_and_is_encrypted(self):
        self._setup()
        self.bot.updates = [_start(1, 42, f"/start {self.payload}")]
        self.run_(self.svc.poll_once())
        again = self._service()
        self.assertTrue(again.is_configured)
        self.assertEqual(len(again.pending()), 1)
        self.assertGreaterEqual(len(again.history()), 1)
        self.run_(again.poll_once())
        self.assertEqual(self.bot.offsets[-1], 2)
        if vault_mod.ENCRYPTED:
            raw = self.vault.path.read_bytes()
            self.assertNotIn(b"SECRET-TOKEN", raw)
            self.assertNotIn(API_HASH.encode(), raw)


if __name__ == "__main__":
    unittest.main()
