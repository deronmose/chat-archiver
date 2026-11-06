"""Chat Archiver window (Flet). All Telegram work goes through a Backend."""
from __future__ import annotations

import asyncio
import io
import json
import os
from datetime import date
from pathlib import Path

import flet as ft
import png
import qrcode

from chat_archiver import APP_NAME, __version__
from chat_archiver.backend import (AccessDenied, AccessState, Account, Backend, ChatInfo, ExportOptions,
                                   ExportResult, Progress, Topic, UserFacingError, friendly_error)

ACCENT = ft.Colors.INDIGO
STEPS = ["Access", "Sign in", "Archive"]
SIZE_OPTIONS = [("10", "10 MB"), ("50", "50 MB"), ("200", "200 MB"), ("0", "No limit")]


def qr_png(data: str, target: int = 200) -> tuple[bytes, int]:
    """8-bit greyscale PNG (Flutter does not render qrcode's 1-bit PNGs) with the standard 4-module
    quiet zone. Returns (png, size): show it at exactly that size, unfiltered, so modules stay sharp."""
    qr = qrcode.QRCode(border=4, error_correction=qrcode.constants.ERROR_CORRECT_M)
    qr.add_data(data)
    matrix = qr.get_matrix()
    scale = max(3, target // len(matrix))           # whole pixels per module, never resampled
    rows = []
    for line in matrix:
        row = [0 if dark else 255 for dark in line for _ in range(scale)]
        rows.extend([row] * scale)
    buf = io.BytesIO()
    png.Writer(len(rows[0]), len(rows), greyscale=True, bitdepth=8).write(buf, rows)
    return buf.getvalue(), len(rows)


def qr_image(data: str, target: int = 200) -> ft.Image:
    src, size = qr_png(data, target)
    return ft.Image(src=src, width=size, height=size, filter_quality=ft.FilterQuality.NONE,
                    gapless_playback=True)


def T(text: str, size: int = 14, weight=None, color=None, **kw) -> ft.Text:
    return ft.Text(text, size=size, weight=weight, color=color, **kw)


def muted(text: str, size: int = 13, **kw) -> ft.Text:
    return ft.Text(text, size=size, color=ft.Colors.ON_SURFACE_VARIANT, **kw)


def badge(text: str, color) -> ft.Container:
    return ft.Container(T(text, 11, ft.FontWeight.W_600, color), border_radius=8,
                        padding=ft.Padding.symmetric(horizontal=8, vertical=2),
                        bgcolor=ft.Colors.with_opacity(0.12, color))


def note(text: str, kind: str = "info") -> ft.Container:
    icon, color = {"info": (ft.Icons.INFO_OUTLINE, ft.Colors.BLUE),
                   "warn": (ft.Icons.WARNING_AMBER_ROUNDED, ft.Colors.AMBER_800),
                   "error": (ft.Icons.ERROR_OUTLINE, ft.Colors.ERROR),
                   "ok": (ft.Icons.CHECK_CIRCLE, ft.Colors.GREEN)}[kind]
    return ft.Container(
        ft.Row([ft.Icon(icon, color=color, size=20), ft.Text(text, size=13, expand=True)],
               vertical_alignment=ft.CrossAxisAlignment.START),
        padding=12, border_radius=10, bgcolor=ft.Colors.with_opacity(0.08, color),
        border=ft.Border.all(1, ft.Colors.with_opacity(0.25, color)))


def card(*controls, width=None, padding=28) -> ft.Container:
    return ft.Container(ft.Column(list(controls), spacing=14, tight=True), width=width, padding=padding,
                        border_radius=16, bgcolor=ft.Colors.SURFACE_CONTAINER_LOW,
                        border=ft.Border.all(1, ft.Colors.OUTLINE_VARIANT))


def centered(control: ft.Control) -> ft.Control:
    return ft.Column([ft.Row([control], alignment=ft.MainAxisAlignment.CENTER)],
                     alignment=ft.MainAxisAlignment.CENTER, scroll=ft.ScrollMode.AUTO, expand=True)


class ChatArchiverApp:
    def __init__(self, page: ft.Page, backend: Backend, *, home: Path, export_folder: Path, demo: bool):
        self.page = page
        self.be = backend
        self.home = Path(home)
        self.demo = demo
        self.prefs_path = self.home / "prefs.json"
        self.prefs = self._load_prefs()
        self.export_folder = Path(self.prefs.get("export_folder") or export_folder)
        self.account: Account | None = None
        self.bg: asyncio.Task | None = None
        self.exporting = False                       # one archive at a time; the panel belongs to it
        self.body = ft.Container(expand=True)
        self.header_right = ft.Row(spacing=4)

    # ---------- infrastructure ----------
    def _load_prefs(self) -> dict:
        try:
            return json.loads(self.prefs_path.read_text("utf-8"))
        except (OSError, ValueError):
            return {}

    def _save_prefs(self) -> None:
        self.home.mkdir(parents=True, exist_ok=True)
        self.prefs_path.write_text(json.dumps(self.prefs), "utf-8")

    def _stop_bg(self) -> None:
        # a background loop may navigate away itself; it must not cancel its own task
        if self.bg and not self.bg.done() and self.bg is not asyncio.current_task():
            self.bg.cancel()
        self.bg = None

    def _spawn(self, coro) -> asyncio.Task:
        self._stop_bg()
        self.bg = asyncio.create_task(coro)
        return self.bg

    def show(self, control: ft.Control) -> None:
        self.body.content = control
        self.page.update()

    def toast(self, text: str) -> None:
        self.page.show_dialog(ft.SnackBar(ft.Text(text)))

    def busy(self) -> bool:
        if self.exporting:
            self.toast("An archive is being made. Stop it first, or wait until it's done.")
        return self.exporting

    async def start(self) -> None:
        p = self.page
        p.title = APP_NAME
        p.theme = ft.Theme(color_scheme_seed=ACCENT)
        p.dark_theme = ft.Theme(color_scheme_seed=ACCENT)
        p.theme_mode = ft.ThemeMode.SYSTEM
        p.padding = 0
        if not p.web:
            p.window.width, p.window.height = 1060, 760
            p.window.min_width, p.window.min_height = 820, 620
        self.clipboard = ft.Clipboard()
        self.launcher = ft.UrlLauncher()
        self.picker = ft.FilePicker() if not p.web else None
        logo = ft.Container(ft.Icon(ft.Icons.ARCHIVE_ROUNDED, color=ft.Colors.WHITE, size=20), width=34, height=34,
                            border_radius=10, bgcolor=ACCENT, alignment=ft.Alignment.CENTER)
        header = ft.Container(
            ft.Row([ft.Row([logo, T(APP_NAME, 17, ft.FontWeight.BOLD)] + ([badge("DEMO", ft.Colors.AMBER_800)]
                                                                         if self.demo else []), spacing=10),
                    self.header_right], alignment=ft.MainAxisAlignment.SPACE_BETWEEN),
            padding=ft.Padding.symmetric(horizontal=20, vertical=12),
            border=ft.Border.only(bottom=ft.BorderSide(1, ft.Colors.OUTLINE_VARIANT)))
        p.add(ft.Column([header, self.body], expand=True, spacing=0))
        self._set_header()
        await self.route()

    def _set_header(self) -> None:
        items = [ft.IconButton(ft.Icons.INFO_OUTLINE, tooltip="About", on_click=lambda e: self.show_about())]
        if self.account:
            items = [
                ft.Icon(ft.Icons.PERSON_OUTLINE, size=18, color=ft.Colors.ON_SURFACE_VARIANT),
                muted(self.account.name + (f"  @{self.account.username}" if self.account.username else "")),
                ft.PopupMenuButton(icon=ft.Icons.MORE_VERT, items=[
                    ft.PopupMenuItem(content=T("Switch account"), icon=ft.Icons.SWITCH_ACCOUNT,
                                     on_click=lambda e: self.switch_account()),
                    ft.PopupMenuItem(content=T("About"), icon=ft.Icons.INFO_OUTLINE, on_click=lambda e: self.show_about()),
                    ft.PopupMenuItem(content=T("Log out"), icon=ft.Icons.LOGOUT, on_click=lambda e: self.confirm_logout()),
                ]),
            ]
        self.header_right.controls = items

    def steps(self, current: int) -> ft.Row:
        parts = []
        for i, name in enumerate(STEPS):
            done, active = i < current, i == current
            color = ACCENT if (done or active) else ft.Colors.OUTLINE
            dot = ft.Container(ft.Icon(ft.Icons.CHECK, size=14, color=ft.Colors.WHITE) if done
                               else T(str(i + 1), 12, ft.FontWeight.BOLD, ft.Colors.WHITE if active else color),
                               width=24, height=24, border_radius=12, alignment=ft.Alignment.CENTER,
                               bgcolor=color if (done or active) else None, border=ft.Border.all(1.5, color))
            parts.append(ft.Row([dot, T(name, 13, ft.FontWeight.W_600 if active else None,
                                        None if active else ft.Colors.ON_SURFACE_VARIANT)], spacing=6))
            if i < len(STEPS) - 1:
                parts.append(ft.Container(width=28, height=1.5, bgcolor=ft.Colors.OUTLINE_VARIANT))
        return ft.Row(parts, spacing=8, alignment=ft.MainAxisAlignment.CENTER)

    # ---------- routing ----------
    async def route(self) -> None:
        self._stop_bg()
        state = self.be.access_state()
        if state is AccessState.NOT_CONFIGURED:
            return self.show_not_configured()
        if not self.prefs.get("welcome_done"):
            return self.show_welcome()
        if state is AccessState.NEEDS_ACTIVATION:
            return self.show_activation()
        cur = self.be.current_account()
        if cur and cur.signed_in:                       # resume the account used last time
            return await self.open_account(cur.user_id)
        self.show_accounts()

    async def open_account(self, user_id: int) -> None:
        self._stop_bg()
        self.show_loading("Connecting…")
        try:
            await self.be.select_account(user_id)
            await self.be.connect()
            if await self.be.is_authorized():
                return await self.after_login()
        except UserFacingError as e:
            return self.show_fatal(str(e), retry=True)
        await self.show_login()

    # ---------- accounts ----------
    def show_accounts(self, notice: str | None = None) -> None:
        self._stop_bg()
        self.account = None
        self._set_header()
        accounts = self.be.accounts()
        if not accounts:
            return self.show_activation(notice)
        rows = []
        for a in accounts:
            async def pick(e, uid=a.user_id):
                await self.open_account(uid)

            initials = "".join(w[0] for w in a.label.split()[:2]).upper() if a.name else "#"
            status = ("@" + a.username + "  ·  " if a.username else "") + ("Signed in" if a.signed_in else "Signed out")
            rows.append(ft.Container(
                ft.Row([
                    ft.Container(T(initials, 14, ft.FontWeight.BOLD, ft.Colors.WHITE), width=40, height=40,
                                 border_radius=20, bgcolor=ACCENT, alignment=ft.Alignment.CENTER),
                    ft.Column([T(a.label, 15, ft.FontWeight.W_600), muted(status, 12)], spacing=2, expand=True),
                    ft.IconButton(ft.Icons.DELETE_OUTLINE, tooltip="Remove from this computer",
                                  on_click=lambda e, a=a: self.confirm_remove(a)),
                    ft.Icon(ft.Icons.CHEVRON_RIGHT, color=ft.Colors.ON_SURFACE_VARIANT),
                ], spacing=14),
                padding=ft.Padding.symmetric(horizontal=14, vertical=10), border_radius=12, ink=True,
                border=ft.Border.all(1, ft.Colors.OUTLINE_VARIANT), on_click=pick))
        self.show(centered(card(
            T("Choose an account", 22, ft.FontWeight.BOLD),
            muted("Telegram accounts approved on this computer. Each account needs the owner's approval.", 14),
            *([note(notice, "warn")] if notice else []),
            *rows,
            ft.Row([ft.OutlinedButton("Add another account", icon=ft.Icons.ADD,
                                      on_click=lambda e: self.show_activation())]),
            width=560)))

    def confirm_remove(self, a) -> None:
        async def do(e):
            self.page.pop_dialog()
            self.show_loading("Removing…")
            await self.be.remove_account(a.user_id)
            self.show_accounts()

        self.page.show_dialog(ft.AlertDialog(
            modal=True, title=T(f"Remove {a.label}?", 18, ft.FontWeight.BOLD),
            content=ft.Container(muted("This account will be signed out and forgotten on this computer. To use it "
                                       "again, it will need the owner's approval.", 14), width=420),
            actions=[ft.TextButton("Cancel", on_click=lambda e: self.page.pop_dialog()),
                     ft.FilledButton("Remove", on_click=do)]))

    def switch_account(self) -> None:
        if not self.busy():
            self.show_accounts()

    def show_loading(self, text: str) -> None:
        self.show(centered(ft.Column([ft.ProgressRing(width=36, height=36), muted(text, 14)],
                                     horizontal_alignment=ft.CrossAxisAlignment.CENTER, spacing=16)))

    def show_fatal(self, text: str, retry: bool = False) -> None:
        async def again(e):
            await self.route()
        self.show(centered(card(note(text, "error"),
                                *([ft.FilledButton("Try again", icon=ft.Icons.REFRESH, on_click=again)] if retry else []),
                                width=520)))

    def show_not_configured(self) -> None:
        self.show(centered(card(
            T("This copy is not set up", 20, ft.FontWeight.BOLD),
            muted("This build of Chat Archiver has no owner configuration, so it can't request access. "
                  "Please ask the person who gave you the app for a new copy.", 14),
            width=520)))

    # ---------- welcome ----------
    def show_welcome(self) -> None:
        go = ft.FilledButton("Continue", icon=ft.Icons.ARROW_FORWARD, disabled=True)
        agree = ft.Checkbox(label="I understand")

        def on_agree(e):
            go.disabled = not agree.value
            self.page.update()

        async def on_go(e):
            self.prefs["welcome_done"] = True
            self._save_prefs()
            await self.route()

        agree.on_change = on_agree
        go.on_click = on_go

        def point(icon, title, text):
            return ft.Row([ft.Container(ft.Icon(icon, color=ACCENT, size=22), padding=8, border_radius=10,
                                        bgcolor=ft.Colors.with_opacity(0.1, ACCENT)),
                           ft.Column([T(title, 14, ft.FontWeight.W_600), muted(text)], spacing=2, expand=True)],
                          vertical_alignment=ft.CrossAxisAlignment.START, spacing=14)

        self.show(centered(card(
            T("Archive your Telegram chats", 24, ft.FontWeight.BOLD),
            muted("Save the full history of groups and channels you are in — as a readable web page, "
                  "a spreadsheet and JSON, with photos and files.", 14),
            ft.Divider(height=8, color=ft.Colors.TRANSPARENT),
            point(ft.Icons.VISIBILITY_OUTLINED, "Read-only",
                  "Never sends messages, deletes anything or marks chats as read."),
            point(ft.Icons.LOCK_OUTLINE, "Private",
                  "Your sign-in is stored encrypted on this computer. Archives are saved only to your disk."),
            point(ft.Icons.FORUM_OUTLINED, "Groups, channels and topics",
                  "Archive a whole chat or a single topic. Interrupted? It continues where it stopped."),
            note("Chat Archiver is an independent app that uses the Telegram API. It is not affiliated with "
                 "or endorsed by Telegram. Archives contain other people's messages — keep them private and "
                 "do not use them to train AI models."),
            ft.Row([agree, go], alignment=ft.MainAxisAlignment.SPACE_BETWEEN),
            width=600)))

    # ---------- activation ----------
    def show_activation(self, error: str | None = None) -> None:
        url = self.be.request_url()
        status = ft.Column(spacing=10)
        err = ft.Text(error or "", color=ft.Colors.ERROR, size=13, visible=bool(error))
        code_field = ft.TextField(label="Activation code", hint_text="Paste the code here if it isn't picked up",
                                  dense=True, expand=True)

        def activate(text: str | None, manual: bool) -> bool:
            try:
                if self.be.try_activate(text):
                    return True
                if manual:
                    err.value, err.visible = "That doesn't look like an activation code.", True
            except UserFacingError as ex:
                err.value, err.visible = str(ex), True
            self.page.update()
            return False

        async def done():
            self._stop_bg()
            self.toast("Access granted")
            cur = self.be.current_account()             # the account this approval is for
            await (self.open_account(cur.user_id) if cur else self.route())

        async def on_manual(e):
            if activate(code_field.value, manual=True):
                await done()

        async def watch():
            last_clip = None
            while True:
                code = await self.be.poll_activation()
                if code and activate(code, manual=False):
                    return await done()
                try:
                    clip = await self.clipboard.get()
                except Exception:
                    clip = None
                if clip and clip != last_clip:
                    last_clip = clip
                    if activate(clip, manual=False):
                        return await done()
                await asyncio.sleep(1.5)

        async def open_bot():
            try:                                   # may never return if the OS/browser blocks it
                await asyncio.wait_for(self.launcher.launch_url(url), timeout=10)
            except Exception:
                pass

        async def on_request(e):
            if not self.demo:                       # the demo bot link is fake; don't open it
                asyncio.create_task(open_bot())
            status.controls = [
                ft.Row([ft.ProgressRing(width=18, height=18, stroke_width=2.5),
                        T("Waiting for the owner's approval…", 14, ft.FontWeight.W_600)], spacing=10),
                muted("In Telegram, press Start in the bot chat. When the owner approves, the bot will send you "
                      "a “Copy activation code” button — tap it and come back here. "
                      "Chat Archiver picks the code up automatically."),
            ]
            request_btn.content = "Open Telegram again"
            self.page.update()
            if not self.bg or self.bg.done():
                self._spawn(watch())

        request_btn = ft.FilledButton("Request access", icon=ft.Icons.SEND_ROUNDED, on_click=on_request)
        adding = bool(self.be.accounts())                 # other accounts are already approved here
        back = [ft.TextButton("Back to accounts", icon=ft.Icons.ARROW_BACK,
                              on_click=lambda e: self.show_accounts())] if adding else []
        self.show(centered(ft.Column([
            self.steps(0),
            card(
                T("Add another account" if adding else "Request access", 22, ft.FontWeight.BOLD),
                muted(("Press Start in the owner's bot from the Telegram account you want to add — each account is "
                       "approved separately. " if adding else
                       "Chat Archiver works only with the owner's approval. The button opens the owner's bot in your "
                       "Telegram app — press Start there. ") +
                      "Your Telegram name and this computer's name will be sent to the owner.", 14),
                ft.Row([
                    ft.Column([request_btn, status], spacing=14, expand=True),
                    ft.Column([qr_image(url, 200),
                               muted("or scan with your phone", 12)],
                              horizontal_alignment=ft.CrossAxisAlignment.CENTER, spacing=6),
                ], vertical_alignment=ft.CrossAxisAlignment.START, spacing=24),
                ft.Divider(),
                ft.Row([code_field, ft.OutlinedButton("Activate", on_click=on_manual)]),
                err,
                *back,
                width=640),
        ], horizontal_alignment=ft.CrossAxisAlignment.CENTER, spacing=22, tight=True)))

    # ---------- login ----------
    async def show_login(self, error: str | None = None, offer_reset: bool = False) -> None:
        """offer_reset: the signed-in account isn't approved here — offer to request access for it."""
        self._stop_bg()

        approved = self.be.accounts()
        cur = self.be.current_account()
        reset = ft.Row([
            ft.OutlinedButton("Request access for this account", icon=ft.Icons.SEND_ROUNDED,
                              on_click=lambda e: self.show_activation()),
            ft.TextButton("Choose another account", on_click=lambda e: self.show_accounts()),
        ], wrap=True, visible=offer_reset)
        hint = muted(f"Sign in as {cur.label}" + (f" (@{cur.username})" if cur.username else "") + ".", 14) \
            if cur and cur.name and not offer_reset else None
        others = ft.TextButton("Choose another account", on_click=lambda e: self.show_accounts()) \
            if len(approved) > 1 and not offer_reset else None
        mode = ft.SegmentedButton(segments=[ft.Segment("qr", label=T("QR code"), icon=ft.Icon(ft.Icons.QR_CODE_2)),
                                            ft.Segment("phone", label=T("Phone number"), icon=ft.Icon(ft.Icons.PHONE))],
                                  selected=["qr"])
        area = ft.Container()
        err = ft.Text(error or "", color=ft.Colors.ERROR, size=13, visible=bool(error))

        def fail(text: str):
            err.value, err.visible = text, True
            self.page.update()

        async def qr_loop():
            try:
                await qr_steps()
            except asyncio.CancelledError:
                raise
            except Exception as ex:                # never leave a stale QR with "Waiting for scan…"
                fail(friendly_error(ex))
                area.content = ft.FilledButton("Try again", icon=ft.Icons.REFRESH,
                                               on_click=lambda e: self._spawn(qr_loop()))
                self.page.update()

        async def qr_steps():
            while True:
                url = await self.be.qr_start()
                if err.value != error:              # keep the reason we came here (e.g. wrong account)
                    err.visible = False
                area.content = ft.Row([
                    qr_image(url, 230),
                    ft.Column([T("Scan with your phone", 16, ft.FontWeight.W_600),
                               muted("1. Open Telegram on your phone\n2. Go to Settings → Devices\n"
                                     "3. Tap “Link Desktop Device” and scan this code"),
                               ft.Row([ft.ProgressRing(width=14, height=14, stroke_width=2),
                                       muted("Waiting for scan…", 12)], spacing=8)],
                              spacing=12, expand=True),
                ], spacing=24, vertical_alignment=ft.CrossAxisAlignment.CENTER)
                self.page.update()
                result = await self.be.qr_wait(30)
                if result == "ok":
                    return await self.after_login()
                if result == "password":
                    return password_step()

        def password_step():
            pw = ft.TextField(label="Two-step verification password", password=True, can_reveal_password=True,
                              autofocus=True)

            async def submit(e):
                btn.disabled = True
                self.page.update()
                try:
                    await self.be.sign_in_password(pw.value or "")
                    await self.after_login()
                except UserFacingError as ex:
                    btn.disabled = False
                    fail(str(ex))

            async def back(e):
                await self.show_login()

            btn = ft.FilledButton("Sign in", on_click=submit)
            pw.on_submit = submit
            area.content = ft.Column([muted("Your account has a cloud password. Enter it to finish signing in."),
                                      pw, ft.Row([btn, ft.TextButton("Back", on_click=back)])], spacing=12)
            mode.disabled = True
            self.page.update()

        def phone_step():
            phone = ft.TextField(label="Phone number", hint_text="+380501234567", prefix_icon=ft.Icons.PHONE,
                                 autofocus=True, keyboard_type=ft.KeyboardType.PHONE)
            code = ft.TextField(label="Code from Telegram", visible=False, keyboard_type=ft.KeyboardType.NUMBER)

            async def send(e):
                btn.disabled = True
                err.visible = False
                self.page.update()
                try:
                    if not code.visible:
                        await self.be.send_code(phone.value or "")
                        code.visible, phone.disabled, change.visible = True, True, True
                        btn.content = "Sign in"
                        hint.value = "Telegram sent a code to your Telegram app (or by SMS). Enter it below."
                    else:
                        if await self.be.sign_in_code(code.value or "") == "password":
                            return password_step()
                        return await self.after_login()
                except UserFacingError as ex:
                    fail(str(ex))
                btn.disabled = False
                self.page.update()

            hint = muted("We'll send a login code to your Telegram app.")
            btn = ft.FilledButton("Send code", on_click=send)
            change = ft.TextButton("Change number", visible=False, on_click=lambda e: phone_step())
            phone.on_submit = code.on_submit = send
            area.content = ft.Column([hint, phone, code, ft.Row([btn, change])], spacing=12)
            self.page.update()

        async def on_mode(e):
            err.visible = False
            if mode.selected[0] == "qr":
                self._spawn(qr_loop())
            else:
                self._stop_bg()
                phone_step()

        mode.on_change = on_mode
        self.show(centered(ft.Column([
            self.steps(1),
            card(T("Sign in to Telegram", 22, ft.FontWeight.BOLD),
                 hint or muted("Use the Telegram account that is a member of the chats you want to archive.", 14),
                 mode, err, reset, area, *([others] if others else []), width=640),
        ], horizontal_alignment=ft.CrossAxisAlignment.CENTER, spacing=22, tight=True)))
        self._spawn(qr_loop())

    async def after_login(self) -> None:
        self._stop_bg()
        self.show_loading("Checking access…")
        try:
            await self.be.verify_access()
            self.account = await self.be.account()
        except AccessDenied as e:
            self.account = None
            self._set_header()
            if e.reason == "wrong_account":
                return await self.show_login(str(e), offer_reset=True)
            return self.show_accounts(str(e))        # expired/revoked: only that account was removed
        except UserFacingError as e:
            return self.show_fatal(str(e), retry=True)
        self._set_header()
        await self.show_main()

    # ---------- main ----------
    async def show_main(self) -> None:
        self._stop_bg()
        self.right = ft.Container(expand=True, padding=24)
        self.chat_list = ft.ListView(expand=True, spacing=2)
        self.all_chats: list[ChatInfo] = []
        link = ft.TextField(hint_text="Paste a chat link", tooltip="t.me/name, t.me/+invite, t.me/c/…/…, @name",
                            expand=True, dense=True,
                            prefix_icon=ft.Icons.LINK)
        search = ft.TextField(hint_text="Search your chats", dense=True, prefix_icon=ft.Icons.SEARCH)
        link_err = ft.Text("", color=ft.Colors.ERROR, size=12, visible=False)

        async def find(e):
            link_err.visible = False
            if not (link.value or "").strip() or self.busy():
                return
            find_btn.disabled = True
            self.page.update()
            try:
                res = await self.be.resolve(link.value)
                if res.needs_join:
                    self.ask_join(link.value, res.invite_title)
                else:
                    await self.select_chat(res.chat, res.topic_id)
            except UserFacingError as ex:
                link_err.value, link_err.visible = str(ex), True
            find_btn.disabled = False
            self.page.update()

        async def paste(e):
            try:
                link.value = (await self.clipboard.get() or "").strip()
            except Exception:
                return
            await find(e)

        def filter_list(e=None):
            q = (search.value or "").lower().strip()
            self.chat_list.controls = [self._chat_tile(c) for c in self.all_chats
                                       if not q or q in c.title.lower() or q in (c.username or "").lower()]
            if not self.chat_list.controls:
                self.chat_list.controls = [ft.Container(muted("No chats found." if self.all_chats else
                                                              "You are not in any groups or channels yet."),
                                                        padding=16)]
            self.page.update()

        find_btn = ft.FilledButton("Find", on_click=find)
        link.on_submit = find
        search.on_change = filter_list
        self.refilter = filter_list
        left = ft.Container(ft.Column([
            T("Choose a chat", 16, ft.FontWeight.BOLD),
            ft.Row([link, ft.IconButton(ft.Icons.CONTENT_PASTE, tooltip="Paste and find", on_click=paste), find_btn],
                   spacing=4),
            link_err,
            ft.Row([ft.Container(height=1, bgcolor=ft.Colors.OUTLINE_VARIANT, expand=True),
                    muted("or pick from your chats", 12),
                    ft.Container(height=1, bgcolor=ft.Colors.OUTLINE_VARIANT, expand=True)], spacing=8),
            search, self.chat_list,
        ], spacing=10, expand=True), width=380, padding=20,
            border=ft.Border.only(right=ft.BorderSide(1, ft.Colors.OUTLINE_VARIANT)))
        self.right.content = self._placeholder()
        self.show(ft.Row([left, self.right], expand=True, spacing=0, vertical_alignment=ft.CrossAxisAlignment.STRETCH))
        self.chat_list.controls = [ft.Container(ft.Row([ft.ProgressRing(width=16, height=16, stroke_width=2),
                                                        muted("Loading your chats…")], spacing=10), padding=16)]
        self.page.update()
        try:
            self.all_chats = await self.be.chats()
        except UserFacingError as ex:
            self.chat_list.controls = [note(str(ex), "error")]
            self.page.update()
            return
        filter_list()

    def _placeholder(self) -> ft.Control:
        return centered(ft.Column([
            ft.Icon(ft.Icons.ARCHIVE_OUTLINED, size=56, color=ft.Colors.OUTLINE),
            T("Pick a group or channel", 18, ft.FontWeight.W_600),
            muted("Paste a link on the left or choose one of your chats.", 14),
        ], horizontal_alignment=ft.CrossAxisAlignment.CENTER, spacing=8))

    def _chat_tile(self, c: ChatInfo) -> ft.ListTile:
        icon = ft.Icons.CAMPAIGN_OUTLINED if c.kind == "channel" else (
            ft.Icons.FORUM_OUTLINED if c.forum else ft.Icons.GROUP_OUTLINED)
        tags = []
        if c.forum:
            tags.append(badge("Topics", ACCENT))
        if c.protected:
            tags.append(badge("Protected", ft.Colors.AMBER_800))

        async def pick(e):
            if not self.busy():
                await self.select_chat(c, None)

        return ft.ListTile(leading=ft.Icon(icon), title=T(c.title, 14, max_lines=1, overflow=ft.TextOverflow.ELLIPSIS),
                           subtitle=ft.Row([muted(("Channel" if c.kind == "channel" else "Group")
                                                  + (f" · @{c.username}" if c.username else ""), 12)] + tags,
                                           spacing=6),
                           dense=True, on_click=pick)

    def ask_join(self, link: str, title: str | None) -> None:
        async def join(e):
            self.page.pop_dialog()
            try:
                chat = await self.be.join(link)
                self.all_chats.append(chat)
                self.refilter()
                await self.select_chat(chat, None)
            except UserFacingError as ex:
                self.toast(str(ex))

        self.page.show_dialog(ft.AlertDialog(
            modal=True, title=T(f"Join “{title or 'this group'}”?", 18, ft.FontWeight.BOLD),
            content=ft.Container(muted("You are not a member of this chat yet. To archive it, Chat Archiver will join "
                                       "it with your account. You can leave it later in Telegram.", 14), width=420),
            actions=[ft.TextButton("Cancel", on_click=lambda e: self.page.pop_dialog()),
                     ft.FilledButton("Join", on_click=join)]))

    # ---------- export panel ----------
    async def select_chat(self, chat: ChatInfo, topic_id: int | None) -> None:
        topic_dd = ft.Dropdown(label="Topic", options=[ft.DropdownOption(key="all", text="All topics")],
                               value="all", visible=chat.forum, expand=True)
        folder = ft.TextField(label="Save to", value=str(self.export_folder), read_only=not self.page.web,
                              expand=True, dense=True)
        media = ft.Switch(label="Download photos, videos and files", value=True)
        size = ft.Dropdown(label="Skip files larger than", value="50", dense=True, width=200,
                           options=[ft.DropdownOption(key=k, text=t) for k, t in SIZE_OPTIONS])
        since = ft.TextField(label="Since date (optional)", hint_text="YYYY-MM-DD", dense=True, width=220,
                             tooltip="Only archive messages sent on or after this date")
        fmts = {k: ft.Checkbox(label=lbl, value=True) for k, lbl in
                (("html", "Web page (HTML)"), ("csv", "Spreadsheet (CSV)"), ("json", "JSON"))}
        err = ft.Text("", color=ft.Colors.ERROR, size=13, visible=False)

        async def change_folder(e):
            path = await self.picker.get_directory_path(dialog_title="Choose where to save archives",
                                                        initial_directory=folder.value)
            if path:
                folder.value = path
                self.page.update()

        def on_media(e):
            size.disabled = not media.value
            self.page.update()

        async def start(e):
            err.visible = False
            formats = tuple(k for k, cb in fmts.items() if cb.value)
            since_date = None
            try:
                if (since.value or "").strip():
                    since_date = date.fromisoformat(since.value.strip())
            except ValueError:
                err.value, err.visible = "Enter the date as YYYY-MM-DD, e.g. 2025-01-31.", True
            if not formats:
                err.value, err.visible = "Choose at least one file format.", True
            if err.visible:
                return self.page.update()
            self.export_folder = Path(folder.value)
            self.prefs["export_folder"] = folder.value
            self._save_prefs()
            opts = ExportOptions(folder=self.export_folder, media=media.value,
                                 max_media_mb=float(size.value) or None, since=since_date, formats=formats)
            chosen = None if topic_dd.value == "all" else int(topic_dd.value)
            title = chat.title + (f" — {topic_names.get(chosen, '')}" if chosen is not None else "")
            await self.run_export(chat, chosen, opts, title)

        media.on_change = on_media
        topic_names: dict[int, str] = {}
        header = [ft.Row([T(chat.title, 22, ft.FontWeight.BOLD, max_lines=2, overflow=ft.TextOverflow.ELLIPSIS,
                            expand=True)]),
                  ft.Row([badge("Channel" if chat.kind == "channel" else "Group", ft.Colors.BLUE_GREY)]
                         + ([badge("Topics", ACCENT)] if chat.forum else [])
                         + ([badge(f"@{chat.username}", ft.Colors.BLUE_GREY)] if chat.username else []), spacing=6)]
        warn = [note("This chat restricts saving content. Its admins may not want it archived — please respect "
                     "their wishes and keep the archive private.", "warn")] if chat.protected else []
        folder_row = ft.Row([folder] + ([ft.OutlinedButton("Change…", icon=ft.Icons.FOLDER_OPEN,
                                                            on_click=change_folder)] if self.picker else []))
        options = ft.ExpansionTile(
            title=T("Options", 14, ft.FontWeight.W_600), leading=ft.Icon(ft.Icons.TUNE),
            controls=[ft.Container(ft.Column([folder_row, media, ft.Row([size, since], wrap=True),
                                              ft.Row(list(fmts.values()), wrap=True)], spacing=12),
                                   padding=ft.Padding.only(left=16, right=16, top=10, bottom=16))])
        go = ft.FilledButton("Download", icon=ft.Icons.DOWNLOAD, on_click=start, height=46,
                             style=ft.ButtonStyle(text_style=ft.TextStyle(size=15, weight=ft.FontWeight.W_600)))
        self.right.content = ft.Column(header + warn + ([ft.Row([topic_dd])] if chat.forum else []) + [
            muted(f"Will be saved to {self.export_folder}", 12),
            options, err, ft.Row([go]),
        ], spacing=16, scroll=ft.ScrollMode.AUTO)
        self.page.update()
        if chat.forum:
            topic_dd.disabled = True
            topic_dd.label = "Loading topics…"
            self.page.update()
            try:
                topics: list[Topic] = await self.be.topics(chat.id)
            except UserFacingError as ex:
                topics = []
                self.toast(str(ex))
            topic_names.update({t.id: t.title for t in topics})
            topic_dd.options = [ft.DropdownOption(key="all", text="All topics")] + \
                               [ft.DropdownOption(key=str(t.id), text=t.title) for t in topics]
            topic_dd.value = str(topic_id) if topic_id in topic_names else "all"
            topic_dd.label, topic_dd.disabled = "Topic", False
            self.page.update()

    # ---------- progress ----------
    async def run_export(self, chat: ChatInfo, topic_id: int | None, opts: ExportOptions, title: str) -> None:
        if self.busy():
            return
        self.exporting = True
        try:
            await self._run_export(chat, topic_id, opts, title)
        finally:
            self.exporting = False

    async def _run_export(self, chat: ChatInfo, topic_id: int | None, opts: ExportOptions, title: str) -> None:
        try:                                         # access may have expired or been revoked while open
            await self.be.verify_access()
        except AccessDenied as e:
            self.account = None
            self._set_header()
            self.exporting = False
            return self.show_accounts(str(e)) if e.reason != "wrong_account" \
                else await self.show_login(str(e), offer_reset=True)
        except UserFacingError as e:
            return self._export_failed(str(e), chat, topic_id, opts, title)
        stage = T("Starting…", 15, ft.FontWeight.W_600)
        detail = muted("")
        bar = ft.ProgressBar(value=None, border_radius=4, height=8)
        cancel = ft.OutlinedButton("Stop", icon=ft.Icons.STOP_CIRCLE_OUTLINED)
        self.right.content = centered(card(
            T(f"Archiving {title}", 20, ft.FontWeight.BOLD, max_lines=2, overflow=ft.TextOverflow.ELLIPSIS),
            stage, bar, detail,
            muted("You can stop at any time — what is saved stays saved, and the next run continues from there.", 12),
            ft.Row([cancel], alignment=ft.MainAxisAlignment.END), width=560))
        self.page.update()

        def on_progress(pr: Progress):
            if pr.stage == "messages":
                stage.value = "Reading messages…"
                detail.value = f"{pr.done:,} of ~{pr.total:,}" if pr.total else f"{pr.done:,}"
                bar.value = min(pr.done / pr.total, 1) if pr.total else None
            elif pr.stage == "media":
                stage.value = "Downloading photos and files…"
                detail.value = f"{pr.done:,} of {pr.total:,}" if pr.total else "Looking for files…"
                bar.value = pr.done / pr.total if pr.total else None
            elif pr.stage == "files":
                stage.value, detail.value, bar.value = "Building the web page and files…", "", None
            self.page.update()

        task = self._spawn(self.be.export(chat.id, topic_id, opts, on_progress))

        async def stop(e):
            task.cancel()

        cancel.on_click = stop
        try:
            result = await task
        except asyncio.CancelledError:
            if task.cancelled():
                return self._stopped(chat, topic_id, opts, title)
            raise
        except UserFacingError as ex:
            return self._export_failed(str(ex), chat, topic_id, opts, title)
        except Exception as ex:                       # never leave the user on a frozen progress screen
            return self._export_failed(friendly_error(ex), chat, topic_id, opts, title)
        self._done(result, title)

    def _again_btn(self, text, chat, topic_id, opts, title, icon=ft.Icons.REFRESH) -> ft.Control:
        async def again(e):
            await self.run_export(chat, topic_id, opts, title)
        return ft.FilledButton(text, icon=icon, on_click=again)

    def _back_btn(self) -> ft.Control:
        def back(e):
            self.right.content = self._placeholder()
            self.page.update()
        return ft.TextButton("Choose another chat", on_click=back)

    def _stopped(self, chat, topic_id, opts, title) -> None:
        self.right.content = centered(card(
            T("Stopped", 20, ft.FontWeight.BOLD),
            muted("Everything downloaded so far is saved. Continue to pick up where you left off.", 14),
            ft.Row([self._back_btn(), self._again_btn("Continue", chat, topic_id, opts, title, ft.Icons.PLAY_ARROW)],
                   alignment=ft.MainAxisAlignment.END), width=520))
        self.page.update()

    def _export_failed(self, text, chat, topic_id, opts, title) -> None:
        self.right.content = centered(card(
            T("Archiving stopped", 20, ft.FontWeight.BOLD), note(text, "error"),
            muted("Everything downloaded so far is saved. Trying again continues from there.", 13),
            ft.Row([self._back_btn(), self._again_btn("Try again", chat, topic_id, opts, title)],
                   alignment=ft.MainAxisAlignment.END), width=520))
        self.page.update()

    def _done(self, r: ExportResult, title: str) -> None:
        def opener(path: Path):
            def go(e):
                try:
                    os.startfile(str(path))           # noqa: S606 — opens the user's own archive
                except OSError as ex:
                    self.toast(f"Can't open: {ex}")
            return go

        files = f", {r.media_ok:,} files" if r.media_ok else ""
        failed = [note(f"{r.media_failed:,} files could not be downloaded. Run the archive again to retry them.",
                       "warn")] if r.media_failed else []
        actions = []
        if not self.page.web:
            if r.html:
                actions.append(ft.FilledButton("Open chat", icon=ft.Icons.OPEN_IN_NEW, on_click=opener(r.html)))
            actions.append(ft.OutlinedButton("Open folder", icon=ft.Icons.FOLDER_OPEN, on_click=opener(r.folder)))
        self.right.content = centered(card(
            ft.Row([ft.Icon(ft.Icons.CHECK_CIRCLE, color=ft.Colors.GREEN, size=30), T("Done!", 22, ft.FontWeight.BOLD)],
                   spacing=10),
            T(title, 15, ft.FontWeight.W_600),
            muted(f"{r.messages:,} messages{files} saved to:", 14),
            ft.Text(str(r.folder), size=12, selectable=True, font_family="Consolas"),
            *failed,
            ft.Row(actions + [self._back_btn()], wrap=True),
            width=560))
        self.page.update()

    # ---------- dialogs ----------
    def show_about(self) -> None:
        self.page.show_dialog(ft.AlertDialog(
            title=ft.Row([ft.Icon(ft.Icons.ARCHIVE_ROUNDED, color=ACCENT), T(f"{APP_NAME} {__version__}", 18,
                                                                             ft.FontWeight.BOLD)]),
            content=ft.Container(ft.Column([
                muted("An independent app that uses the Telegram API to archive chats you are a member of. "
                      "It is not affiliated with or endorsed by Telegram.", 14),
                muted("Read-only: it never sends messages, deletes anything or marks chats as read. The only "
                      "exception is joining a group from an invite link, and only after you confirm.", 14),
                muted(f"Your sign-in is stored encrypted on this computer:\n{self.home}", 13),
                muted("Please keep archives private and don't use them to train AI models.", 14),
            ], spacing=10, tight=True), width=460),
            actions=[ft.TextButton("Close", on_click=lambda e: self.page.pop_dialog())]))

    def confirm_logout(self) -> None:
        async def do(e):
            self.page.pop_dialog()
            self.show_loading("Logging out…")
            await self.be.logout()
            self.show_accounts()

        self.page.show_dialog(ft.AlertDialog(
            modal=True, title=T("Log out?", 18, ft.FontWeight.BOLD),
            content=muted("This account will be signed out of Telegram on this computer, but stays approved — you "
                          "can sign in again or add another account. Archives already saved stay on your disk.", 14),
            actions=[ft.TextButton("Cancel", on_click=lambda e: self.page.pop_dialog()),
                     ft.FilledButton("Log out", on_click=do)]))
