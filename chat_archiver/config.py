"""Build-time configuration. Contains only public data: the owner's bot and verify key.

Release builds embed it as chat_archiver/_build_config.py (written by build.py from
the file Chat Archiver Admin exports). Development runs read build/approval.json.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path

from approval.protocol import _b64d


@dataclass(frozen=True)
class BuildConfig:
    bot_username: str
    bot_id: int
    owner_verify_key: bytes


def from_dict(d: dict) -> BuildConfig:
    key = _b64d(d["owner_verify_key"])
    if len(key) != 32:
        raise ValueError("owner_verify_key must be 32 bytes")
    return BuildConfig(str(d["bot_username"]).lstrip("@"), int(d["bot_id"]), key)


def load_build_config() -> BuildConfig | None:
    try:
        from chat_archiver import _build_config as embedded  # type: ignore[attr-defined]
        return from_dict(embedded.CONFIG)
    except ImportError:
        pass
    candidates = [Path(os.environ["CHAT_ARCHIVER_CONFIG"])] if os.environ.get("CHAT_ARCHIVER_CONFIG") else \
        [Path.cwd() / "release" / "approval.json", Path.cwd() / "build" / "approval.json"]
    for path in candidates:
        if path.exists():
            return from_dict(json.loads(path.read_text("utf-8")))
    return None
