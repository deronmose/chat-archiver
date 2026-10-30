"""Chat Archiver Admin window (Flet 1.0).

One `Hub` per process owns the service and the listen loop; every window/browser
session subscribes to it, so a reload or a second tab sees the same live state.
The bot token is never shown: token fields are masked and start empty.
"""
from __future__ import annotations

import asyncio
import contextlib
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Awaitable, Callable

import flet as ft

from . import APP_NAME
from .botapi import BotApiError
from .service import MIN_BACKUP_PASSWORD, AdminError, AdminService, listen_loop

PAGE_SIZE = 25
SEED = ft.Colors.INDIGO

C = ft.Colors


# ---------- shared state ----------

class Hub:
    """Process-wide owner of the service and the listen loop."""

    def __init__(self, svc_factory: Callable[[], AdminService], *, demo: bool = False,
                 demo_defaults: dict | None = None, build_dir: Path | None = None,
                 reset_data: Callable[[], None] | None = None):
        self.svc_factory = svc_factory
        self.demo = demo
        self.demo_defaults = demo_defaults or {}
        self.build_dir = build_dir or (Path.cwd() / "build")
        self.reset_data = reset_data
        self.svc: AdminService | None = None
        self.load_error: str | None = None
        self.status: tuple[str, str | None] = ("stopped", None)
        self.task: asyncio.Task | None = None
        self.subscribers: list[Callable[[str, Any], None]] = []
        self.reload()

    def reload(self) -> None:
        try:
            self.svc, self.load_error = self.svc_factory(), None
        except AdminError as e:
            self.svc, self.load_error = None, str(e)

    def subscribe(self, cb: Callable[[str, Any], None]) -> None:
        if cb not in self.subscribers:
            self.subscribers.append(cb)

    def unsubscribe(self, cb) -> None:
        with contextlib.suppress(ValueError):
            self.subscribers.remove(cb)

    def broadcast(self, event: str, data: Any = None) -> None:
        for cb in list(self.subscribers):
            try:
                cb(event, data)
            except Exception as e:
                # A closed browser tab or window: drop it. Log only the exception type (no secrets).
                print(f"[admin] session dropped after {event!r}: {type(e).__name__}", file=sys.stderr, flush=True)
                self.unsubscribe(cb)

    @property
    def listening(self) -> bool:
        return self.task is not None and not self.task.done()

    def _set_status(self, state: str, message: str | None) -> None:
        self.status = (state, message)
        self.broadcast("status")

    def _on_new(self, reqs) -> None:
        self.broadcast("new", reqs)

    async def start(self) -> None:
        if self.listening or self.svc is None:
            return
        self.task = asyncio.create_task(listen_loop(self.svc, self._set_status, self._on_new))

    async def stop(self) -> None:
        task, self.task = self.task, None
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        self._set_status("stopped", None)


# ---------- helpers ----------

def fmt_time(ts: int | None, *, relative: bool = False) -> str:
    if not ts:
        return "—"
    dt = datetime.fromtimestamp(ts)
    now = datetime.now()
    if relative:
        secs = (now - dt).total_seconds()
        if 0 <= secs < 60:
            return "just now"
        if 0 <= secs < 3600:
            return f"{int(secs // 60)} min ago"
    if dt.date() == now.date():
        return f"Today, {dt:%H:%M}"
    return f"{dt:%d %b %Y}, {dt:%H:%M}"


def fmt_date(ts: int) -> str:
    return f"{datetime.fromtimestamp(ts):%d %b %Y}"


def initials(name: str) -> str:
    parts = [p for p in (name or "").replace("@", "").split() if p]
    return ("".join(p[0] for p in parts[:2]) or "?").upper()


def muted(text: str, **kw) -> ft.Text:
    return ft.Text(text, color=C.ON_SURFACE_VARIANT, size=kw.pop("size", 13), **kw)


def link(text: str, url: str) -> ft.TextSpan:
    return ft.TextSpan(text, url=url, style=ft.TextStyle(color=C.PRIMARY, decoration=ft.TextDecoration.UNDERLINE))


def section(title: str, *controls: ft.Control, icon: str | None = None, subtitle: str | None = None) -> ft.Container:
    title_row = ft.Row(([ft.Icon(icon, color=C.PRIMARY, size=20)] if icon else []) +
                       [ft.Text(title, size=16, weight=ft.FontWeight.W_600)], spacing=8)
    items: list[ft.Control] = [title_row]
    if subtitle:
        items.append(muted(subtitle))
    items.extend(controls)
    return ft.Container(
        content=ft.Column(items, spacing=12, horizontal_alignment=ft.CrossAxisAlignment.STRETCH),
        padding=20, border_radius=14,
        bgcolor=C.SURFACE_CONTAINER_LOW,
        border=ft.Border.all(1, C.OUTLINE_VARIANT),
    )


def empty_state(icon: str, title: str, text: str, action: ft.Control | None = None) -> ft.Container:
    items: list[ft.Control] = [
        ft.Icon(icon, size=56, color=C.OUTLINE),
        ft.Text(title, size=18, weight=ft.FontWeight.W_600),
        ft.Text(text, color=C.ON_SURFACE_VARIANT, text_align=ft.TextAlign.CENTER, width=440),
    ]
    if action is not None:
        items.append(ft.Container(action, margin=ft.Margin.only(top=8)))
    return ft.Container(
        content=ft.Column(items, horizontal_alignment=ft.CrossAxisAlignment.CENTER, spacing=8),
        alignment=ft.Alignment.CENTER, padding=ft.Padding.symmetric(vertical=60, horizontal=20),
    )


