"""Approval protocol.

The distributed app contains no Telegram API keys. Each device has its own X25519
key pair. The owner's Admin approves a device and sends it an *activation code*:
the API keys plus the approved Telegram user id and expiry, encrypted to the
device's public key (X25519 + HKDF-SHA256 + ChaCha20-Poly1305) and signed with
the owner's Ed25519 key. Only that device can decrypt it, and only the owner can
produce it.

Wire formats (all text is base64url without padding):
- start payload (t.me/<bot>?start=..., <= 64 chars): "a" + device_pub(43) + label(<= 20)
- activation code (<= 256 chars, fits a copy_text button):
  "CA1" + b64(eph_pub 32 | ciphertext 48+16 | signature 64)
- revoke notice: "CR1" + b64(issued_at 8 | signature 64)
"""
from __future__ import annotations

import base64
import re
import struct
import unicodedata
from dataclasses import dataclass

from cryptography.exceptions import InvalidSignature, InvalidTag
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey, X25519PublicKey
from cryptography.hazmat.primitives.ciphers.aead import ChaCha20Poly1305
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

VALIDITY_DAYS = 14
START_PREFIX = "a"
CODE_PREFIX = "CA1"
REVOKE_PREFIX = "CR1"

_PLAIN = struct.Struct(">Q16sQQQ")        # api_id, api_hash, user_id, issued_at, expires_at
_CODE_BYTES = 32 + _PLAIN.size + 16 + 64
_REVOKE_BYTES = 8 + 64
_RAW = serialization.Encoding.Raw
_ZERO_NONCE = bytes(12)                   # safe: every code uses a fresh ephemeral key


class ProtocolError(ValueError):
    """Malformed start payload."""


class InvalidCode(ValueError):
    """Activation code is forged, damaged, or meant for another device."""


def _b64e(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _b64d(text: str) -> bytes:
    data = base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))
    if _b64e(data) != text:               # reject non-canonical encodings
        raise ValueError("non-canonical base64")
    return data


_CODE_LEN = len(_b64e(bytes(_CODE_BYTES)))
_REVOKE_LEN = len(_b64e(bytes(_REVOKE_BYTES)))
_CODE_RE = re.compile(re.escape(CODE_PREFIX) + r"[A-Za-z0-9_-]{%d}(?![A-Za-z0-9_-])" % _CODE_LEN)
_REVOKE_RE = re.compile(re.escape(REVOKE_PREFIX) + r"[A-Za-z0-9_-]{%d}(?![A-Za-z0-9_-])" % _REVOKE_LEN)


# ---------- keys ----------

@dataclass(frozen=True)
class DeviceKey:
    private: bytes
    public: bytes


@dataclass(frozen=True)
class OwnerKey:
    private: bytes
    verify_key: bytes


def new_device_key() -> DeviceKey:
    return device_key_from_private(X25519PrivateKey.generate().private_bytes(
        _RAW, serialization.PrivateFormat.Raw, serialization.NoEncryption()))


def device_key_from_private(private: bytes) -> DeviceKey:
    key = X25519PrivateKey.from_private_bytes(private)
    return DeviceKey(private, key.public_key().public_bytes(_RAW, serialization.PublicFormat.Raw))


def new_owner_key() -> OwnerKey:
    return owner_key_from_private(Ed25519PrivateKey.generate().private_bytes(
        _RAW, serialization.PrivateFormat.Raw, serialization.NoEncryption()))


def owner_key_from_private(private: bytes) -> OwnerKey:
    key = Ed25519PrivateKey.from_private_bytes(private)
    return OwnerKey(private, key.public_key().public_bytes(_RAW, serialization.PublicFormat.Raw))


# ---------- start payload ----------

def device_label(hostname: str) -> str:
    ascii_name = unicodedata.normalize("NFKD", hostname or "").encode("ascii", "ignore").decode()
    label = re.sub(r"[^A-Za-z0-9-]", "", ascii_name)[:20]
    return label or "PC"


def make_start_payload(device_pub: bytes, label: str) -> str:
    payload = START_PREFIX + _b64e(device_pub) + device_label(label)
    assert len(payload) <= 64
    return payload


