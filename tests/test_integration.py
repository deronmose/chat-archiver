"""End to end across packages: Admin approves a device, the app activates with Admin's build config."""
from __future__ import annotations

import asyncio
import tempfile
import time
import unittest
from pathlib import Path

from approval import protocol as p
from chat_archiver import backend as b
from chat_archiver.config import from_dict
from chat_archiver.telethon_backend import TelethonBackend
from chat_archiver_admin.service import AdminService
from tg_export.vault import Vault

from tests.test_admin import FakeBot, _start


class AdminToAppTests(unittest.TestCase):
    def test_request_approve_activate_revoke(self):
        with tempfile.TemporaryDirectory() as d:
            bot = FakeBot()
            admin = AdminService(vault=Vault(Path(d) / "admin.bin"), bot_factory=lambda t: bot, clock=time.time)
            asyncio.run(admin.setup(bot_token="123456789:x", api_id=7654321,
                                    api_hash="0123456789abcdef0123456789abcdef"))
            app = TelethonBackend(home=Path(d) / "app", config=from_dict(admin.build_config()))

            # the app's Request access link is what the user's Telegram sends to the bot as /start
            payload = app.request_url().split("start=")[1]
            bot.updates = [_start(1, 4242, f"/start {payload}")]
            req = asyncio.run(admin.poll_once())[0]
            asyncio.run(admin.approve(req.id))
            code = bot.sent[-1]["reply_markup"]["inline_keyboard"][0][0]["copy_text"]["text"]

            self.assertTrue(app.try_activate(f"clipboard: {code}"))
            self.assertEqual(app.access_state(), b.AccessState.ACTIVE)
            grant = app._grant()
            self.assertEqual((grant.api_id, grant.user_id), (7654321, 4242))

            appr = admin.approved()[0]
            asyncio.run(admin.revoke(appr.id))
            revoke_text = bot.sent[-1]["text"]
            reason = b.evaluate_access(grant, 4242, time.time(), [revoke_text], app.device.public,
                                       app.config.owner_verify_key)
            self.assertEqual(reason, "revoked")

    def test_code_from_other_admin_is_rejected(self):
        with tempfile.TemporaryDirectory() as d:
            mine, other = FakeBot(), FakeBot()
            admin = AdminService(vault=Vault(Path(d) / "a.bin"), bot_factory=lambda t: mine, clock=time.time)
            stranger = AdminService(vault=Vault(Path(d) / "b.bin"), bot_factory=lambda t: other, clock=time.time)
            for svc in (admin, stranger):
                asyncio.run(svc.setup(bot_token="123456789:x", api_id=1, api_hash="0123456789abcdef0123456789abcdef"))
            app = TelethonBackend(home=Path(d) / "app", config=from_dict(admin.build_config()))
            other.updates = [_start(1, 5, "/start " + app.request_url().split("start=")[1])]
            req = asyncio.run(stranger.poll_once())[0]
            asyncio.run(stranger.approve(req.id))
            code = other.sent[-1]["reply_markup"]["inline_keyboard"][0][0]["copy_text"]["text"]
            with self.assertRaises(b.ActivationError):
                app.try_activate(code)


if __name__ == "__main__":
    unittest.main()
