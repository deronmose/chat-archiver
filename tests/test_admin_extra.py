"""Extra tests for Chat Archiver Admin: backups, build config, Bot API hygiene, listen loop, demo bot."""
from __future__ import annotations

import asyncio
import io
import json
import logging
import tempfile
import traceback
import unittest
from pathlib import Path

import httpx

from approval import protocol as p
from tg_export.vault import Vault

from chat_archiver_admin import botapi as botapi_mod
from chat_archiver_admin.botapi import BotApi, BotApiError, redact
from chat_archiver_admin.demo import DEMO_API_HASH, DEMO_API_ID, DEMO_TOKEN, DemoBot
from chat_archiver_admin.service import AdminError, AdminService, listen_loop

NOW = 1_800_000_000
API_HASH = "0123456789abcdef0123456789abcdef"
TOKEN = "123456789:AAH-secret-token-value_xyz0123456"


class FakeBot:
    def __init__(self):
        self.updates: list[dict] = []
        self.sent: list[dict] = []
        self.fail_send = False
        self.fail_updates: list[Exception] = []
        self.me = {"id": 999, "is_bot": True, "username": "my_archiver_bot"}

    async def get_me(self):
        return self.me

    async def delete_webhook(self):
        return True

    async def get_updates(self, offset=None, timeout=0):
        if self.fail_updates:
            raise self.fail_updates.pop(0)
        return [u for u in self.updates if offset is None or u["update_id"] >= offset]

    async def send_message(self, chat_id, text, reply_markup=None):
        if self.fail_send:
            raise BotApiError("Telegram error 400: chat not found", error_code=400)
        self.sent.append({"chat_id": chat_id, "text": text, "reply_markup": reply_markup})
        return {"message_id": len(self.sent)}


def start(update_id, user_id, text, first="Ann", username="ann"):
    return {"update_id": update_id, "message": {
        "message_id": update_id, "date": NOW,
        "from": {"id": user_id, "is_bot": False, "first_name": first, "username": username},
        "chat": {"id": user_id, "type": "private"}, "text": text}}


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.vault = Vault(Path(self.tmp.name) / "admin.bin")
        self.bot = FakeBot()
        self.svc = self.make()
        self.device = p.new_device_key()
        self.payload = p.make_start_payload(self.device.public, "DESKTOP-ANN")

    def tearDown(self):
        self.tmp.cleanup()

    def make(self):
        return AdminService(vault=self.vault, bot_factory=lambda t: self.bot, clock=lambda: NOW)

    def run_(self, coro):
        return asyncio.run(coro)

    def setup_svc(self):
        self.run_(self.svc.setup(bot_token=TOKEN, api_id=1234567, api_hash=API_HASH))

    def one_request(self, user_id=42, first="Ann"):
        self.bot.updates = [start(1, user_id, f"/start {self.payload}", first=first)]
        return self.run_(self.svc.poll_once())[0]

    def verify_key(self):
        return p._b64d(self.svc.build_config()["owner_verify_key"])