def parse_start_payload(payload: str) -> tuple[bytes, str]:
    if not payload or not payload.startswith(START_PREFIX) or len(payload) < 44 or len(payload) > 64:
        raise ProtocolError("not a Chat Archiver request")
    body, label = payload[1:44], payload[44:]
    if not re.fullmatch(r"[A-Za-z0-9-]{0,20}", label):
        raise ProtocolError("bad device label")
    try:
        pub = _b64d(body)
    except ValueError as e:
        raise ProtocolError("bad device key") from e
    if len(pub) != 32:
        raise ProtocolError("bad device key")
    return pub, label


# ---------- activation code ----------

@dataclass(frozen=True)
class Grant:
    api_id: int
    api_hash: str
    user_id: int
    issued_at: int
    expires_at: int


def is_expired(grant: Grant, now: float) -> bool:
    return now > grant.expires_at


def _aead_key(shared: bytes, eph_pub: bytes, device_pub: bytes) -> bytes:
    return HKDF(algorithm=hashes.SHA256(), length=32, salt=eph_pub + device_pub,
                info=b"chat-archiver/activation/v1").derive(shared)


def issue_code(owner: OwnerKey, device_pub: bytes, grant: Grant) -> str:
    if not re.fullmatch(r"[0-9a-fA-F]{32}", grant.api_hash or ""):
        raise ValueError("api_hash must be 32 hex characters")
    plain = _PLAIN.pack(grant.api_id, bytes.fromhex(grant.api_hash), grant.user_id,
                        grant.issued_at, grant.expires_at)
    eph = X25519PrivateKey.generate()
    eph_pub = eph.public_key().public_bytes(_RAW, serialization.PublicFormat.Raw)
    key = _aead_key(eph.exchange(X25519PublicKey.from_public_bytes(device_pub)), eph_pub, device_pub)
    aad = CODE_PREFIX.encode() + device_pub
    ct = ChaCha20Poly1305(key).encrypt(_ZERO_NONCE, plain, aad)
    sig = Ed25519PrivateKey.from_private_bytes(owner.private).sign(aad + eph_pub + ct)
    return CODE_PREFIX + _b64e(eph_pub + ct + sig)


def open_code(code: str, device: DeviceKey, owner_verify_key: bytes) -> Grant:
    try:
        if not _CODE_RE.fullmatch(code or ""):
            raise ValueError("format")
        blob = _b64d(code[len(CODE_PREFIX):])
        eph_pub, ct, sig = blob[:32], blob[32:-64], blob[-64:]
        aad = CODE_PREFIX.encode() + device.public
        Ed25519PublicKey.from_public_bytes(owner_verify_key).verify(sig, aad + eph_pub + ct)
        shared = X25519PrivateKey.from_private_bytes(device.private).exchange(
            X25519PublicKey.from_public_bytes(eph_pub))
        plain = ChaCha20Poly1305(_aead_key(shared, eph_pub, device.public)).decrypt(_ZERO_NONCE, ct, aad)
        api_id, api_hash, user_id, issued_at, expires_at = _PLAIN.unpack(plain)
    except (ValueError, InvalidSignature, InvalidTag, struct.error) as e:
        raise InvalidCode("This activation code is not valid for this computer.") from e
    return Grant(api_id, api_hash.hex(), user_id, issued_at, expires_at)


def find_code(text: str | None) -> str | None:
    m = _CODE_RE.search(text or "")
    return m.group(0) if m else None


# ---------- revoke ----------

def issue_revoke(owner: OwnerKey, device_pub: bytes, issued_at: int) -> str:
    ts = struct.pack(">Q", issued_at)
    sig = Ed25519PrivateKey.from_private_bytes(owner.private).sign(REVOKE_PREFIX.encode() + device_pub + ts)
    return REVOKE_PREFIX + _b64e(ts + sig)


def check_revoke(text: str | None, device_pub: bytes, owner_verify_key: bytes) -> int | None:
    """Return issued_at of a valid revoke notice for this device found in text, else None."""
    for m in _REVOKE_RE.finditer(text or ""):
        try:
            blob = _b64d(m.group(0)[len(REVOKE_PREFIX):])
            ts, sig = blob[:8], blob[8:]
            Ed25519PublicKey.from_public_bytes(owner_verify_key).verify(sig, REVOKE_PREFIX.encode() + device_pub + ts)
            return struct.unpack(">Q", ts)[0]
        except (ValueError, InvalidSignature):
            continue
    return None
