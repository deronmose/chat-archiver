"""Entry point: window, demo mode, web mode for testing, and --selftest for release checks."""
from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from pathlib import Path


def selftest(out: str | None) -> int:
    """Headless checks of a build: no window, no network. Exit code 0 = healthy."""
    results: dict[str, object] = {}
    ok = True

    def check(name, fn):
        nonlocal ok
        try:
            results[name] = fn() or "ok"
        except Exception as e:                    # report every failure, not just the first
            ok = False
            results[name] = f"FAIL: {type(e).__name__}: {e}"

    def imports():
        import cryptography, flet, png, qrcode, telethon  # noqa: F401
        return f"telethon {telethon.__version__}, flet {flet.__version__ if hasattr(flet, '__version__') else '?'}"

    def vault_roundtrip():
        from tg_export.vault import ENCRYPTED, Vault
        with tempfile.TemporaryDirectory() as d:
            v = Vault(Path(d) / "v.bin")
            v.save({"secret": "selftest-value"})
            assert b"selftest-value" not in v.path.read_bytes() or not ENCRYPTED
            assert v.load() == {"secret": "selftest-value"}
        return "encrypted (DPAPI)" if ENCRYPTED else "plain (non-Windows)"

    def protocol_roundtrip():
        from approval import protocol as p
        o, d = p.new_owner_key(), p.new_device_key()
        g = p.Grant(1, "0" * 32, 1, 0, 10)
        assert p.open_code(p.issue_code(o, d.public, g), d, o.verify_key) == g

    def guard_blocks_writes():
        from telethon import types
        from telethon.tl import functions
        from tg_export.guard import BlockedRequestError, check_request
        try:
            check_request(functions.messages.SendMessageRequest(peer=types.InputPeerEmpty(), message="x"))
        except BlockedRequestError:
            return "SendMessage blocked"
        raise AssertionError("SendMessage was not blocked")

    def build_config():
        from chat_archiver.config import load_build_config
        cfg = load_build_config()
        if cfg is None:
            raise AssertionError("no owner configuration in this build")
        return f"bot @{cfg.bot_username}"

    def bundled_client():
        if not getattr(sys, "frozen", False):
            return "not frozen (dev run)"
        import flet_desktop
        base = Path(getattr(sys, "_MEIPASS", ""))
        found = [str(p.relative_to(base)) for p in base.rglob("flet-windows*.zip")]
        if not found:
            raise AssertionError("Flet client archive is not bundled")
        return found[0]

    for name, fn in [("imports", imports), ("vault", vault_roundtrip), ("protocol", protocol_roundtrip),
                     ("guard", guard_blocks_writes), ("build_config", build_config), ("flet_client", bundled_client)]:
        check(name, fn)
    results["ok"] = ok
    text = json.dumps(results, indent=2)
    if out:
        Path(out).write_text(text, "utf-8")
    else:
        try:
            print(text)
        except Exception:
            pass
    return 0 if ok else 1


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="chat-archiver")
    ap.add_argument("--demo", action="store_true", help="fake Telegram and fake owner approval")
    ap.add_argument("--web", action="store_true", help="serve the UI over HTTP (testing)")
    ap.add_argument("--port", type=int, default=0)
    ap.add_argument("--selftest", nargs="?", const="", default=None, metavar="OUT.json")
    args = ap.parse_args(argv)
    if args.selftest is not None:
        return selftest(args.selftest or None)

    import flet as ft

    from chat_archiver.backend import default_export_folder
    from chat_archiver.ui import ChatArchiverApp
    from tg_export.vault import app_home

    if args.demo:
        from chat_archiver.demo_backend import DemoBackend
        home = Path(tempfile.mkdtemp(prefix="chat-archiver-demo-"))
        backend = DemoBackend(home)
        export_folder = home / "exports"
    else:
        from chat_archiver.config import load_build_config
        from chat_archiver.telethon_backend import TelethonBackend
        home = app_home("ChatArchiver", "CHAT_ARCHIVER_HOME")
        backend = TelethonBackend(home, load_build_config())
        export_folder = default_export_folder()

    async def page_main(page: ft.Page):
        async def on_close(e):
            await backend.close()
        page.on_close = on_close
        await ChatArchiverApp(page, backend, home=home, export_folder=export_folder, demo=args.demo).start()

    if args.web:
        os.environ["FLET_FORCE_WEB_SERVER"] = "1"      # serve only; don't open a browser
        ft.run(page_main, view=ft.AppView.WEB_BROWSER, port=args.port or 8550)
    else:
        ft.run(page_main)
    return 0
