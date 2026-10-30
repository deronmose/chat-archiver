"""Offline tests: no network, no Telegram account needed."""
from __future__ import annotations

import asyncio
import csv
import json
import os
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from telethon import types
from telethon.tl import functions

from tg_export import exporters, guard, vault
from tg_export.refs import parse_chat_ref
from tg_export.scraper import (chat_folder_name, media_filename, message_to_row,
                               resolve_chat, sanitize_filename, UserError)
from tg_export.store import Archive


class GuardTests(unittest.TestCase):
    def test_allowlist_names_exist_in_telethon(self):
        for name in guard.ALLOWED | guard.JOIN_REQUESTS | guard.DENIED_GETTERS:
            ns, cls = name.split(".")
            self.assertTrue(hasattr(getattr(functions, ns), cls), name)

    def test_reading_is_allowed(self):
        guard.check_request(functions.messages.GetHistoryRequest(
            peer=types.InputPeerEmpty(), offset_id=0, offset_date=None, add_offset=0,
            limit=1, max_id=0, min_id=0, hash=0))
        guard.check_request(functions.upload.GetFileRequest(
            location=types.InputPeerPhotoFileLocation(peer=types.InputPeerEmpty(), photo_id=1), offset=0, limit=1024))

    def test_writes_are_blocked(self):
        peer = types.InputPeerEmpty()
        dangerous = [
            functions.messages.SendMessageRequest(peer=peer, message="x"),
            functions.messages.DeleteMessagesRequest(id=[1]),
            functions.messages.ReadHistoryRequest(peer=peer, max_id=0),
            functions.messages.SendReactionRequest(peer=peer, msg_id=1),
            functions.channels.LeaveChannelRequest(channel=types.InputChannelEmpty()),
            functions.channels.JoinChannelRequest(channel=types.InputChannelEmpty()),
            functions.account.UpdateProfileRequest(first_name="x"),
            functions.messages.GetMessagesViewsRequest(peer=peer, id=[1], increment=True),
            functions.messages.GetBotCallbackAnswerRequest(peer=peer, msg_id=1),
        ]
        for req in dangerous:
            with self.assertRaises(guard.BlockedRequestError, msg=guard.request_name(req)):
                guard.check_request(req)

    def test_join_needs_flag(self):
        req = functions.messages.ImportChatInviteRequest(hash="abc")
        with self.assertRaises(guard.BlockedRequestError):
            guard.check_request(req)
        guard.check_request(req, allow_join=True)

    def test_client_call_is_guarded(self):
        client = guard.ReadOnlyClient(None, 1, "0" * 32)
        req = functions.messages.SendMessageRequest(peer=types.InputPeerEmpty(), message="x")
        with self.assertRaises(guard.BlockedRequestError):
            asyncio.run(client._call(client._sender, req))
        with self.assertRaises(guard.BlockedRequestError):
            asyncio.run(client._call(client._sender, [req]))


class RefTests(unittest.TestCase):
    def test_parse(self):
        cases = {
            "https://t.me/+AbC_d-12": ("invite", "AbC_d-12"),
            "t.me/joinchat/XyZ123": ("invite", "XyZ123"),
            "tg://join?invite=Hash1": ("invite", "Hash1"),
            "https://t.me/durov": ("username", "durov"),
            "https://t.me/s/durov": ("username", "durov"),
            "t.me/some_channel/1234": ("username", "some_channel"),
            "@some_group": ("username", "some_group"),
            "some_group": ("username", "some_group"),
            "https://t.me/c/1597139842/55": ("id", -1001597139842),
            "-1001597139842": ("id", -1001597139842),
            "123456": ("id", 123456),
        }
        for text, (kind, value) in cases.items():
            ref = parse_chat_ref(text)
            self.assertEqual((ref.kind, ref.value), (kind, value), text)

    def test_rejects_garbage(self):
        for bad in ("", "http://evil.com/+x", "t.me/+", "a b c", "../../etc"):
            with self.assertRaises(ValueError, msg=bad):
                parse_chat_ref(bad)


class FilenameTests(unittest.TestCase):
    def test_sanitize(self):
        self.assertEqual(sanitize_filename("../../Windows/evil.exe"), "evil.exe")
        self.assertEqual(sanitize_filename("..\\..\\x.txt"), "x.txt")
        self.assertEqual(sanitize_filename('a<b>c:"d|e?f*.txt'), "a_b_c__d_e_f_.txt")
        self.assertEqual(sanitize_filename("CON.txt"), "_CON.txt")
        self.assertEqual(sanitize_filename(" .hidden. "), "hidden")
        long = sanitize_filename("я" * 300 + ".pdf")
        self.assertTrue(long.endswith(".pdf") and len(long) <= 80)

    def test_media_filename(self):
        self.assertEqual(media_filename(7, None, ".jpg"), "7.jpg")
        self.assertEqual(media_filename(7, "report.pdf", ".pdf"), "7_report.pdf")
        self.assertEqual(media_filename(7, "../x", ".bin"), "7_x.bin")
        self.assertEqual(media_filename(7, None, None), "7.bin")
        self.assertEqual(chat_folder_name("Моя/группа: тест", -1001), "Моя_группа_ тест_1001")


def _msg(mid, text="", **kw):
    m = types.Message(id=mid, peer_id=types.PeerChannel(1),
                      date=datetime(2026, 1, 2, 3, 4, 5, tzinfo=timezone.utc), message=text, **kw)
    return m


