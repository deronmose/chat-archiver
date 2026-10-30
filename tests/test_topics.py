"""Forum topics (K6): link parsing, topic detection, archive, export, topic-scoped dump."""
from __future__ import annotations

import asyncio
import csv
import sqlite3
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from telethon import types

from tg_export import exporters
from tg_export.refs import parse_chat_ref
from tg_export.scraper import dump_messages, message_to_row, topic_of
from tg_export.store import Archive


def _msg(mid, text="", **kw):
    return types.Message(id=mid, peer_id=types.PeerChannel(1),
                         date=datetime(2026, 1, 1, tzinfo=timezone.utc), message=text, **kw)


def _in_topic(mid, topic, text="x"):
    return _msg(mid, text, reply_to=types.MessageReplyHeader(forum_topic=True, reply_to_msg_id=topic))


class RefTopicTests(unittest.TestCase):
    def test_topic_hints(self):
        r = parse_chat_ref("https://t.me/c/1597139842/55/120")
        self.assertEqual((r.kind, r.value, r.topic_hint, r.post_id), ("id", -1001597139842, 55, 120))
        r = parse_chat_ref("https://t.me/c/1597139842/55")
        self.assertEqual((r.topic_hint, r.post_id), (None, 55))
        r = parse_chat_ref("t.me/some_group/7/88")
        self.assertEqual((r.kind, r.value, r.topic_hint), ("username", "some_group", 7))
        r = parse_chat_ref("https://t.me/some_group/88?thread=7")
        self.assertEqual((r.topic_hint, r.post_id), (7, 88))
        self.assertIsNone(parse_chat_ref("@some_group").topic_hint)


class TopicOfTests(unittest.TestCase):
    def test_topic_detection(self):
        self.assertEqual(topic_of(_in_topic(10, 5), forum=True), 5)
        top = _msg(11, reply_to=types.MessageReplyHeader(forum_topic=True, reply_to_msg_id=12, reply_to_top_id=5))
        self.assertEqual(topic_of(top, forum=True), 5)
        self.assertEqual(topic_of(_msg(12), forum=True), 1)                       # General
        created = types.MessageService(id=5, peer_id=types.PeerChannel(1), date=datetime(2026, 1, 1, tzinfo=timezone.utc),
                                       action=types.MessageActionTopicCreate(title="Ideas", icon_color=0))
        self.assertEqual(topic_of(created, forum=True), 5)
        self.assertIsNone(topic_of(_in_topic(10, 5), forum=False))
        self.assertEqual(message_to_row(_in_topic(10, 5), forum=True)["topic_id"], 5)


class ArchiveTopicTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_migrates_old_archive(self):
        db = self.dir / "messages.db"
        con = sqlite3.connect(db)
        con.execute("CREATE TABLE messages (id INTEGER PRIMARY KEY, date TEXT NOT NULL, text TEXT)")
        con.execute("INSERT INTO messages (id, date, text) VALUES (1, '2026-01-01', 'old')")
        con.commit()
        con.close()
        with Archive(db) as a:
            a.upsert([message_to_row(_in_topic(2, 5), forum=True)])
            rows = {r["id"]: r for r in a.rows()}
        self.assertEqual(rows[1]["text"], "old")
        self.assertEqual(rows[2]["topic_id"], 5)

    def test_export_has_topic_columns(self):
        with Archive(self.dir / "messages.db") as a:
            a.set_topics({1: "General", 5: "Ideas"})
            a.set_meta(chat_id=-100, title="Forum", exported_at="now", forum=1)
            a.upsert([message_to_row(_msg(1, "hello"), forum=True),
                      message_to_row(_in_topic(6, 5, "idea!"), forum=True)])
            self.assertEqual(a.topics(), {1: "General", 5: "Ideas"})
            exporters.export_all(a, self.dir, ["csv", "html", "json"])
        with open(self.dir / "messages.csv", encoding="utf-8-sig", newline="") as f:
            rows = list(csv.DictReader(f))
        self.assertEqual([(r["topic_id"], r["topic"]) for r in rows], [("1", "General"), ("5", "Ideas")])
        page = (self.dir / "chat.html").read_text("utf-8")
        self.assertIn("Ideas", page)


class FakeClient:
    def __init__(self, messages):
        self.messages = messages
        self.calls = []

    async def get_messages(self, entity, limit=None, **kw):
        class R(list):
            total = len(self.messages)
        return R()

    async def iter_messages(self, entity, **kw):
        self.calls.append(kw)
        for m in self.messages:
            if kw.get("reply_to") is not None and topic_of(m, forum=True) != kw["reply_to"]:
                continue
            if m.id > (kw.get("min_id") or 0):
                yield m


class DumpTopicTests(unittest.TestCase):
    def test_single_topic_and_general(self):
        msgs = [_msg(1, "general"), _in_topic(2, 5), _in_topic(3, 5), _in_topic(4, 9), _msg(6, "general 2")]
        with tempfile.TemporaryDirectory() as d:
            client = FakeClient(msgs)
            seen = []
            with Archive(Path(d) / "t5.db") as a:
                asyncio.run(dump_messages(client, object(), a, limit=None, since=None, include_raw=False,
                                          topic_id=5, forum=True, on_progress=lambda done, total: seen.append(done)))
                self.assertEqual([r["id"] for r in a.rows()], [2, 3])
            self.assertEqual(client.calls[0].get("reply_to"), 5)
            self.assertTrue(seen)
            with Archive(Path(d) / "t1.db") as a:
                asyncio.run(dump_messages(FakeClient(msgs), object(), a, limit=None, since=None, include_raw=False,
                                          topic_id=1, forum=True))
                self.assertEqual([r["id"] for r in a.rows()], [1, 6])


if __name__ == "__main__":
    unittest.main()