class OwnerKeyTests(Base):
    def test_backup_round_trip_restores_same_key(self):
        self.setup_svc()
        vk = self.svc.build_config()["owner_verify_key"]
        backup = self.svc.export_owner_backup("correct horse battery")
        self.assertTrue(backup.startswith("CAOK1:"))
        # Another Admin (new machine) restores it and signs codes the old build accepts.
        other = AdminService(Vault(Path(self.tmp.name) / "other.bin"), lambda t: self.bot, lambda: NOW)
        other.import_owner_backup(backup, "correct horse battery")
        self.run_(other.setup(TOKEN, 1234567, API_HASH))
        self.assertEqual(other.build_config()["owner_verify_key"], vk)

    def test_backup_wrong_password_and_garbage(self):
        self.setup_svc()
        backup = self.svc.export_owner_backup("correct horse battery")
        with self.assertRaises(AdminError):
            self.svc.import_owner_backup(backup, "wrong password!")
        with self.assertRaises(AdminError):
            self.svc.import_owner_backup("hello", "whatever123")
        with self.assertRaises(AdminError):
            self.svc.import_owner_backup(backup[:-4], "correct horse battery")
        with self.assertRaises(AdminError):
            self.svc.export_owner_backup("short")

    def test_backup_contains_no_plain_key_and_tolerates_whitespace(self):
        self.setup_svc()
        backup = self.svc.export_owner_backup("correct horse battery")
        self.assertNotIn(self.svc._state["owner_private"], backup)
        wrapped = "\n".join(backup[i:i + 40] for i in range(0, len(backup), 40)) + "\n"
        self.svc.import_owner_backup(wrapped, "correct horse battery")

    def test_repeated_setup_keeps_owner_key(self):
        self.setup_svc()
        vk = self.svc.build_config()["owner_verify_key"]
        self.run_(self.svc.setup(bot_token=TOKEN, api_id=7654321, api_hash=API_HASH.upper()))
        self.assertEqual(self.svc.build_config()["owner_verify_key"], vk)
        self.assertEqual(self.svc.api_id, 7654321)

    def test_export_build_config_writes_public_json_only(self):
        self.setup_svc()
        out = Path(self.tmp.name) / "build" / "approval.json"
        self.assertEqual(self.svc.export_build_config(out), out)
        data = json.loads(out.read_text(encoding="utf-8"))
        self.assertEqual(set(data), {"bot_username", "bot_id", "owner_verify_key"})
        raw = out.read_text(encoding="utf-8")
        for secret in (TOKEN, API_HASH, "1234567"):
            self.assertNotIn(secret, raw)

    def test_build_config_requires_setup(self):
        with self.assertRaises(AdminError):
            self.svc.build_config()

    def test_validation_messages(self):
        for api_id in (0, -5, "abc", ""):
            with self.assertRaises(AdminError):
                AdminService.validate_api_keys(api_id, API_HASH)
        for h in ("", "0123", API_HASH + "0", "g" * 32):
            with self.assertRaises(AdminError):
                AdminService.validate_api_keys(1, h)
        self.assertEqual(AdminService.validate_api_keys(" 42 ", API_HASH.upper()), (42, API_HASH))


