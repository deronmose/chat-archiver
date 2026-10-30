"""Contract tests for the approval / key-delivery protocol (K4)."""
from __future__ import annotations

import unittest

from approval import protocol as p

API_ID = 1234567
API_HASH = "0123456789abcdef0123456789abcdef"
NOW = 1_800_000_000


def _grant(user_id=42, issued=NOW, days=p.VALIDITY_DAYS):
    return p.Grant(api_id=API_ID, api_hash=API_HASH, user_id=user_id,
                   issued_at=issued, expires_at=issued + days * 86400)


class StartPayloadTests(unittest.TestCase):
    def test_payload_roundtrip_and_limits(self):
        dev = p.new_device_key()
        payload = p.make_start_payload(dev.public, p.device_label("DESKTOP-ÄB 12/x__very-long-hostname"))
        self.assertLessEqual(len(payload), 64)
        self.assertRegex(payload, r"^[A-Za-z0-9_-]+$")
        pub, label = p.parse_start_payload(payload)
        self.assertEqual(pub, dev.public)
        self.assertTrue(label.startswith("DESKTOP"))
        self.assertLessEqual(len(label), 20)

    def test_bad_payloads(self):
        for bad in ("", "a", "x" + "A" * 43, "a" + "!" * 43, "a" + "A" * 10):
            with self.assertRaises(p.ProtocolError, msg=bad):
                p.parse_start_payload(bad)


class CodeTests(unittest.TestCase):
    def setUp(self):
        self.owner = p.new_owner_key()
        self.dev = p.new_device_key()

    def test_roundtrip_fits_copy_button(self):
        code = p.issue_code(self.owner, self.dev.public, _grant())
        self.assertLessEqual(len(code), 256)
        self.assertTrue(code.startswith(p.CODE_PREFIX))
        g = p.open_code(code, self.dev, self.owner.verify_key)
        self.assertEqual((g.api_id, g.api_hash, g.user_id), (API_ID, API_HASH, 42))
        self.assertEqual(g.expires_at - g.issued_at, 14 * 86400)

    def test_code_for_other_device_is_useless(self):
        code = p.issue_code(self.owner, self.dev.public, _grant())
        with self.assertRaises(p.InvalidCode):
            p.open_code(code, p.new_device_key(), self.owner.verify_key)

    def test_forged_by_someone_else_is_rejected(self):
        mallory = p.new_owner_key()
        code = p.issue_code(mallory, self.dev.public, _grant())
        with self.assertRaises(p.InvalidCode):
            p.open_code(code, self.dev, self.owner.verify_key)

    def test_any_flipped_character_is_rejected(self):
        code = p.issue_code(self.owner, self.dev.public, _grant())
        for i in range(len(p.CODE_PREFIX), len(code), 7):
            ch = "A" if code[i] != "A" else "B"
            with self.assertRaises(p.InvalidCode, msg=i):
                p.open_code(code[:i] + ch + code[i + 1:], self.dev, self.owner.verify_key)

    def test_find_code_in_text(self):
        code = p.issue_code(self.owner, self.dev.public, _grant())
        self.assertEqual(p.find_code(f"Your code:\n{code}\nthanks"), code)
        self.assertIsNone(p.find_code("nothing here"))
        self.assertIsNone(p.find_code(None))

    def test_expiry(self):
        g = _grant()
        self.assertFalse(p.is_expired(g, NOW + 13 * 86400))
        self.assertTrue(p.is_expired(g, NOW + 14 * 86400 + 1))


class RevokeTests(unittest.TestCase):
    def test_revoke_is_device_bound_and_signed(self):
        owner, dev, other = p.new_owner_key(), p.new_device_key(), p.new_device_key()
        text = "Access revoked.\n" + p.issue_revoke(owner, dev.public, NOW + 5)
        self.assertEqual(p.check_revoke(text, dev.public, owner.verify_key), NOW + 5)
        self.assertIsNone(p.check_revoke(text, other.public, owner.verify_key))
        self.assertIsNone(p.check_revoke(text, dev.public, p.new_owner_key().verify_key))
        self.assertIsNone(p.check_revoke("hello", dev.public, owner.verify_key))


class KeySerialisationTests(unittest.TestCase):
    def test_restore_from_private_bytes(self):
        owner, dev = p.new_owner_key(), p.new_device_key()
        self.assertEqual(p.owner_key_from_private(owner.private).verify_key, owner.verify_key)
        self.assertEqual(p.device_key_from_private(dev.private).public, dev.public)
        code = p.issue_code(p.owner_key_from_private(owner.private), dev.public, _grant())
        p.open_code(code, p.device_key_from_private(dev.private), owner.verify_key)

    def test_api_hash_validation(self):
        owner, dev = p.new_owner_key(), p.new_device_key()
        with self.assertRaises(ValueError):
            p.issue_code(owner, dev.public, p.Grant(1, "not-hex", 1, NOW, NOW + 1))


if __name__ == "__main__":
    unittest.main()