def chip(text: str, icon: str | None = None, color: str = C.ON_SURFACE_VARIANT,
         bg: str | None = None) -> ft.Container:
    row: list[ft.Control] = []
    if icon:
        row.append(ft.Icon(icon, size=14, color=color))
    row.append(ft.Text(text, size=12, color=color, weight=ft.FontWeight.W_500))
    return ft.Container(ft.Row(row, spacing=4, tight=True),
                        padding=ft.Padding.symmetric(horizontal=8, vertical=3), border_radius=20,
                        bgcolor=bg or C.SURFACE_CONTAINER_HIGHEST)


# ---------- the window ----------

class AdminApp:
    def __init__(self, page: ft.Page, hub: Hub):
        self.page = page
        self.hub = hub
        self.view = 0                      # 0 requests, 1 approved, 2 history, 3 settings
        self.limits = {0: PAGE_SIZE, 1: PAGE_SIZE, 2: 50}
        self.clipboard = ft.Clipboard()

    @property
    def svc(self) -> AdminService:
        assert self.hub.svc is not None
        return self.hub.svc

    def notify(self, event: str) -> None:
        """Tell every open session (this one included) that the state changed."""
        self.hub.subscribe(self.on_hub_event)   # self-heal if this session was dropped
        self.hub.broadcast(event)

    # ---- lifecycle ----
    def start(self) -> None:
        page = self.page
        page.title = APP_NAME
        page.theme = ft.Theme(color_scheme_seed=SEED)
        page.dark_theme = ft.Theme(color_scheme_seed=SEED)
        page.theme_mode = ft.ThemeMode.SYSTEM
        page.padding = 0
        page.spacing = 0
        with contextlib.suppress(Exception):
            page.window.width, page.window.height = 1120, 780
            page.window.min_width, page.window.min_height = 860, 600
        self.hub.subscribe(self.on_hub_event)
        # A disconnect can be a short websocket reconnect; only a closed session is gone for good.
        page.on_close = lambda e: self.hub.unsubscribe(self.on_hub_event)
        page.on_connect = self.on_reconnect
        self.render()

    def on_reconnect(self, _=None) -> None:
        self.hub.subscribe(self.on_hub_event)
        with contextlib.suppress(Exception):
            self.render()

    def on_hub_event(self, event: str, data: Any) -> None:
        if event == "new" and data:
            names = ", ".join(r.display_name for r in data[:3])
            more = f" and {len(data) - 3} more" if len(data) > 3 else ""
            self.toast(f"New access request: {names}{more}")
        if event == "reload":
            self.render()
            return
        if self.hub.svc is not None and self.svc.is_configured:
            # Settings has forms: rebuild it only when the user acts there, not on background events.
            self.refresh_main(body=self.view != 3 or event == "changed")
        self.page.update()

    def render(self) -> None:
        self.page.controls.clear()
        if self.hub.svc is None:
            self.page.add(self.build_load_error())
        elif not self.svc.is_configured:
            self.page.add(self.build_setup())
        else:
            self.page.add(self.build_main())
            self.refresh_main()
        self.page.update()

    # ---- small UI services ----
    def toast(self, text: str, error: bool = False) -> None:
        self.page.show_dialog(ft.SnackBar(
            content=ft.Text(text, color=C.ON_ERROR_CONTAINER if error else None),
            bgcolor=C.ERROR_CONTAINER if error else None,
            behavior=ft.SnackBarBehavior.FLOATING, show_close_icon=True,
            duration=ft.Duration(seconds=6 if error else 4)))

    async def copy(self, text: str, what: str) -> None:
        try:
            await self.clipboard.set(text)
            self.toast(f"{what} copied to the clipboard.")
        except Exception:
            self.toast("Could not access the clipboard. Select the text and copy it manually.", error=True)

    def confirm(self, title: str, body: str | ft.Control, confirm_label: str,
                action: Callable[[], Awaitable[str | None]], *, destructive: bool = False,
                icon: str | None = None) -> None:
        """Modal confirmation. `action` returns a success toast text; AdminError stays in the dialog."""
        err = ft.Text("", color=C.ERROR, visible=False)
        ring = ft.ProgressRing(width=18, height=18, stroke_width=2, visible=False)
        content = ft.Column([body if isinstance(body, ft.Control) else ft.Text(body), err],
                            tight=True, spacing=12, width=440)
        cancel = ft.TextButton("Cancel")
        ok = ft.FilledButton(confirm_label,
                             style=ft.ButtonStyle(bgcolor=C.ERROR, color=C.ON_ERROR) if destructive else None)

        dlg: ft.AlertDialog | None = None

        def close(_=None):
            # Close this dialog, not the topmost one (a toast may be on top of it).
            if dlg is not None and dlg.open:
                dlg.open = False
                dlg.update()

        async def run(_):
            ok.disabled = cancel.disabled = True
            ring.visible, err.visible = True, False
            self.page.update()
            try:
                msg = await action()
            except (AdminError, BotApiError) as e:
                err.value, err.visible = str(e), True
                ok.disabled = cancel.disabled = False
                ring.visible = False
                ok.content = "Try again"
                self.page.update()
                return
            close()
            if msg:
                self.toast(msg)
            self.notify("changed")

        cancel.on_click = close
        ok.on_click = run
        dlg = ft.AlertDialog(
            modal=True, icon=ft.Icon(icon) if icon else None, title=ft.Text(title),
            content=content, actions=[ring, cancel, ok],
            actions_alignment=ft.MainAxisAlignment.END)
        self.page.show_dialog(dlg)

    # ---- load error ----
    def build_load_error(self) -> ft.Control:
        async def retry(_):
            self.hub.reload()
            self.notify("reload")

        async def reset():
            if self.hub.reset_data:
                self.hub.reset_data()
            self.hub.reload()
            self.notify("reload")
            return "Saved data removed. Set up the Admin again."

        return ft.Container(
            empty_state(ft.Icons.ERROR_OUTLINE, "Cannot open the Admin data",
                        self.hub.load_error or "Unknown error.",
                        ft.Row([ft.FilledButton("Try again", on_click=retry),
                                ft.OutlinedButton("Start over…", on_click=lambda e: self.confirm(
                                    "Start over?",
                                    "This deletes the saved bot token, API keys, requests and the owner key. "
                                    "Builds made with the old owner key will no longer accept new codes.",
                                    "Delete and start over", reset, destructive=True))],
                               alignment=ft.MainAxisAlignment.CENTER)),
            expand=True, alignment=ft.Alignment.CENTER)

    # ---- setup ----
    def build_setup(self, *, embedded: bool = False) -> ft.Control:
        d = self.hub.demo_defaults
        token = ft.TextField(label="Bot token", password=True, can_reveal_password=False,
                             hint_text="123456789:AA…", value=d.get("token", ""), prefix_icon=ft.Icons.SMART_TOY_OUTLINED)
        api_id = ft.TextField(label="api_id", value=d.get("api_id", ""), keyboard_type=ft.KeyboardType.NUMBER,
                              input_filter=ft.NumbersOnlyInputFilter(), expand=1)
        api_hash = ft.TextField(label="api_hash", password=True, can_reveal_password=True,
                                value=d.get("api_hash", ""), hint_text="32 characters, 0-9 a-f", expand=2)
        err = ft.Text("", color=C.ERROR, visible=False)
        ring = ft.ProgressRing(width=18, height=18, stroke_width=2, visible=False)
        btn = ft.FilledButton("Connect", icon=ft.Icons.LINK)

        async def connect(_):
            err.visible = False
            btn.disabled, ring.visible = True, True
            btn.content = "Connecting…"
            self.page.update()
            try:
                await self.svc.setup(token.value or "", api_id.value or "", api_hash.value or "")
            except (AdminError, BotApiError) as e:
                err.value, err.visible = str(e), True
                btn.disabled, ring.visible = False, False
                btn.content = "Connect"
                self.page.update()
                return
            token.value = ""
            self.toast(f"Connected to @{self.svc.bot_username}.")
            self.notify("reload")

        btn.on_click = connect
        for f in (token, api_id, api_hash):
            f.on_submit = connect

        steps = ft.Column([
            ft.Text(spans=[ft.TextSpan("1. ", ft.TextStyle(weight=ft.FontWeight.W_600)),
                           ft.TextSpan("In Telegram, open "), link("@BotFather", "https://t.me/BotFather"),
                           ft.TextSpan(", send /newbot and paste the token it gives you.")]),
            ft.Text(spans=[ft.TextSpan("2. ", ft.TextStyle(weight=ft.FontWeight.W_600)),
                           ft.TextSpan("Sign in at "), link("my.telegram.org", "https://my.telegram.org"),
                           ft.TextSpan(", open API development tools and copy api_id and api_hash.")]),
            ft.Text(spans=[ft.TextSpan("3. ", ft.TextStyle(weight=ft.FontWeight.W_600)),
                           ft.TextSpan("Click Connect. Everything is stored encrypted on this computer "
                                       "(Windows DPAPI) and never put into the Chat Archiver build.")]),
        ], spacing=6)

        items: list[ft.Control] = []
        if self.hub.demo:
            items.append(self.demo_banner())
        items += [
            ft.Row([ft.Container(ft.Icon(ft.Icons.ARCHIVE_OUTLINED, color=C.ON_PRIMARY_CONTAINER, size=28),
                                 bgcolor=C.PRIMARY_CONTAINER, border_radius=12, padding=10),
                    ft.Column([ft.Text("Set up Chat Archiver Admin", size=22, weight=ft.FontWeight.W_700),
                               muted("Connect your bot once. People will request access through it.")],
                              spacing=2, tight=True)], spacing=14),
            steps,
            token,
            ft.Row([api_id, api_hash], spacing=12),
            err,
            ft.Row([ring, btn], alignment=ft.MainAxisAlignment.END),
        ]
        card = ft.Container(ft.Column(items, spacing=16, tight=True,
                                      horizontal_alignment=ft.CrossAxisAlignment.STRETCH), width=600, padding=28, border_radius=18,
                            bgcolor=C.SURFACE_CONTAINER_LOW, border=ft.Border.all(1, C.OUTLINE_VARIANT))
        return ft.Container(ft.Column([card], horizontal_alignment=ft.CrossAxisAlignment.CENTER,
                                      scroll=ft.ScrollMode.AUTO),
                            expand=True, alignment=ft.Alignment.CENTER, padding=24)

    def demo_banner(self) -> ft.Control:
        return ft.Container(
            ft.Row([ft.Icon(ft.Icons.SCIENCE_OUTLINED, color=C.ON_TERTIARY_CONTAINER, size=18),
                    ft.Text("Demo mode: a simulated bot, nothing is sent to Telegram. "
                            "Data is temporary and removed on exit.",
                            color=C.ON_TERTIARY_CONTAINER, size=13, expand=True)], spacing=8),
            bgcolor=C.TERTIARY_CONTAINER, border_radius=10, padding=ft.Padding.symmetric(horizontal=12, vertical=8))

    # ---- main ----
    def build_main(self) -> ft.Control:
        self.status_dot = ft.Container(width=10, height=10, border_radius=5)
        self.status_text = ft.Text("", size=13, weight=ft.FontWeight.W_500)
        self.status_ring = ft.ProgressRing(width=12, height=12, stroke_width=2, visible=False)
        self.status_pill = ft.Container(ft.Row([self.status_dot, self.status_ring, self.status_text],
                                               spacing=8, tight=True),
                                        padding=ft.Padding.symmetric(horizontal=12, vertical=6), border_radius=20)
        self.listen_switch = ft.Switch(label="Listen", value=self.hub.listening, on_change=self.on_listen)
        self.error_bar_text = ft.Text("", color=C.ON_ERROR_CONTAINER, size=13, expand=True, selectable=True)
        self.error_bar = ft.Container(
            ft.Row([ft.Icon(ft.Icons.ERROR_OUTLINE, color=C.ON_ERROR_CONTAINER, size=18), self.error_bar_text],
                   spacing=8),
            bgcolor=C.ERROR_CONTAINER, padding=ft.Padding.symmetric(horizontal=16, vertical=10), visible=False)
        self.theme_btn = ft.IconButton(on_click=self.cycle_theme)
        self.sync_theme_button()

        header = ft.Container(
            ft.Row([
                ft.Container(ft.Icon(ft.Icons.ARCHIVE_OUTLINED, color=C.ON_PRIMARY_CONTAINER, size=22),
                             bgcolor=C.PRIMARY_CONTAINER, border_radius=10, padding=8),
                ft.Column([ft.Text(APP_NAME, size=17, weight=ft.FontWeight.W_700),
                           muted(f"@{self.svc.bot_username}" + ("  ·  demo" if self.hub.demo else ""), size=12)],
                          spacing=0, tight=True),
                ft.Container(expand=True),
                ft.OutlinedButton("Export build config", icon=ft.Icons.FILE_DOWNLOAD_OUTLINED,
                                  on_click=lambda e: self.export_build_dialog()),
                ft.Container(width=8),
                self.status_pill,
                self.listen_switch,
                self.theme_btn,
            ], spacing=12, vertical_alignment=ft.CrossAxisAlignment.CENTER),
            padding=ft.Padding.symmetric(horizontal=20, vertical=12),
            bgcolor=C.SURFACE_CONTAINER_LOW,
        )
        self.rail = ft.NavigationRail(
            selected_index=self.view, label_type=ft.NavigationRailLabelType.ALL, min_width=96,
            group_alignment=-1.0, on_change=self.on_nav, destinations=[])
        self.body = ft.Column([], expand=True, scroll=ft.ScrollMode.AUTO, spacing=12)
        content = ft.Container(self.body, expand=True, padding=ft.Padding.symmetric(horizontal=28, vertical=20))
        top: list[ft.Control] = [header, ft.Divider(height=1, thickness=1)]
        if self.hub.demo:
            top.append(ft.Container(self.demo_banner(), padding=ft.Padding.only(left=16, right=16, top=10)))
        top.append(self.error_bar)
        return ft.Column(top + [ft.Row([self.rail, ft.VerticalDivider(width=1, thickness=1), content],
                                       expand=True, spacing=0, vertical_alignment=ft.CrossAxisAlignment.START)],
                         expand=True, spacing=0)

    def refresh_main(self, body: bool = True) -> None:
        if not hasattr(self, "body"):
            return
        self.sync_status()
        n_pending = len(self.svc.pending())

        def dest(icon, sel, label, count=0):
            return ft.NavigationRailDestination(
                icon=ft.Icon(icon, badge=str(count) if count else None),
                selected_icon=ft.Icon(sel, badge=str(count) if count else None), label=label)

        self.rail.destinations = [
            dest(ft.Icons.INBOX_OUTLINED, ft.Icons.INBOX, "Requests", n_pending),
            dest(ft.Icons.VERIFIED_USER_OUTLINED, ft.Icons.VERIFIED_USER, "Approved"),
            dest(ft.Icons.HISTORY, ft.Icons.HISTORY, "History"),
            dest(ft.Icons.SETTINGS_OUTLINED, ft.Icons.SETTINGS, "Settings"),
        ]
        self.rail.selected_index = self.view
        if body:
            builders = [self.view_requests, self.view_approved, self.view_history, self.view_settings]
            self.body.controls = builders[self.view]()

    def sync_status(self) -> None:
        state, message = self.hub.status
        if state == "starting":
            color, label = C.AMBER, "Connecting…"
        elif state == "listening":
            color, label = C.GREEN, "Listening"
        elif state == "error":
            color, label = C.ERROR, "Error"
        else:
            color, label = C.OUTLINE, "Stopped"
        self.status_dot.bgcolor = color
        self.status_dot.visible = state != "starting"
        self.status_ring.visible = state == "starting"
        self.status_text.value = label
        self.status_text.color = C.ERROR if state == "error" else None
        self.status_pill.bgcolor = C.with_opacity(0.12, color)
        self.listen_switch.value = self.hub.listening
        self.error_bar.visible = state == "error"
        self.error_bar_text.value = (message or "") + "  You can keep working; the Admin reconnects by itself."

    async def on_listen(self, e) -> None:
        if e.control.value:
            await self.hub.start()
        else:
            await self.hub.stop()
        self.refresh_main()
        self.page.update()

    async def start_listening(self, _=None) -> None:
        await self.hub.start()
        self.refresh_main()
        self.page.update()

    def on_nav(self, e) -> None:
        self.view = int(e.control.selected_index)
        self.refresh_main()
        self.page.update()

    def cycle_theme(self, _=None) -> None:
        order = [ft.ThemeMode.SYSTEM, ft.ThemeMode.LIGHT, ft.ThemeMode.DARK]
        cur = self.page.theme_mode or ft.ThemeMode.SYSTEM
        self.page.theme_mode = order[(order.index(cur) + 1) % 3]
        self.sync_theme_button()
        self.page.update()

    def sync_theme_button(self) -> None:
        mode = self.page.theme_mode or ft.ThemeMode.SYSTEM
        icon, tip = {ft.ThemeMode.SYSTEM: (ft.Icons.BRIGHTNESS_AUTO_OUTLINED, "Theme: system (click for light)"),
                     ft.ThemeMode.LIGHT: (ft.Icons.LIGHT_MODE_OUTLINED, "Theme: light (click for dark)"),
                     ft.ThemeMode.DARK: (ft.Icons.DARK_MODE_OUTLINED, "Theme: dark (click for system)")}[mode]
        self.theme_btn.icon, self.theme_btn.tooltip = icon, tip

    def page_header(self, title: str, subtitle: str, *actions: ft.Control) -> ft.Control:
        return ft.Row([ft.Column([ft.Text(title, size=22, weight=ft.FontWeight.W_700), muted(subtitle)],
                                 spacing=2, expand=True),
                       *actions], vertical_alignment=ft.CrossAxisAlignment.CENTER)

    def more_button(self, view: int, left: int) -> ft.Control:
        def more(_):
            self.limits[view] += PAGE_SIZE
            self.refresh_main()
            self.page.update()
        return ft.Container(ft.TextButton(f"Show more ({left} more)", icon=ft.Icons.EXPAND_MORE, on_click=more),
                            alignment=ft.Alignment.CENTER)

    def person_card(self, name: str, lines: list[ft.Control], actions: list[ft.Control]) -> ft.Control:
        return ft.Container(
            ft.Row([
                ft.CircleAvatar(content=ft.Text(initials(name), weight=ft.FontWeight.W_600),
                                bgcolor=C.SECONDARY_CONTAINER, color=C.ON_SECONDARY_CONTAINER, radius=22),
                ft.Column([ft.Text(name, size=15, weight=ft.FontWeight.W_600, max_lines=1,
                                   overflow=ft.TextOverflow.ELLIPSIS), *lines], spacing=4, expand=True),
                *actions,
            ], spacing=14, vertical_alignment=ft.CrossAxisAlignment.CENTER),
            padding=ft.Padding.symmetric(horizontal=16, vertical=14), border_radius=14,
            bgcolor=C.SURFACE_CONTAINER_LOW, border=ft.Border.all(1, C.OUTLINE_VARIANT))

    @staticmethod
    def who(username: str, user_id: int) -> str:
        return (f"@{username}  ·  " if username else "no @username  ·  ") + f"ID {user_id}"

    # ---- Requests ----
    def view_requests(self) -> list[ft.Control]:
        pending = self.svc.pending()
        clear = ft.TextButton("Clear all", icon=ft.Icons.CLEANING_SERVICES_OUTLINED, visible=bool(pending),
                              on_click=lambda e: self.confirm(
                                  "Clear all pending requests?",
                                  f"{len(pending)} request(s) will be removed without replying. Use this for spam. "
                                  "People can still send a new request later.",
                                  "Clear all", self._clear_pending, destructive=True))
        out: list[ft.Control] = [self.page_header(
            "Pending requests",
            f"{len(pending)} waiting for your decision" if pending else "People who pressed Start in your bot",
            clear)]
        if not pending:
            if self.hub.listening:
                out.append(empty_state(ft.Icons.INBOX_OUTLINED, "Listening for requests…",
                                       "New requests appear here automatically. People request access from "
                                       "Chat Archiver with the Request access button."))
            else:
                out.append(empty_state(ft.Icons.INBOX_OUTLINED, "No requests yet",
                                       "Click Listen to start receiving requests from your bot. Requests sent "
                                       "while the Admin was closed are kept by Telegram for about 24 hours.",
                                       ft.FilledButton("Start listening", icon=ft.Icons.PLAY_ARROW,
                                                       on_click=self.start_listening)))
            return out
        limit = self.limits[0]
        for r in pending[:limit]:
            out.append(self.person_card(r.display_name, [
                muted(self.who(r.username, r.user_id)),
                ft.Row([chip(r.device_label, ft.Icons.COMPUTER), muted(fmt_time(r.created_at, relative=True), size=12)],
                       spacing=10),
            ], [
                ft.OutlinedButton("Deny", icon=ft.Icons.BLOCK, on_click=lambda e, r=r: self.ask_deny(r)),
                ft.FilledButton("Approve", icon=ft.Icons.CHECK, on_click=lambda e, r=r: self.ask_approve(r)),
            ]))
        if len(pending) > limit:
            out.append(self.more_button(0, len(pending) - limit))
        return out

    async def _clear_pending(self) -> str:
        n = self.svc.dismiss_all_pending()
        return f"Cleared {n} request(s)."

    def ask_approve(self, r) -> None:
        body = ft.Column([
            ft.Text(spans=[ft.TextSpan(r.display_name, ft.TextStyle(weight=ft.FontWeight.W_600)),
                           ft.TextSpan(f"  ({self.who(r.username, r.user_id)})")]),
            ft.Text("The bot will send them an activation code with a Copy button. The code works only on "
                    f"{r.device_label} and only for this Telegram account, for 14 days. "
                    "Approve only people you trust: they can use your API keys."),
        ], spacing=8, tight=True)

        async def act():
            await self.svc.approve(r.id)
            return f"Access granted to {r.display_name}. The code was sent by the bot."

        self.confirm(f"Grant access to {r.display_name} on {r.device_label}?", body, "Approve", act,
                     icon=ft.Icons.HOW_TO_REG)

    def ask_deny(self, r) -> None:
        async def act():
            await self.svc.deny(r.id)
            return f"Request from {r.display_name} denied."

        self.confirm(f"Deny {r.display_name} on {r.device_label}?",
                     "The bot will politely tell them the request was not approved. They can ask again later.",
                     "Deny", act, destructive=True, icon=ft.Icons.BLOCK)

    # ---- Approved ----
    def view_approved(self) -> list[ft.Control]:
        approved = self.svc.approved()
        now = datetime.now().timestamp()
        out: list[ft.Control] = [self.page_header(
            "Approved devices",
            f"{len(approved)} device(s) with access" if approved else "People you gave access to")]
        if not approved:
            out.append(empty_state(ft.Icons.VERIFIED_USER_OUTLINED, "Nobody has access yet",
                                   "Approve a request to give someone access for 14 days. "
                                   "You can revoke it here at any time."))
            return out
        limit = self.limits[1]
        for a in approved[:limit]:
            expired = a.is_expired(now)
            status = (chip(f"Expired {fmt_date(a.expires_at)}", ft.Icons.TIMER_OFF_OUTLINED, C.ON_SURFACE_VARIANT)
                      if expired else
                      chip(f"Valid until {fmt_date(a.expires_at)}", ft.Icons.CHECK_CIRCLE_OUTLINE,
                           C.ON_PRIMARY_CONTAINER, C.PRIMARY_CONTAINER))
            out.append(self.person_card(a.display_name, [
                muted(self.who(a.username, a.user_id)),
                ft.Row([chip(a.device_label, ft.Icons.COMPUTER), status,
                        muted(f"Approved {fmt_time(a.approved_at)}", size=12)], spacing=10, wrap=True),
            ], [ft.OutlinedButton("Revoke", icon=ft.Icons.REMOVE_MODERATOR_OUTLINED,
                                  style=ft.ButtonStyle(color=C.ERROR),
                                  on_click=lambda e, a=a: self.ask_revoke(a))]))
        if len(approved) > limit:
            out.append(self.more_button(1, len(approved) - limit))
        return out

    def ask_revoke(self, a) -> None:
        async def act():
            await self.svc.revoke(a.id)
            return f"Access revoked for {a.display_name} on {a.device_label}."

        self.confirm(f"Revoke access for {a.display_name} on {a.device_label}?",
                     "The bot will send them a signed revoke notice. Chat Archiver on that computer deletes "
                     "its keys the next time it starts.", "Revoke", act, destructive=True,
                     icon=ft.Icons.REMOVE_MODERATOR_OUTLINED)

    # ---- History ----
    HIST_ICONS = {
        "request": (ft.Icons.PERSON_ADD_ALT_1_OUTLINED, C.PRIMARY),
        "approved": (ft.Icons.CHECK_CIRCLE_OUTLINE, C.GREEN),
        "denied": (ft.Icons.BLOCK, C.ERROR),
        "revoked": (ft.Icons.REMOVE_MODERATOR_OUTLINED, C.ERROR),
        "dismissed": (ft.Icons.CLEANING_SERVICES_OUTLINED, C.ON_SURFACE_VARIANT),
        "setup": (ft.Icons.SMART_TOY_OUTLINED, C.PRIMARY),
        "key": (ft.Icons.KEY_OUTLINED, C.TERTIARY),
        "error": (ft.Icons.ERROR_OUTLINE, C.ERROR),
        "info": (ft.Icons.INFO_OUTLINE, C.ON_SURFACE_VARIANT),
    }

    def view_history(self) -> list[ft.Control]:
        events = self.svc.history()

        async def clear():
            self.svc.clear_history()
            return "History cleared."

        out: list[ft.Control] = [self.page_header(
            "History", "Every request and decision, newest first",
            ft.TextButton("Clear history", icon=ft.Icons.DELETE_SWEEP_OUTLINED, visible=bool(events),
                          on_click=lambda e: self.confirm("Clear history?",
                                                          "The event log is deleted. Requests and approvals stay.",
                                                          "Clear", clear, destructive=True)))]
        if not events:
            out.append(empty_state(ft.Icons.HISTORY, "No events yet", "Requests, approvals and revokes appear here."))
            return out
        limit = self.limits[2]
        rows: list[ft.Control] = []
        for ev in events[:limit]:
            icon, color = self.HIST_ICONS.get(ev.kind, self.HIST_ICONS["info"])
            rows.append(ft.Container(
                ft.Row([ft.Icon(icon, color=color, size=20),
                        ft.Text(ev.text, expand=True, selectable=True),
                        muted(fmt_time(ev.ts), size=12)], spacing=12),
                padding=ft.Padding.symmetric(horizontal=16, vertical=10)))
            rows.append(ft.Divider(height=1, thickness=1, color=C.OUTLINE_VARIANT))
        if rows:
            rows.pop()
        out.append(ft.Container(ft.Column(rows, spacing=0), border_radius=14,
                                bgcolor=C.SURFACE_CONTAINER_LOW, border=ft.Border.all(1, C.OUTLINE_VARIANT)))
        if len(events) > limit:
            out.append(self.more_button(2, len(events) - limit))
        return out

    # ---- build config ----
    def default_build_path(self) -> Path:
        return self.hub.build_dir / "approval.json"

    def export_build_dialog(self) -> None:
        path = ft.TextField(label="Save to", value=str(self.default_build_path()), width=440)

        async def act():
            saved = self.svc.export_build_config(path.value.strip() or self.default_build_path())
            return f"Build config saved to {saved}"

        self.confirm("Export build config",
                     ft.Column([ft.Text("Chat Archiver needs this file to build. It contains only public data: "
                                        "the bot name, the bot id and your public key. No token, no API keys."),
                                path], spacing=12, tight=True),
                     "Export", act, icon=ft.Icons.FILE_DOWNLOAD_OUTLINED)

    # ---- Settings ----
    def view_settings(self) -> list[ft.Control]:
        out: list[ft.Control] = [self.page_header("Settings", "Bot, API keys, build config and the owner key")]
        out.append(self.settings_bot())
        out.append(self.settings_build())
        out.append(self.settings_owner_key())
        out.append(self.settings_appearance())
        return out

    def kv(self, k: str, v: str) -> ft.Control:
        return ft.Row([ft.Text(k, width=150, color=C.ON_SURFACE_VARIANT), ft.Text(v, selectable=True, expand=True)])

    def settings_bot(self) -> ft.Control:
        token = ft.TextField(label="New bot token", password=True, can_reveal_password=False,
                             hint_text="Leave empty to keep the current token")
        api_id = ft.TextField(label="api_id", value=str(self.svc.api_id or ""),
                              input_filter=ft.NumbersOnlyInputFilter(), expand=1)
        api_hash = ft.TextField(label="New api_hash", password=True, can_reveal_password=True,
                                hint_text="Leave empty to keep", expand=2)
        err = ft.Text("", color=C.ERROR, visible=False)
        ring = ft.ProgressRing(width=18, height=18, stroke_width=2, visible=False)
        btn = ft.FilledButton("Save changes", icon=ft.Icons.SAVE_OUTLINED)

        async def save(_):
            err.visible, ring.visible, btn.disabled = False, True, True
            self.page.update()
            was_listening = self.hub.listening
            try:
                if was_listening and (token.value or "").strip():
                    await self.hub.stop()
                await self.svc.update_settings(token.value, api_id.value, api_hash.value)
            except (AdminError, BotApiError) as e:
                err.value, err.visible = str(e), True
                ring.visible, btn.disabled = False, False
                self.page.update()
                return
            if was_listening and not self.hub.listening:
                await self.hub.start()
            self.toast("Settings saved.")
            self.notify("reload")

        btn.on_click = save
        return section(
            "Bot & API keys",
            self.kv("Bot", f"@{self.svc.bot_username}  (id {self.svc.bot_id})"),
            self.kv("Bot token", "•••••••• stored encrypted"),
            self.kv("api_hash", "•••••••• stored encrypted"),
            token, ft.Row([api_id, api_hash], spacing=12), err,
            ft.Row([ring, btn], alignment=ft.MainAxisAlignment.END),
            icon=ft.Icons.SMART_TOY_OUTLINED,
            subtitle="Change the token after /revoke in @BotFather, or switch to other API keys. "
                     "Empty fields keep their current values.")

    def settings_build(self) -> ft.Control:
        path = ft.TextField(label="Save to", value=str(self.default_build_path()), expand=True)
        result = ft.Text("", selectable=True, visible=False, color=C.PRIMARY)

        def export(_):
            try:
                saved = self.svc.export_build_config(path.value.strip() or self.default_build_path())
            except AdminError as e:
                result.value, result.color, result.visible = str(e), C.ERROR, True
            else:
                result.value, result.color, result.visible = f"Saved to {saved}", C.PRIMARY, True
                self.toast(f"Build config saved to {saved}")
            self.page.update()

        return section(
            "Build config",
            ft.Row([path, ft.FilledButton("Export", icon=ft.Icons.FILE_DOWNLOAD_OUTLINED, on_click=export)],
                   spacing=12),
            result,
            icon=ft.Icons.FILE_DOWNLOAD_OUTLINED,
            subtitle="approval.json goes into the Chat Archiver build. It has only public data: bot name, "
                     "bot id and your public key.")

    def settings_owner_key(self) -> ft.Control:
        warn = ft.Container(
            ft.Row([ft.Icon(ft.Icons.WARNING_AMBER_ROUNDED, color=C.ON_TERTIARY_CONTAINER),
                    ft.Text("The owner key signs every activation code. If it is lost, you have to rebuild "
                            "Chat Archiver with a new key and everyone must request access again. "
                            "Keep a password-protected backup somewhere safe.",
                            color=C.ON_TERTIARY_CONTAINER, expand=True)], spacing=10,
                   vertical_alignment=ft.CrossAxisAlignment.START),
            bgcolor=C.TERTIARY_CONTAINER, border_radius=10, padding=12)

        # export
        pw1 = ft.TextField(label="Backup password", password=True, can_reveal_password=True, expand=True,
                           hint_text=f"At least {MIN_BACKUP_PASSWORD} characters")
        pw2 = ft.TextField(label="Repeat password", password=True, can_reveal_password=True, expand=True)
        exp_err = ft.Text("", color=C.ERROR, visible=False)
        out_field = ft.TextField(label="Backup (encrypted)", multiline=True, read_only=True, min_lines=2,
                                 max_lines=3, visible=False, text_size=12)
        save_path = ft.TextField(label="Save to file", value=str(self.hub.build_dir / "owner-key-backup.txt"),
                                 expand=True)
        save_result = ft.Text("", visible=False, selectable=True)
        after = ft.Column([out_field,
                           ft.Row([ft.OutlinedButton("Copy", icon=ft.Icons.CONTENT_COPY,
                                                     on_click=lambda e: self.copy(out_field.value, "Backup")),
                                   save_path,
                                   ft.OutlinedButton("Save", icon=ft.Icons.SAVE_ALT, on_click=lambda e: save_file())],
                                  spacing=12),
                           save_result], spacing=10, visible=False,
                          horizontal_alignment=ft.CrossAxisAlignment.STRETCH)

        def make_backup(_):
            exp_err.visible = False
            if (pw1.value or "") != (pw2.value or ""):
                exp_err.value, exp_err.visible = "The passwords do not match.", True
            else:
                try:
                    text = self.svc.export_owner_backup(pw1.value or "")
                except AdminError as e:
                    exp_err.value, exp_err.visible = str(e), True
                else:
                    out_field.value, out_field.visible, after.visible = text, True, True
                    pw1.value = pw2.value = ""
            self.page.update()

        def save_file():
            try:
                p = Path(save_path.value.strip())
                p.parent.mkdir(parents=True, exist_ok=True)
                p.write_text(out_field.value + "\n", encoding="utf-8")
            except OSError as e:
                save_result.value, save_result.color = f"Cannot save: {e.strerror or type(e).__name__}", C.ERROR
            else:
                save_result.value, save_result.color = f"Saved to {p}", C.PRIMARY
            save_result.visible = True
            self.page.update()

        # import
        imp_text = ft.TextField(label="Paste a backup (CAOK1:…)", multiline=True, min_lines=2, max_lines=3,
                                text_size=12)
        imp_pw = ft.TextField(label="Backup password", password=True, can_reveal_password=True, expand=True)

        def ask_import(_):
            async def act():
                self.svc.import_owner_backup(imp_text.value or "", imp_pw.value or "")
                imp_text.value = imp_pw.value = ""
                return "Owner key restored. Export the build config again if you rebuild the app."

            self.confirm("Replace the owner key?",
                         "The current owner key is replaced by the one from the backup. Builds made with the "
                         "current key will stop accepting new activation codes.",
                         "Replace key", act, destructive=True, icon=ft.Icons.KEY_OUTLINED)

        return section(
            "Owner key",
            self.kv("Public key", self.svc.owner_fingerprint or "—"),
            warn,
            ft.Text("Create a backup", weight=ft.FontWeight.W_600),
            ft.Row([pw1, pw2, ft.FilledButton("Create backup", icon=ft.Icons.LOCK_OUTLINE, on_click=make_backup)],
                   spacing=12),
            exp_err, after,
            ft.Divider(height=1, color=C.OUTLINE_VARIANT),
            ft.Text("Restore from a backup", weight=ft.FontWeight.W_600),
            imp_text,
            ft.Row([imp_pw, ft.OutlinedButton("Restore", icon=ft.Icons.UPLOAD_FILE, on_click=ask_import)],
                   spacing=12),
            icon=ft.Icons.KEY_OUTLINED)

    def settings_appearance(self) -> ft.Control:
        def changed(e):
            value = next(iter(e.control.selected), "system")
            self.page.theme_mode = {"system": ft.ThemeMode.SYSTEM, "light": ft.ThemeMode.LIGHT,
                                    "dark": ft.ThemeMode.DARK}[value]
            self.sync_theme_button()
            self.page.update()

        cur = {ft.ThemeMode.LIGHT: "light", ft.ThemeMode.DARK: "dark"}.get(self.page.theme_mode, "system")
        return section(
            "Appearance",
            ft.SegmentedButton(selected=[cur], on_change=changed, segments=[
                ft.Segment("system", label=ft.Text("System"), icon=ft.Icon(ft.Icons.BRIGHTNESS_AUTO_OUTLINED)),
                ft.Segment("light", label=ft.Text("Light"), icon=ft.Icon(ft.Icons.LIGHT_MODE_OUTLINED)),
                ft.Segment("dark", label=ft.Text("Dark"), icon=ft.Icon(ft.Icons.DARK_MODE_OUTLINED)),
            ]),
            icon=ft.Icons.PALETTE_OUTLINED)


def make_main(hub: Hub) -> Callable[[ft.Page], None]:
    def main(page: ft.Page) -> None:
        AdminApp(page, hub).start()
    return main
