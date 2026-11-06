"""Chat Archiver service layer (K2, K3, K4): demo backend flow, access rules, real backend offline parts."""
from __future__ import annotations

import asyncio
import tempfile
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path

from telethon import errors, types
from telethon.sessions import StringSession

from approval import protocol as p
from chat_archiver import backend as b
from chat_archiver.config import BuildConfig
from chat_archiver.demo_backend import DemoBackend
from chat_archiver.telethon_backend import TelethonBackend
from tg_export.guard import ReadOnlyClient
from tg_export.vault import Vault

NOW = 1_800_000_000
API_HASH = "0123456789abcdef0123456789abcdef"


class _FakeMsg:
    def __init__(self, sender_id, text, ts):
        self.sender_id, self.message = sender_id, text
        self.date = datetime.fromtimestamp(ts, timezone.utc)


class _FakeClient:
    """Just enough of TelegramClient for verify_access()."""
    def __init__(self, me_id, history, fail_history=False):
        self.me_id, self.history, self.fail_history = me_id, history, fail_history
        self.session = StringSession()

    async def get_me(self):
        return types.User(id=self.me_id, first_name="X")

    async def __call__(self, request):
        class Cfg:
            date = datetime.now(timezone.utc)
        return Cfg()

    async def get_entity(self, username):
        return types.User(id=999, first_name="Bot", bot=True)

    async def iter_messages(self, entity, limit=None):
        if self.fail_history:
            raise ConnectionError("offline")
        for m in self.history:
            yield m

    async def log_out(self):
        return True


def run(coro):
    return asyncio.run(coro)


class DemoFlowTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    async def _activate(self, be: DemoBackend):
        self.assertEqual(be.access_state(), b.AccessState.NEEDS_ACTIVATION)
        url = be.request_url()
        self.assertTrue(url.startswith("https://t.me/") and "?start=a" in url)
        code = None
        for _ in range(10):
            code = await be.poll_activation()
            if code:
                break
        self.assertIsNotNone(code)
        self.assertFalse(be.try_activate("no code in this text"))
        self.assertTrue(be.try_activate(f"copied: {code}"))
        self.assertEqual(be.access_state(), b.AccessState.ACTIVE)

    def test_full_flow_with_topic_export(self):
        async def flow():
            be = DemoBackend(home=self.dir / "home", fast=True)
            await self._activate(be)
            await be.connect()
            self.assertFalse(await be.is_authorized())
            self.assertTrue((await be.qr_start()).startswith("tg://login?token="))
            self.assertEqual(await be.qr_wait(5), "ok")
            await be.verify_access()
            acct = await be.account()
            self.assertTrue(acct.name)
            chats = await be.chats()
            forum = next(c for c in chats if c.forum)
            self.assertTrue(any(c.protected for c in chats))

            res = await be.resolve("https://t.me/+demoinvite")
            self.assertTrue(res.needs_join)
            self.assertIsNotNone(res.invite_title)
            joined = await be.join("https://t.me/+demoinvite")
            self.assertTrue(joined.title)

            res = await be.resolve(f"https://t.me/c/{abs(forum.id) - 10**12}/3/77")
            self.assertEqual((res.chat.id, res.topic_id), (forum.id, 3))
            topics = await be.topics(forum.id)
            self.assertIn(3, [t.id for t in topics])
            self.assertTrue(all(t.title for t in topics))

            stages = []
            opts = b.ExportOptions(folder=self.dir / "out", media=True)
            result = await be.export(forum.id, 3, opts, stages.append)
            self.assertTrue(result.html and result.html.exists())
            self.assertGreater(result.messages, 0)
            self.assertIn("topic", result.folder.name)
            self.assertEqual(stages[-1].stage, "done")
            self.assertTrue({"messages", "media", "files"} <= {s.stage for s in stages})

            again = await be.export(forum.id, 3, opts, lambda _: None)
            self.assertEqual(again.messages, result.messages)          # resume: no duplicates
            await be.logout()
            self.assertFalse(await be.is_authorized())
        run(flow())

    def test_cancel_keeps_archive_consistent_and_resumes(self):
        async def flow():
            be = DemoBackend(home=self.dir / "home", fast=True)
            await self._activate(be)
            await be.connect()
            await be.qr_start()
            await be.qr_wait(5)
            big = next(c for c in await be.chats() if c.title.startswith("Big"))
            opts = b.ExportOptions(folder=self.dir / "out", media=False)
            started = asyncio.Event()

            def on_progress(pr):
                if pr.stage == "messages" and pr.done > 0:
                    started.set()

            task = asyncio.create_task(be.export(big.id, None, opts, on_progress))
            await started.wait()
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
            result = await be.export(big.id, None, opts, lambda _: None)
            self.assertEqual(result.messages, be.demo_message_count(big.id))
        run(flow())

    def test_phone_login_and_errors(self):
        async def flow():
            be = DemoBackend(home=self.dir / "home", fast=True)
            await self._activate(be)
            await be.connect()
            with self.assertRaises(b.UserFacingError):
                await be.send_code("123")
            await be.send_code("+15550001111")
            with self.assertRaises(b.UserFacingError):
                await be.sign_in_code("00000")
            self.assertEqual(await be.sign_in_code("12345"), "password")
            with self.assertRaises(b.UserFacingError):
                await be.sign_in_password("wrong")
            await be.sign_in_password("demo")
            self.assertTrue(await be.is_authorized())
        run(flow())