class FlowTests(Base):
    def test_received_reply_and_approve_message_format(self):
        self.setup_svc()
        req = self.one_request()
        self.assertIn("Request received", self.bot.sent[-1]["text"])
        self.assertIn("waiting for approval", self.bot.sent[-1]["text"])
        self.run_(self.svc.approve(req.id))
        msg = self.bot.sent[-1]
        btn = msg["reply_markup"]["inline_keyboard"][0][0]
        self.assertEqual(btn["text"], "📋 Copy activation code")
        code = btn["copy_text"]["text"]
        self.assertEqual(p.find_code(msg["text"]), code)            # fallback: code in the text too
        self.assertIn(f"<code>{code}</code>", msg["text"])
        self.assertLessEqual(len(code), 256)

    def test_names_are_html_escaped(self):
        self.setup_svc()
        self.bot.updates = [start(1, 42, f"/start {self.payload}", first="<b>Eve</b>")]
        req = self.run_(self.svc.poll_once())[0]
        self.assertEqual(req.name, "<b>Eve</b>")
        for m in self.bot.sent:
            self.assertNotIn("<b>Eve</b>", m["text"])

    def test_failed_send_keeps_request_pending(self):
        self.setup_svc()
        req = self.one_request()
        self.bot.fail_send = True
        with self.assertRaises(AdminError):
            self.run_(self.svc.approve(req.id))
        self.assertEqual([r.id for r in self.svc.pending()], [req.id])
        self.assertEqual(self.svc.approved(), [])
        self.bot.fail_send = False
        self.run_(self.svc.approve(req.id))
        with self.assertRaises(AdminError):                          # already handled
            self.run_(self.svc.approve(req.id))

    def test_revoke_twice_and_unknown_ids(self):
        self.setup_svc()
        appr = self.run_(self.svc.approve(self.one_request().id))
        self.run_(self.svc.revoke(appr.id))
        with self.assertRaises(AdminError):
            self.run_(self.svc.revoke(appr.id))
        with self.assertRaises(AdminError):
            self.run_(self.svc.deny("req_nope"))

    def test_bare_start_gets_hint_and_no_request(self):
        self.setup_svc()
        self.bot.updates = [start(1, 50, "/start"), start(2, 51, "/start@my_archiver_bot")]
        self.assertEqual(self.run_(self.svc.poll_once()), [])
        self.assertEqual([m["chat_id"] for m in self.bot.sent], [50, 51])
        self.assertIn("Request access", self.bot.sent[0]["text"])

    def test_same_user_other_device_is_separate_request(self):
        self.setup_svc()
        other = p.make_start_payload(p.new_device_key().public, "LAPTOP")
        self.bot.updates = [start(1, 42, f"/start {self.payload}"), start(2, 42, f"/start {other}")]
        self.assertEqual(len(self.run_(self.svc.poll_once())), 2)

    def test_spam_guard_per_user(self):
        self.setup_svc()
        self.bot.updates = [start(i, 42, "/start " + p.make_start_payload(p.new_device_key().public, f"PC{i}"))
                            for i in range(1, 8)]
        self.run_(self.svc.poll_once())
        self.assertEqual(len(self.svc.pending()), 3)
        self.assertEqual(self.svc.dismiss_all_pending(), 3)
        self.assertEqual(self.svc.pending(), [])

    def test_redelivered_updates_are_not_processed_twice(self):
        self.setup_svc()
        self.bot.updates = [start(5, 42, f"/start {self.payload}")]
        self.run_(self.svc.poll_once())
        self.run_(self.svc.deny(self.svc.pending()[0].id))
        # Telegram (or a racing poll) hands back an update we already processed.
        self.svc._state["offset"] = 6
        self.bot.updates = [start(5, 42, f"/start {self.payload}"), start(6, 43, "hi")]

        async def stale(offset=None, timeout=0):
            return list(self.bot.updates)
        self.bot.get_updates = stale
        self.assertEqual(self.run_(self.svc.poll_once()), [])
        self.assertEqual(self.svc._state["offset"], 7)

    def test_update_settings_keeps_unchanged_values(self):
        self.setup_svc()
        self.run_(self.svc.update_settings(bot_token="", api_id="555", api_hash=""))
        self.assertEqual(self.svc.api_id, 555)
        self.assertEqual(self.svc._state["api_hash"], API_HASH)
        self.assertEqual(self.svc._state["bot_token"], TOKEN)

    def test_new_bot_resets_offset(self):
        self.setup_svc()
        self.one_request()
        self.assertEqual(self.svc._state["offset"], 2)
        self.bot.me = {"id": 1000, "is_bot": True, "username": "another_bot"}
        self.run_(self.svc.update_settings(bot_token="987654321:AAother-token-value_123"))
        self.assertIsNone(self.svc._state["offset"])
        self.assertEqual(self.svc.bot_username, "another_bot")

    def test_reply_failure_is_logged_not_raised(self):
        self.setup_svc()
        self.bot.fail_send = True
        self.bot.updates = [start(1, 42, f"/start {self.payload}")]
        self.assertEqual(len(self.run_(self.svc.poll_once())), 1)
        self.assertEqual(self.svc.history()[0].kind, "error")

    def test_history_is_newest_first_and_clearable(self):
        self.setup_svc()
        self.run_(self.svc.deny(self.one_request().id))
        kinds = [e.kind for e in self.svc.history()]
        self.assertEqual(kinds[0], "denied")
        self.svc.clear_history()
        self.assertEqual(len(self.svc.history()), 1)


class ListenLoopTests(Base):
    def test_retries_after_network_error_and_reports_status(self):
        self.setup_svc()
        self.bot.fail_updates = [BotApiError("Cannot reach Telegram (ConnectError).", network=True)]
        self.bot.updates = [start(1, 42, f"/start {self.payload}")]
        statuses, got = [], []

        async def main():
            async def on_new(reqs):
                got.extend(reqs)
            task = asyncio.create_task(listen_loop(
                self.svc, lambda s, m: statuses.append((s, m)), on_new, timeout=0, retry_delays=(0,)))
            for _ in range(200):
                await asyncio.sleep(0.01)
                if got:
                    break
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task

        self.run_(main())
        self.assertEqual(len(got), 1)
        states = [s for s, _ in statuses]
        self.assertEqual(states[:2], ["starting", "listening"])
        self.assertIn("error", states)
        self.assertEqual(states[-1], "listening")
        err = next(m for s, m in statuses if s == "error")
        self.assertIn("Retrying", err)


