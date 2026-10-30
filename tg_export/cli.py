"""Command line interface: login, chats, dump, export, logout."""
from __future__ import annotations

import argparse
import asyncio
import getpass
import io
import logging
import os
import platform
import sys
from datetime import datetime, timezone
from pathlib import Path

from telethon import errors, utils
from telethon.sessions import StringSession

from tg_export import __version__, vault
from tg_export.exporters import EXPORTERS, export_all
from tg_export.guard import BlockedRequestError, ReadOnlyClient
from tg_export.refs import parse_chat_ref
from tg_export.scraper import (UserError, chat_folder_name, download_media, dump_messages, fetch_topics,
                               resolve_chat)
from tg_export.store import Archive


# ---------- credentials & client ----------

def _credentials(data: dict, interactive: bool) -> tuple[int, str]:
    api_id = os.environ.get("TG_API_ID") or data.get("api_id")
    api_hash = os.environ.get("TG_API_HASH") or data.get("api_hash")
    if api_id and api_hash:
        return int(api_id), str(api_hash)
    if not interactive:
        raise UserError("Нет API-ключей. Сначала выполните: tg-export login")
    print("Нужны api_id и api_hash с https://my.telegram.org → API development tools.")
    api_id = input("api_id: ").strip()
    if not api_id.isdigit():
        raise UserError("api_id должен быть числом.")
    api_hash = getpass.getpass("api_hash (ввод скрыт): ").strip()
    if len(api_hash) != 32:
        raise UserError("api_hash — это 32 символа.")
    data["api_id"], data["api_hash"] = int(api_id), api_hash
    return int(api_id), api_hash


def _client(data: dict, *, interactive=False, allow_join=False, receive_updates=False) -> ReadOnlyClient:
    api_id, api_hash = _credentials(data, interactive)
    return ReadOnlyClient(
        StringSession(data.get("session") or ""), api_id, api_hash,
        allow_join=allow_join,
        receive_updates=receive_updates,
        flood_sleep_threshold=3600,          # wait out FloodWait up to an hour automatically
        device_model="tg-export",            # easy to find in Settings → Devices
        system_version=platform.system(),
        app_version=__version__,
    )


def _save_session(data: dict, client: ReadOnlyClient) -> None:
    data["session"] = client.session.save()
    vault.save(data)


async def _authorized_client(data: dict, **kw) -> ReadOnlyClient:
    client = _client(data, **kw)
    await client.connect()
    if not await client.is_user_authorized():
        await client.disconnect()
        raise UserError("Вы не вошли. Выполните: tg-export login")
    return client


# ---------- commands ----------

async def cmd_login(args) -> None:
    data = vault.load()
    client = _client(data, interactive=True, receive_updates=True)   # QR login needs updates
    await client.connect()
    try:
        if await client.is_user_authorized():
            me = await client.get_me()
            print(f"Уже выполнен вход: {me.first_name} (id {me.id}).")
        elif args.qr:
            await _login_qr(client)
        else:
            await _login_phone(client)
        _save_session(data, client)
        me = await client.get_me()
        where = "зашифровано DPAPI" if vault.ENCRYPTED else "права 0600"
        print(f"Готово: {me.first_name} (id {me.id}). Сессия сохранена в {vault.vault_path()} ({where}).")
    finally:
        await client.disconnect()


async def _login_phone(client) -> None:
    phone = input("Номер телефона в формате +380…: ").strip()
    await client.send_code_request(phone)
    code = getpass.getpass("Код из Telegram (ввод скрыт): ").strip()
    try:
        await client.sign_in(phone=phone, code=code)
    except errors.SessionPasswordNeededError:
        await client.sign_in(password=getpass.getpass("Облачный пароль 2FA (ввод скрыт): "))


async def _login_qr(client) -> None:
    import qrcode

    qr = await client.qr_login()
    for _ in range(5):
        buf = io.StringIO()
        code = qrcode.QRCode(border=1)
        code.add_data(qr.url)
        code.print_ascii(out=buf, invert=True)
        print("\nTelegram на телефоне → Настройки → Устройства → Подключить устройство:\n")
        print(buf.getvalue())
        try:
            await qr.wait(timeout=60)
            return
        except asyncio.TimeoutError:
            await qr.recreate()
        except errors.SessionPasswordNeededError:
            await client.sign_in(password=getpass.getpass("Облачный пароль 2FA (ввод скрыт): "))
            return
    raise UserError("QR-код не отсканирован. Попробуйте ещё раз или войдите по номеру.")


async def cmd_chats(args) -> None:
    data = vault.load()
    client = await _authorized_client(data)
    try:
        print(f"{'ID':>16}  {'тип':<7} {'защ.':<4} название")
        async for d in client.iter_dialogs():
            if d.is_user and not args.all:
                continue
            kind = "канал" if d.is_channel and not d.is_group else "группа" if d.is_group else "личный"
            protected = "да" if getattr(d.entity, "noforwards", False) else ""
            username = getattr(d.entity, "username", None)
            print(f"{d.id:>16}  {kind:<7} {protected:<4} {d.name}" + (f"  (@{username})" if username else ""))
        _save_session(data, client)
    finally:
        await client.disconnect()