class RowTests(unittest.TestCase):
    def test_message_to_row(self):
        m = _msg(5, "привет", reply_to=types.MessageReplyHeader(reply_to_msg_id=3), views=10)
        m._sender = types.User(id=42, first_name="Ann", last_name="Lee", username="ann")
        m._sender_id = 42
        row = message_to_row(m, include_raw=True)
        self.assertEqual(row["id"], 5)
        self.assertEqual(row["date"], "2026-01-02T03:04:05+00:00")
        self.assertEqual(row["text"], "привет")
        self.assertEqual(row["reply_to"], 3)
        self.assertEqual(row["sender_name"], "Ann Lee")
        self.assertEqual(row["sender_username"], "ann")
        self.assertEqual(row["views"], 10)
        self.assertIsNone(row["media_type"])
        self.assertEqual(json.loads(row["raw"])["id"], 5)

    def test_service_and_media(self):
        s = types.MessageService(id=9, peer_id=types.PeerChannel(1),
                                 date=datetime(2026, 1, 1, tzinfo=timezone.utc),
                                 action=types.MessageActionChatJoinedByLink(inviter_id=1))
        self.assertEqual(message_to_row(s)["service"], "MessageActionChatJoinedByLink")
        doc = types.Document(id=1, access_hash=1, file_reference=b"", date=None, mime_type="application/pdf",
                             size=1234, dc_id=2, attributes=[types.DocumentAttributeFilename("a.pdf")])
        m = _msg(10, media=types.MessageMediaDocument(document=doc))
        row = message_to_row(m)
        self.assertEqual((row["media_type"], row["media_name"], row["media_size"]),
                         ("MessageMediaDocument", "a.pdf", 1234))


class ArchiveExportTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def _rows(self):
        return [
            message_to_row(_msg(1, "=HYPERLINK(\"http://evil\")")),
            message_to_row(_msg(2, "<script>alert(1)</script>", reply_to=types.MessageReplyHeader(reply_to_msg_id=1))),
            message_to_row(_msg(3, "обычный текст")),
        ]

    def test_resume_and_upsert(self):
        with Archive(self.dir / "messages.db") as a:
            self.assertEqual(a.last_id(), 0)
            a.upsert(self._rows())
            self.assertEqual((a.last_id(), a.count()), (3, 3))
            a.set_media(3, "media/3.jpg")
            edited = message_to_row(_msg(3, "исправлено"))
            a.upsert([edited])                       # re-scan must not erase media_path
            row = [r for r in a.rows() if r["id"] == 3][0]
            self.assertEqual((row["text"], row["media_path"]), ("исправлено", "media/3.jpg"))

    def test_pending_media(self):
        doc = types.Document(id=1, access_hash=1, file_reference=b"", date=None, mime_type="x",
                             size=1, dc_id=2, attributes=[])
        with Archive(self.dir / "messages.db") as a:
            a.upsert([message_to_row(_msg(1, media=types.MessageMediaDocument(document=doc))),
                      message_to_row(_msg(2, media=types.MessageMediaWebPage(webpage=types.WebPageEmpty(id=1))))])
            self.assertEqual(a.pending_media(), [1])
            a.set_media(1, None, "boom")
            self.assertEqual(a.pending_media(), [])
            self.assertEqual(a.pending_media(retry_failed=True), [1])

    def test_exports_are_safe(self):
        with Archive(self.dir / "messages.db") as a:
            a.upsert(self._rows())
            a.set_meta(chat_id=-100, title="<b>Чат</b>", exported_at="now")
            results = exporters.export_all(a, self.dir, ["json", "csv", "html"])
        self.assertEqual([n for _, n in results], [3, 3, 3])

        data = json.loads((self.dir / "messages.json").read_text("utf-8"))
        self.assertEqual(data[2]["text"], "обычный текст")
        self.assertNotIn("raw", data[0])

        with open(self.dir / "messages.csv", encoding="utf-8-sig", newline="") as f:
            rows = list(csv.DictReader(f))
        self.assertTrue(rows[0]["text"].startswith("'="))      # formula neutralised

        page = (self.dir / "chat.html").read_text("utf-8")
        self.assertNotIn("<script>", page)
        self.assertIn("&lt;script&gt;", page)
        self.assertIn("&lt;b&gt;Чат&lt;/b&gt;", page)
        self.assertIn("default-src 'none'", page)
        self.assertFalse(list(self.dir.glob("*.tmp")))


class ResolveTests(unittest.TestCase):
    def test_invite_without_membership_requires_join(self):
        class FakeClient:
            async def __call__(self, request):
                assert guard.request_name(request) == "messages.CheckChatInviteRequest"
                return types.ChatInvite(title="Секретная", photo=types.PhotoEmpty(id=0),
                                        participants_count=5, color=0)
        with self.assertRaises(UserError):
            asyncio.run(resolve_chat(FakeClient(), parse_chat_ref("t.me/+abc"), allow_join=False))

    def test_invite_already_member(self):
        chat = types.Channel(id=1, title="G", photo=types.ChatPhotoEmpty(), date=None)

        class FakeClient:
            async def __call__(self, request):
                return types.ChatInviteAlready(chat=chat)
        self.assertIs(asyncio.run(resolve_chat(FakeClient(), parse_chat_ref("t.me/+abc"), False)), chat)


class VaultTests(unittest.TestCase):
    def test_roundtrip_is_encrypted(self):
        with tempfile.TemporaryDirectory() as d:
            os.environ["TG_EXPORT_HOME"] = d
            try:
                secret = {"api_id": 1, "api_hash": "f" * 32, "session": "1SECRETSESSION"}
                vault.save(secret)
                raw = vault.vault_path().read_bytes()
                if vault.ENCRYPTED:
                    self.assertNotIn(b"SECRETSESSION", raw)
                    self.assertNotIn(b"ffffffff", raw)
                self.assertEqual(vault.load(), secret)
                self.assertTrue(vault.delete())
                self.assertEqual(vault.load(), {})
            finally:
                del os.environ["TG_EXPORT_HOME"]


if __name__ == "__main__":
    unittest.main()