class BotApiTests(unittest.TestCase):
    def api(self, handler):
        client = httpx.AsyncClient(base_url=botapi_mod.API_BASE, transport=httpx.MockTransport(handler))
        return BotApi(TOKEN, client=client)

    def test_calls_official_endpoint_with_html_and_markup(self):
        seen = []

        def handler(request: httpx.Request):
            seen.append(request)
            return httpx.Response(200, json={"ok": True, "result": {"message_id": 1}})

        api = self.api(handler)
        asyncio.run(api.send_message(42, "<b>x</b>", reply_markup={"inline_keyboard": []}))
        req = seen[0]
        self.assertEqual(req.url.host, "api.telegram.org")
        self.assertEqual(req.url.scheme, "https")
        self.assertTrue(req.url.path.endswith("/sendMessage"))
        body = json.loads(req.content)
        self.assertEqual(body["parse_mode"], "HTML")
        self.assertEqual(body["reply_markup"], {"inline_keyboard": []})

    def test_api_error_has_no_token(self):
        def handler(request):
            return httpx.Response(401, json={"ok": False, "error_code": 401,
                                             "description": f"Unauthorized {TOKEN}"})
        with self.assertRaises(BotApiError) as cm:
            asyncio.run(self.api(handler).get_me())
        self.assertEqual(cm.exception.error_code, 401)
        self.assertNotIn(TOKEN, str(cm.exception))
        self.assertNotIn(TOKEN, "".join(traceback.format_exception(cm.exception)))

    def test_network_error_has_no_token_anywhere(self):
        def handler(request):
            raise httpx.ConnectError(f"failed to connect to {request.url}", request=request)
        with self.assertRaises(BotApiError) as cm:
            asyncio.run(self.api(handler).get_updates(offset=5, timeout=0))
        self.assertTrue(cm.exception.network)
        text = "".join(traceback.format_exception(cm.exception))
        self.assertNotIn(TOKEN, text)
        self.assertNotIn(TOKEN.split(":")[1], text)
        self.assertIsNone(cm.exception.__cause__)
        self.assertTrue(cm.exception.__suppress_context__)

    def test_conflict_and_rate_limit_messages(self):
        def conflict(request):
            return httpx.Response(409, json={"ok": False, "error_code": 409, "description": "Conflict"})
        with self.assertRaises(BotApiError) as cm:
            asyncio.run(self.api(conflict).get_updates())
        self.assertIn("Another program", str(cm.exception))

        def flood(request):
            return httpx.Response(429, json={"ok": False, "error_code": 429, "description": "Too Many Requests",
                                             "parameters": {"retry_after": 7}})
        with self.assertRaises(BotApiError) as cm:
            asyncio.run(self.api(flood).get_updates())
        self.assertEqual(cm.exception.retry_after, 7)

    def test_repr_and_bad_token(self):
        self.assertNotIn(TOKEN, repr(BotApi(TOKEN)))
        with self.assertRaises(BotApiError):
            BotApi("not a token")
        self.assertEqual(redact(f"x bot{TOKEN}/getMe"), "x bot***/getMe")

    def test_httpx_logs_never_contain_token(self):
        buf = io.StringIO()
        handler = logging.StreamHandler(buf)
        root = logging.getLogger()
        root.addHandler(handler)
        old = root.level
        root.setLevel(logging.DEBUG)
        try:
            logging.getLogger("httpx").warning("HTTP Request: POST https://api.telegram.org/bot%s/getMe", TOKEN)

            def ok(request):
                return httpx.Response(200, json={"ok": True, "result": {"id": 1}})
            asyncio.run(self.api(ok).get_me())
        finally:
            root.removeHandler(handler)
            root.setLevel(old)
        self.assertIn("bot***", buf.getvalue())
        self.assertNotIn(TOKEN, buf.getvalue())


class DemoTests(unittest.TestCase):
    def test_demo_bot_feeds_a_working_admin(self):
        with tempfile.TemporaryDirectory() as d:
            bot = DemoBot(DEMO_TOKEN, interval=0, first_after=0, max_requests=12, seed=3)
            svc = AdminService(Vault(Path(d) / "a.bin"), lambda t: bot, lambda: NOW)

            async def flow():
                await svc.setup(DEMO_TOKEN, DEMO_API_ID, DEMO_API_HASH)
                new = await svc.poll_once()
                self.assertTrue(new)
                self.assertTrue(all(r.device_label for r in new))
                await svc.approve(new[0].id)
                if len(new) > 1:
                    await svc.deny(new[1].id)
                again = await svc.poll_once()
                self.assertEqual(again, [])
            asyncio.run(flow())

    def test_demo_bot_rejects_bad_token(self):
        bot = DemoBot("123:bad")
        with self.assertRaises(BotApiError):
            asyncio.run(bot.get_me())


if __name__ == "__main__":
    unittest.main()