async def cmd_dump(args) -> None:
    ref = parse_chat_ref(args.chat)
    data = vault.load()
    client = await _authorized_client(data, allow_join=args.join)
    try:
        entity = await resolve_chat(client, ref, args.join)
        chat_id = utils.get_peer_id(entity)
        title = getattr(entity, "title", None) or " ".join(
            p for p in (getattr(entity, "first_name", None), getattr(entity, "last_name", None)) if p) or str(chat_id)
        folder = Path(args.out) / chat_folder_name(title, chat_id)
        since = datetime.fromisoformat(args.since).replace(tzinfo=timezone.utc) if args.since else None
        print(f"Чат: {title} (ID {chat_id}) → {folder}")
        forum = bool(getattr(entity, "forum", False))
        with Archive(folder / "messages.db") as archive:
            if forum:
                archive.set_topics(await fetch_topics(client, entity))
            while True:
                added = await dump_messages(client, entity, archive, limit=args.limit,
                                            since=since, include_raw=args.raw, forum=forum)
                print(f"  новых/обновлённых сообщений: {added}, всего в архиве: {archive.count()}")
                if args.media:
                    max_bytes = int(args.max_media_mb * 1024 * 1024) if args.max_media_mb else None
                    ok, failed = await download_media(client, entity, archive, folder / "media",
                                                      max_bytes=max_bytes, retry_failed=args.retry_failed)
                    print(f"  медиа скачано: {ok}, ошибок: {failed}")
                archive.set_meta(chat_id=chat_id, title=title, username=getattr(entity, "username", None),
                                 exported_at=datetime.now(timezone.utc).isoformat(timespec="seconds"))
                for path, n in export_all(archive, folder, args.formats):
                    print(f"  {path.name}: {n} сообщений")
                _save_session(data, client)
                if not args.watch:
                    break
                print(f"Следующая проверка через {args.watch} с (Ctrl+C — стоп)…")
                await asyncio.sleep(args.watch)
    finally:
        await client.disconnect()


def cmd_export(args) -> None:
    folder = Path(args.folder)
    db = folder / "messages.db"
    if not db.exists():
        raise UserError(f"В {folder} нет messages.db")
    with Archive(db) as archive:
        for path, n in export_all(archive, folder, args.formats):
            print(f"{path}: {n} сообщений")


async def cmd_logout(args) -> None:
    data = vault.load()
    if data.get("session"):
        try:
            client = await _authorized_client(data)
            await client.log_out()                   # invalidates the session on Telegram's side
            print("Сессия завершена на серверах Telegram.")
        except UserError:
            pass
    if args.keep_keys and data.get("api_id"):
        vault.save({"api_id": data["api_id"], "api_hash": data["api_hash"]})
        print("Сессия удалена, API-ключи оставлены (зашифрованы).")
    elif vault.delete():
        print(f"Удалено: {vault.vault_path()}")


# ---------- argparse ----------

def _formats(value: str) -> list[str]:
    items = [v.strip().lower() for v in value.split(",") if v.strip()]
    bad = [v for v in items if v not in EXPORTERS]
    if bad:
        raise argparse.ArgumentTypeError(f"неизвестный формат: {', '.join(bad)} (есть: {', '.join(EXPORTERS)})")
    return items


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="tg-export", description="Выгрузка истории Telegram-чатов (только чтение).")
    p.add_argument("--version", action="version", version=f"tg-export {__version__}")
    sub = p.add_subparsers(dest="command", required=True)

    s = sub.add_parser("login", help="войти в аккаунт (по номеру или QR)")
    s.add_argument("--qr", action="store_true", help="вход по QR-коду с телефона")

    s = sub.add_parser("chats", help="список групп и каналов с их ID")
    s.add_argument("--all", action="store_true", help="показать и личные чаты")

    s = sub.add_parser("dump", help="выгрузить историю чата")
    s.add_argument("chat", help="t.me/+приглашение, t.me/имя, @имя, t.me/c/123/45 или ID")
    s.add_argument("--out", default="exports", help="папка для выгрузок (по умолчанию ./exports)")
    s.add_argument("--media", action="store_true", help="скачать фото, видео и файлы")
    s.add_argument("--max-media-mb", type=float, help="не качать файлы больше N МБ")
    s.add_argument("--retry-failed", action="store_true", help="повторить медиа, которые не скачались")
    s.add_argument("--since", help="только сообщения с даты YYYY-MM-DD (для первой выгрузки)")
    s.add_argument("--limit", type=int, help="не больше N сообщений за запуск")
    s.add_argument("--formats", type=_formats, default=["json", "csv", "html"], help="json,csv,html")
    s.add_argument("--raw", action="store_true", help="сохранять полный JSON каждого сообщения")
    s.add_argument("--join", action="store_true", help="разрешить вступить в группу по ссылке-приглашению")
    s.add_argument("--watch", type=int, metavar="SEC", help="повторять каждые SEC секунд")

    s = sub.add_parser("export", help="пересобрать JSON/CSV/HTML из готового архива без сети")
    s.add_argument("folder", help="папка чата с messages.db")
    s.add_argument("--formats", type=_formats, default=["json", "csv", "html"])

    s = sub.add_parser("logout", help="завершить сессию в Telegram и удалить её с диска")
    s.add_argument("--keep-keys", action="store_true", help="оставить api_id/api_hash")
    return p


COMMANDS = {"login": cmd_login, "chats": cmd_chats, "dump": cmd_dump, "export": cmd_export, "logout": cmd_logout}


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")
    args = build_parser().parse_args(argv)
    fn = COMMANDS[args.command]
    try:
        result = fn(args)
        if asyncio.iscoroutine(result):
            asyncio.run(result)
        return 0
    except KeyboardInterrupt:
        print("\nОстановлено. Прогресс сохранён — повторный запуск продолжит с того же места.")
        return 130
    except (UserError, ValueError) as e:
        print(f"Ошибка: {e}", file=sys.stderr)
        return 2
    except BlockedRequestError as e:
        print(f"Защита: {e}", file=sys.stderr)
        return 3
    except errors.RPCError as e:
        print(f"Telegram ответил ошибкой: {e}", file=sys.stderr)
        return 4
