"""python -m chat_archiver_admin [--demo] [--web] [--port N] [--selftest [out.json]]"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import platform
import sys
import tempfile
import time
import traceback
from pathlib import Path

from . import APP_NAME, __version__

HOME_ENV = "CHAT_ARCHIVER_ADMIN_HOME"


def data_path() -> Path:
    from tg_export.vault import app_home
    return app_home(APP_NAME, HOME_ENV) / "admin.bin"


IMPORT_NAME = "admin-import.bin"


def import_prepared_data(target: Path, base: Path | None = None) -> bool:
    """Adopt an encrypted admin.bin prepared for this Windows user and placed next to the exe
    (or the project root in dev runs). Used when setup was done from a sandboxed tool whose
    AppData writes Windows redirects. Only replaces a missing or unconfigured vault; the file
    is removed after import."""
    import shutil

    from tg_export.vault import Vault

    if base is None:
        base = Path(sys.executable).parent if getattr(sys, "frozen", False) else Path(__file__).resolve().parents[1]
    seed = base / IMPORT_NAME
    if not seed.exists():
        return False
    try:
        incoming = Vault(seed).load()          # must decrypt for this Windows user
    except Exception:
        return False
    if not incoming.get("bot_token") or not incoming.get("owner_private"):
        return False
    try:
        current = Vault(target).load()
    except Exception:
        current = {}
    if current.get("bot_token"):
        return False                           # never overwrite a configured Admin
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(seed, target)
    seed.unlink()
    return True


# ---------- selftest ----------

def selftest(out: str | None) -> int:
    checks: list[dict] = []

    def check(name: str, fn) -> None:
        t = time.perf_counter()
        try:
            detail = fn()
            checks.append({"name": name, "ok": True, "detail": detail or "",
                           "ms": round((time.perf_counter() - t) * 1000)})
        except Exception as e:
            checks.append({"name": name, "ok": False,
                           "detail": f"{type(e).__name__}: {e}",
                           "trace": traceback.format_exc(limit=3)})

    def imports():
        import cryptography
        import flet
        import httpx

        from approval import protocol  # noqa: F401
        from . import botapi, demo, service, ui  # noqa: F401
        return f"flet {flet.version.flet_version if hasattr(flet, 'version') else '?'}, " \
               f"httpx {httpx.__version__}, cryptography {cryptography.__version__}"

    def dpapi():
        from tg_export import vault as vault_mod
        with tempfile.TemporaryDirectory() as d:
            v = vault_mod.Vault(Path(d) / "probe.bin")
            secret = "selftest-secret-0123456789"
            v.save({"s": secret})
            assert v.load() == {"s": secret}, "round-trip mismatch"
            raw = v.path.read_bytes()
            if vault_mod.ENCRYPTED:
                assert secret.encode() not in raw, "plain text found on disk"
        return "encrypted (DPAPI)" if vault_mod.ENCRYPTED else "not encrypted (non-Windows fallback)"

    def protocol():
        from approval import protocol as p
        owner, device = p.new_owner_key(), p.new_device_key()
        grant = p.Grant(1234567, "0123456789abcdef0123456789abcdef", 42, 1_800_000_000,
                        1_800_000_000 + p.VALIDITY_DAYS * 86400)
        code = p.issue_code(owner, device.public, grant)
        assert len(code) <= 256, "code too long"
        assert p.open_code(code, device, owner.verify_key) == grant, "grant mismatch"
        try:
            p.open_code(code, p.new_device_key(), owner.verify_key)
            raise AssertionError("code opened on another device")
        except p.InvalidCode:
            pass
        tok = p.issue_revoke(owner, device.public, 1_800_000_100)
        assert p.check_revoke(f"x {tok} y", device.public, owner.verify_key) == 1_800_000_100
        payload = p.make_start_payload(device.public, "SELFTEST-PC")
        assert p.parse_start_payload(payload) == (device.public, "SELFTEST-PC")
        return f"code {len(code)} chars"

    def service_flow():
        from tg_export.vault import Vault
        from .demo import DEMO_API_HASH, DEMO_API_ID, DEMO_TOKEN, DemoBot
        from .service import AdminService
        with tempfile.TemporaryDirectory() as d:
            bot = DemoBot(DEMO_TOKEN, interval=0, first_after=0, max_requests=10, seed=7)
            svc = AdminService(Vault(Path(d) / "a.bin"), lambda t: bot, time.time)

            async def flow():
                await svc.setup(DEMO_TOKEN, DEMO_API_ID, DEMO_API_HASH)
                new = await svc.poll_once()
                assert new, "no requests parsed"
                appr = await svc.approve(new[0].id)
                btn = bot.sent[-1]["reply_markup"]["inline_keyboard"][0][0]
                assert btn["copy_text"]["text"].startswith("CA1")
                await svc.revoke(appr.id)
                return len(new)
            n = asyncio.run(flow())
            assert DEMO_TOKEN.encode() not in (Path(d) / "a.bin").read_bytes() or not _encrypted()
        return f"{n} demo requests, approve + revoke ok"

    def redaction():
        from .botapi import BotApi, BotApiError
        tok = "123456789:AAFakeTokenForSelftest_abcdefghij"
        assert tok not in str(BotApiError(f"failed POST https://api.telegram.org/bot{tok}/getMe"))
        assert tok not in repr(BotApi(tok))
        return "token redacted"

    check("imports", imports)
    check("dpapi_roundtrip", dpapi)
    check("protocol_issue_open", protocol)
    check("service_demo_flow", service_flow)
    check("token_redaction", redaction)

    ok = all(c["ok"] for c in checks)
    result = {"app": APP_NAME, "version": __version__, "ok": ok, "python": sys.version.split()[0],
              "platform": platform.platform(), "checks": checks}
    text = json.dumps(result, indent=2)
    if out:
        Path(out).parent.mkdir(parents=True, exist_ok=True)
        Path(out).write_text(text + "\n", encoding="utf-8")
    for c in checks:
        print(f"[{'OK' if c['ok'] else 'FAIL'}] {c['name']}: {c['detail']}")
    print("SELFTEST", "PASSED" if ok else "FAILED")
    return 0 if ok else 1


def _encrypted() -> bool:
    from tg_export import vault as vault_mod
    return vault_mod.ENCRYPTED


# ---------- app ----------

def run_app(demo: bool, web: bool, port: int, host: str) -> int:
    import flet as ft

    from tg_export.vault import Vault

    from .botapi import BotApi
    from .service import AdminService
    from .ui import Hub, make_main

    tmp = None
    if demo:
        from .demo import DEMO_API_HASH, DEMO_API_ID, DEMO_TOKEN, demo_bot_factory, demo_vault
        vault, tmp = demo_vault()
        factory = demo_bot_factory
        defaults = {"token": DEMO_TOKEN, "api_id": DEMO_API_ID, "api_hash": DEMO_API_HASH}
        build_dir = Path(tmp.name) / "build"      # never overwrite the real build/approval.json with demo keys
    else:
        import_prepared_data(data_path())
        vault = Vault(data_path())
        factory = BotApi
        defaults = {}
        build_dir = Path.cwd() / "build"

    hub = Hub(lambda: AdminService(vault=vault, bot_factory=factory, clock=time.time),
              demo=demo, demo_defaults=defaults, build_dir=build_dir, reset_data=vault.delete)
    try:
        if web:
            # Web server only: FLET_FORCE_WEB_SERVER makes flet serve the UI without opening a browser.
            os.environ["FLET_FORCE_WEB_SERVER"] = "true"
            print(f"{APP_NAME}{' (demo)' if demo else ''}: open http://{host}:{port} in a browser. "
                  "Press Ctrl+C to stop.", flush=True)
            ft.run(make_main(hub), view=ft.AppView.WEB_BROWSER, host=host, port=port, name="")
        else:
            ft.run(make_main(hub), view=ft.AppView.FLET_APP)
    finally:
        if tmp is not None:
            tmp.cleanup()
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="chat_archiver_admin", description=f"{APP_NAME}: approve access requests.")
    ap.add_argument("--demo", action="store_true", help="simulated bot and temporary data (nothing is sent)")
    ap.add_argument("--web", action="store_true", help="serve the UI over HTTP instead of opening a window")
    ap.add_argument("--port", type=int, default=8551, help="port for --web (default 8551)")
    ap.add_argument("--host", default="127.0.0.1", help="interface for --web (default 127.0.0.1)")
    ap.add_argument("--selftest", nargs="?", const="", metavar="OUT_JSON",
                    help="check imports, DPAPI and the protocol without a window; optional JSON report path")
    args = ap.parse_args(argv)
    if args.selftest is not None:
        return selftest(args.selftest or None)
    return run_app(args.demo, args.web, args.port, args.host)


if __name__ == "__main__":
    sys.exit(main())
