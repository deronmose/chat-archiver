"""Admin adopts a prepared encrypted admin.bin placed next to the exe, but never overwrites a configured one."""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from chat_archiver_admin.__main__ import IMPORT_NAME, import_prepared_data
from tg_export.vault import Vault

PREPARED = {"bot_token": "123456789:x", "owner_private": "ab" * 32, "bot_username": "b", "api_id": 1}


class ImportTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name) / "dist"
        self.base.mkdir()
        self.target = Path(self.tmp.name) / "appdata" / "admin.bin"

    def tearDown(self):
        self.tmp.cleanup()

    def test_imports_into_empty_and_removes_file(self):
        Vault(self.base / IMPORT_NAME).save(PREPARED)
        self.assertTrue(import_prepared_data(self.target, self.base))
        self.assertEqual(Vault(self.target).load(), PREPARED)
        self.assertFalse((self.base / IMPORT_NAME).exists())

    def test_replaces_unconfigured_vault(self):
        Vault(self.target).save({"theme": "dark"})
        Vault(self.base / IMPORT_NAME).save(PREPARED)
        self.assertTrue(import_prepared_data(self.target, self.base))
        self.assertEqual(Vault(self.target).load()["bot_token"], PREPARED["bot_token"])

    def test_never_overwrites_configured_admin(self):
        mine = dict(PREPARED, bot_token="999999999:mine")
        Vault(self.target).save(mine)
        Vault(self.base / IMPORT_NAME).save(PREPARED)
        self.assertFalse(import_prepared_data(self.target, self.base))
        self.assertEqual(Vault(self.target).load()["bot_token"], "999999999:mine")

    def test_ignores_junk(self):
        (self.base / IMPORT_NAME).write_bytes(b"not a vault")
        self.assertFalse(import_prepared_data(self.target, self.base))
        self.assertFalse(self.target.exists())


if __name__ == "__main__":
    unittest.main()