class AccessRuleTests(unittest.TestCase):
    def setUp(self):
        self.owner, self.dev = p.new_owner_key(), p.new_device_key()
        self.grant = p.Grant(1, API_HASH, 42, NOW, NOW + 14 * 86400)

    def check(self, me=42, now=NOW + 10, texts=()):
        return b.evaluate_access(self.grant, me, now, list(texts), self.dev.public, self.owner.verify_key)

    def test_rules(self):
        self.assertIsNone(self.check())
        self.assertEqual(self.check(me=43), "wrong_account")
        self.assertEqual(self.check(now=NOW + 15 * 86400), "expired")
        self.assertEqual(self.check(texts=[p.issue_revoke(self.owner, self.dev.public, NOW + 5)]), "revoked")
        self.assertIsNone(self.check(texts=[p.issue_revoke(self.owner, self.dev.public, NOW - 5)]))   # older grant
        other = p.new_owner_key()
        self.assertIsNone(self.check(texts=[p.issue_revoke(other, self.dev.public, NOW + 5)]))      # forged

    def test_error_messages(self):
        self.assertIn("10 minutes", b.friendly_error(errors.FloodWaitError(request=None, capture=600)))
        self.assertIn("code", b.friendly_error(errors.PhoneCodeInvalidError(request=None)).lower())
        self.assertIn("password", b.friendly_error(errors.PasswordHashInvalidError(request=None)).lower())


class RealBackendOfflineTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name)
        self.owner = p.new_owner_key()
        self.cfg = BuildConfig(bot_username="my_archiver_bot", bot_id=999,
                               owner_verify_key=self.owner.verify_key)

    def tearDown(self):
        self.tmp.cleanup()

    def _backend(self):
        return TelethonBackend(home=self.home, config=self.cfg)

    def test_activation_and_read_only_client(self):
        be = self._backend()
        self.assertEqual(be.access_state(), b.AccessState.NEEDS_ACTIVATION)
        with self.assertRaises(b.ActivationError):
            be.make_client()
        url = be.request_url()
        self.assertTrue(url.startswith("https://t.me/my_archiver_bot?start=a"))
        dev_pub, _ = p.parse_start_payload(url.split("start=")[1])

        self.assertEqual(self._backend().request_url(), url)                    # device key is persistent

        stranger = p.issue_code(p.new_owner_key(), dev_pub, p.Grant(1, API_HASH, 42, NOW, NOW + 99))
        with self.assertRaises(b.ActivationError):
            be.try_activate(stranger)

        code = p.issue_code(self.owner, dev_pub, p.Grant(7654321, API_HASH, 42, NOW, NOW + 14 * 86400))
        self.assertTrue(be.try_activate(code))
        self.assertEqual(self._backend().access_state(), b.AccessState.ACTIVE)
        raw = (self.home / "vault.bin").read_bytes()
        from tg_export import vault as vault_mod
        if vault_mod.ENCRYPTED:
            self.assertNotIn(API_HASH.encode(), raw)
            self.assertNotIn(b"7654321", raw)

        client = self._backend().make_client()
        self.assertIsInstance(client, ReadOnlyClient)
        self.assertFalse(client._allow_join)

    def _active_backend(self, user_id=42, issued=None):
        be = self._backend()
        dev_pub, _ = p.parse_start_payload(be.request_url().split("start=")[1])
        issued = issued or int(time.time()) - 3600
        be.try_activate(p.issue_code(self.owner, dev_pub, p.Grant(1, API_HASH, user_id, issued, issued + 14 * 86400)))
        return be, dev_pub, issued

    def test_revoke_found_behind_many_user_messages_and_fail_closed(self):
        be, dev_pub, issued = self._active_backend()
        revoke = p.issue_revoke(self.owner, dev_pub, issued + 60)
        history = [_FakeMsg(999, f"spam {i}", issued + 600 + i) for i in range(50)]     # newest first
        history.append(_FakeMsg(self.cfg.bot_id, "Access revoked. " + revoke, issued + 60))
        be._client = _FakeClient(me_id=42, history=history)
        with self.assertRaises(b.AccessDenied) as ctx:
            run(be.verify_access())
        self.assertEqual(ctx.exception.reason, "revoked")
        self.assertEqual(be.access_state(), b.AccessState.NEEDS_ACTIVATION)          # grant dropped

        be, dev_pub, issued = self._active_backend()
        be._client = _FakeClient(me_id=42, history=[], fail_history=True)
        with self.assertRaises(b.UserFacingError) as ctx:
            run(be.verify_access())
        self.assertNotIsInstance(ctx.exception, b.AccessDenied)                      # can't verify -> no access
        self.assertEqual(be.access_state(), b.AccessState.ACTIVE)                    # but keys are kept

    def test_old_revoke_does_not_apply_to_new_grant(self):
        be, dev_pub, issued = self._active_backend()
        old = p.issue_revoke(self.owner, dev_pub, issued - 100)
        be._client = _FakeClient(me_id=42, history=[_FakeMsg(self.cfg.bot_id, old, issued - 100)])
        run(be.verify_access())

    def test_login_reconnects_after_logout(self):
        be, _, _ = self._active_backend()
        made = []

        class Conn:
            def __init__(self):
                self.connected = False
                self.session = StringSession()

            def is_connected(self):
                return self.connected

            async def connect(self):
                self.connected = True

            async def log_out(self):
                self.connected = False
                return True

            async def qr_login(self):
                class QR:
                    url = "tg://login?token=x"
                return QR()

            async def send_code_request(self, phone):
                return True

        def factory():
            made.append(Conn())
            return made[-1]

        be.make_client = factory
        run(be.connect())
        run(be.logout())                                   # e.g. the wrong account signed in
        self.assertEqual(run(be.qr_start()), "tg://login?token=x")   # used to fail with "Not connected."
        self.assertEqual(len(made), 2)
        run(be.logout())
        run(be.send_code("+380501234567"))
        self.assertEqual(len(made), 3)

    def test_without_build_config_is_unavailable(self):
        be = TelethonBackend(home=self.home, config=None)
        self.assertEqual(be.access_state(), b.AccessState.NOT_CONFIGURED)


if __name__ == "__main__":
    unittest.main()
