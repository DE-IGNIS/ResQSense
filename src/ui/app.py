"""Flet desktop interface for the ResQSense rescue assistance system.

The interface only reads and commands the backend through ``BackendService``.
"""

from __future__ import annotations

import asyncio
import time
from datetime import datetime
from typing import Any, Optional

import flet as ft

from src.core.backend_service import BackendService
from src.core.detection import PresenceLevel


# Palette inspired by the supplied reference: dark ink navigation, warm canvas,
# white bento cards, and a bright lime active state.
INK = "#211F31"
INK_SOFT = "#343247"
LIME = "#D8F36A"
CANVAS = "#F4F4F1"
WHITE = "#FFFFFF"
MUTED = "#858591"
LINE = "#E9E9E5"
GREEN = "#419B66"
AMBER = "#D89B3D"
RED = "#D45B55"
PURPLE = "#A990E8"


def _icon(name: str) -> Any:
    icons = getattr(ft, "Icons", getattr(ft, "icons", None))
    return getattr(icons, name, None) if icons else None


def _text(value: str, size: int = 14, color: str = INK, *, weight: Any = None,
          font: Optional[str] = None) -> ft.Text:
    return ft.Text(value, size=size, color=color, weight=weight, font_family=font)


def _card(content: Any, *, padding: int = 20, expand: bool = False,
          bgcolor: str = WHITE, radius: int = 20) -> ft.Container:
    return ft.Container(content=content, padding=padding, bgcolor=bgcolor,
                         border_radius=radius, expand=expand)


def _section_title(title: str, trailing: Any = None) -> ft.Row:
    controls: list[Any] = [_text(title, 17, INK, weight=ft.FontWeight.W_600)]
    if trailing is not None:
        controls.append(trailing)
    return ft.Row(controls, alignment=ft.MainAxisAlignment.SPACE_BETWEEN,
                  vertical_alignment=ft.CrossAxisAlignment.CENTER)


def _metric(label: str, value: str, caption: str, accent: str = LIME,
            symbol: str = "•") -> ft.Container:
    return _card(ft.Column([
        ft.Row([ft.Container(width=34, height=34, alignment=ft.Alignment.CENTER,
                             bgcolor=accent, border_radius=11,
                             content=_text(symbol, 18, INK, weight=ft.FontWeight.BOLD)),
                ft.Container(expand=True), _text(caption, 11, MUTED)]),
        ft.Container(height=7),
        _text(value, 27, INK, weight=ft.FontWeight.BOLD),
        _text(label, 12, MUTED),
    ], spacing=2), expand=True)


def build_app(page: ft.Page, service: BackendService, port: Optional[str] = None) -> None:
    page.title = "ResQSense | Rescue operations"
    page.theme_mode = ft.ThemeMode.LIGHT
    page.bgcolor = CANVAS
    page.padding = 0
    page.window_min_width = 1050
    page.window_min_height = 700
    page.theme = ft.Theme(color_scheme_seed=LIME, font_family="Arial")

    pages = ["Overview", "Search zones", "Alerts", "History", "Sensors"]
    page_icons = ["DASHBOARD_ROUNDED", "MAPS_HOME_WORK_ROUNDED", "NOTIFICATIONS_ROUNDED",
                  "QUERY_STATS_ROUNDED", "SENSORS_ROUNDED"]
    selected = {"page": "Overview"}
    scroll_positions: dict[str, float] = {name: 0.0 for name in pages}
    data: dict[str, Any] = {"radar": None, "vitals": None, "status": None,
                            "last_alert": None, "notice": ""}
    content = ft.Container(expand=True)
    body_slot = ft.Container(content=content, expand=True,
                              padding=ft.Padding(left=26, right=26, top=23, bottom=20))
    app_root: dict[str, Any] = {"control": None, "view": None}
    nav_items = ft.Column(spacing=5)
    status_text = _text("Starting sensors…", 11, "#C7C5D0")

    def choose(name: str) -> None:
        selected["page"] = name
        render()

    def remember_scroll(event: Any) -> None:
        scroll_positions[selected["page"]] = float(getattr(event, "pixels", 0.0) or 0.0)

    def nav_button(name: str, icon_name: str) -> ft.Control:
        active = selected["page"] == name
        icon = _icon(icon_name)
        return ft.Container(
            content=ft.Row([
                ft.Icon(icon, size=19, color=INK if active else "#D0CED8") if icon else _text("◈", 18, INK if active else "#D0CED8"),
                _text(name, 13, INK if active else "#E4E2EA", weight=ft.FontWeight.W_600 if active else None),
            ], spacing=12),
            padding=ft.Padding.symmetric(horizontal=13, vertical=12),
            bgcolor=LIME if active else None, border_radius=15,
            on_click=lambda _: choose(name), ink=True,
        )

    def toast(message: str) -> None:
        page.snack_bar = ft.SnackBar(content=_text(message, 13, WHITE), bgcolor=INK)
        page.snack_bar.open = True
        page.update()

    def draw_sidebar() -> ft.Container:
        nav_items.controls = [nav_button(name, icon) for name, icon in zip(pages, page_icons)]
        return ft.Container(
            width=225, bgcolor=INK, padding=ft.Padding(left=18, right=18, top=25, bottom=18),
            content=ft.Column([
                ft.Row([ft.Container(width=35, height=35, bgcolor=LIME, border_radius=12,
                                     alignment=ft.Alignment.CENTER,
                                     content=_text("R", 20, INK, weight=ft.FontWeight.BOLD)),
                        ft.Column([_text("ResQSense", 17, WHITE, weight=ft.FontWeight.BOLD),
                                   _text("FIELD OPERATIONS", 8, "#A5A2B1", weight=ft.FontWeight.BOLD)], spacing=0)], spacing=10),
                ft.Container(height=26),
                _text("WORKSPACE", 9, "#8F8C9B", weight=ft.FontWeight.BOLD),
                nav_items,
                ft.Container(expand=True),
                ft.Container(padding=14, bgcolor=INK_SOFT, border_radius=16,
                             content=ft.Column([
                                 ft.Row([ft.Container(width=8, height=8, bgcolor=GREEN if _connected() else AMBER, border_radius=8),
                                         _text("DEVICE LINK", 9, "#B9B7C1", weight=ft.FontWeight.BOLD)], spacing=8),
                                 status_text,
                                 _text("ESP32-C6  ·  LD2410D", 9, "#9996A5"),
                             ], spacing=8)),
                _text("RESCUE ASSISTANCE CONSOLE", 8, "#777484"),
            ], spacing=12, expand=True),
        )

    def _connected() -> bool:
        status = data.get("status")
        return bool(getattr(status, "connected", False))

    def value_for(attr: str, fallback: str = "—", digits: int = 0) -> str:
        obj = data.get("vitals")
        if obj is None:
            return fallback
        value = getattr(obj, attr, None)
        if value is None or (attr == "heart_rate_bpm" and value <= 0) or (attr == "spo2_percent" and value <= 0):
            return fallback
        return f"{value:.{digits}f}"

    def level_info() -> tuple[str, str, str]:
        try:
            state = service.get_detection_state()
            level = state.current_level
            label = level.name.replace("_", " ")
            if level == PresenceLevel.CONFIRMED:
                return label, RED, "Vital signs or sustained strong radar"
            if level == PresenceLevel.PROBABLE:
                return label, AMBER, "Sustained radar activity detected"
            if level == PresenceLevel.EXPERIMENTAL:
                return label, PURPLE, "Sensor trigger · verify with another sweep"
            return "NO SIGNAL", GREEN, "No presence signal currently detected"
        except Exception:
            return "NO SIGNAL", MUTED, "Waiting for a valid sensor frame"

    def header(kicker: str, title: str, action: Any = None) -> ft.Row:
        return ft.Row([
            ft.Column([_text(kicker.upper(), 10, MUTED, weight=ft.FontWeight.BOLD),
                       _text(title, 26, INK, weight=ft.FontWeight.BOLD)], spacing=3),
            ft.Container(expand=True),
            action if action is not None else ft.Container(),
            ft.Container(width=38, height=38, bgcolor=WHITE, border_radius=13,
                         alignment=ft.Alignment.CENTER,
                         content=ft.Icon(_icon("ACCOUNT_CIRCLE_ROUNDED"), color=INK, size=22)),
        ], vertical_alignment=ft.CrossAxisAlignment.CENTER)

    def radar_visual() -> ft.Control:
        target = data.get("radar")
        has_target = bool(getattr(target, "has_target", False))
        radius = 125
        rings = [ft.Container(width=radius * r, height=radius * r, border=ft.Border.all(1, "#E8E8E4"),
                              border_radius=300, alignment=ft.Alignment.CENTER)
                 for r in (1, 1.55, 2)]
        radar = ft.Container(width=300, height=185, alignment=ft.Alignment.CENTER,
                             content=ft.Stack(controls=[
                                 ft.Container(width=290, height=1, bgcolor="#ECECE8", left=5, top=92),
                                 ft.Container(width=1, height=180, bgcolor="#ECECE8", left=145, top=2),
                                 *[ft.Container(content=ring, left=145-(ring.width/2), top=92-(ring.height/2)) for ring in rings],
                                 ft.Container(width=13, height=13, bgcolor=RED if has_target else LIME,
                                              border=ft.Border.all(3, WHITE), border_radius=20,
                                              left=204 if has_target else 139, top=48 if has_target else 86),
                                 ft.Container(left=5, top=165, content=_text("0 m", 9, MUTED)),
                                 ft.Container(right=0, top=165, content=_text("6 m range", 9, MUTED)),
                             ]))
        return ft.Column([radar, ft.Row([
            ft.Column([_text("MOVING", 9, MUTED, weight=ft.FontWeight.BOLD),
                       _text(f"{getattr(target, 'moving_distance_cm', 0)} cm", 16, INK, weight=ft.FontWeight.BOLD),
                       _text(f"Energy {getattr(target, 'moving_energy', 0)}%", 10, MUTED)], spacing=3, expand=True),
            ft.Container(width=1, height=42, bgcolor=LINE),
            ft.Column([_text("STATIONARY", 9, MUTED, weight=ft.FontWeight.BOLD),
                       _text(f"{getattr(target, 'stationary_distance_cm', 0)} cm", 16, INK, weight=ft.FontWeight.BOLD),
                       _text(f"Energy {getattr(target, 'stationary_energy', 0)}%", 10, MUTED)], spacing=3, expand=True),
        ], spacing=14)], horizontal_alignment=ft.CrossAxisAlignment.CENTER, spacing=5)

    def overview() -> ft.Control:
        level, color, note = level_info()
        active_zones = service.list_zones("active")
        items: list[Any] = [
            header("Rescue operations", "Field overview",
                   ft.Container(content=ft.Row([ft.Container(width=8, height=8, bgcolor=GREEN if _connected() else AMBER, border_radius=8),
                                                _text("CONNECTED" if _connected() else "OFFLINE MODE", 10, INK, weight=ft.FontWeight.BOLD)], spacing=8),
                              bgcolor=WHITE, border_radius=20, padding=ft.Padding.symmetric(horizontal=13, vertical=10))),
        ]
        if not _connected():
            items.append(ft.Container(padding=ft.Padding.symmetric(horizontal=14, vertical=10), bgcolor="#FFF2D9", border_radius=13,
                                      content=ft.Row([ft.Text("ⓘ", color=AMBER, size=15),
                                                      _text(data.get("notice") or "Offline mode · connect an ESP32 to receive live readings", 11, INK)], spacing=9)))
        items.extend([
            ft.Row([
                _metric("Active search zones", str(len(active_zones)), "IN PROGRESS", LIME, "⌖"),
                _metric("Heart rate", f"{value_for('heart_rate_bpm', digits=0)}" + (" bpm" if data.get("vitals") and value_for("heart_rate_bpm") != "—" else ""), "MAX30100", "#DDF0C7", "♥"),
                _metric("Oxygen saturation", f"{value_for('spo2_percent', digits=1)}" + ("%" if value_for("spo2_percent") != "—" else ""), "MAX30100", "#E6DFFC", "◌"),
                _metric("Detection", level, "PRESENCE LEVEL", "#F8E7D1" if color == AMBER else ("#F8D8D4" if color == RED else LIME), "◎"),
            ], spacing=12),
            ft.Row([
                _card(ft.Column([_section_title("Radar scan", ft.Row([ft.Container(width=8, height=8, bgcolor=GREEN if _connected() else MUTED, border_radius=8), _text("LD2410D", 10, MUTED)])),
                                 radar_visual()], spacing=12), expand=True),
                _card(ft.Column([
                    _section_title("Presence assessment", ft.Container(padding=ft.Padding.symmetric(horizontal=10, vertical=6), bgcolor="#F5F5F2", border_radius=20,
                                                       content=_text(level, 9, color, weight=ft.FontWeight.BOLD))),
                    ft.Container(height=5),
                    ft.Row([ft.Container(width=5, height=52, bgcolor=color, border_radius=5),
                            ft.Column([_text(note, 15, INK, weight=ft.FontWeight.W_600),
                                       _text("Treat this as a search lead. Confirm findings with trained responders and established rescue protocols.", 11, MUTED)], spacing=6, expand=True)], spacing=12),
                    ft.Divider(color=LINE, height=18),
                    _section_title("Recent alerts", ft.TextButton("See all", on_click=lambda _: choose("Alerts"))),
                    alert_preview(),
                ], spacing=9), expand=True),
            ], spacing=12, expand=True, vertical_alignment=ft.CrossAxisAlignment.START),
            ft.Row([
                _card(ft.Column([_section_title("Search zones", ft.TextButton("Manage zones", on_click=lambda _: choose("Search zones"))),
                                 zone_preview(active_zones)], spacing=10), expand=True),
                _card(ft.Column([_section_title("Sensor health"), sensor_health()], spacing=12), expand=True),
            ], spacing=12, vertical_alignment=ft.CrossAxisAlignment.START),
        ])
        return ft.Column(items, spacing=13, scroll=ft.ScrollMode.AUTO, expand=True, on_scroll=remember_scroll)

    def alert_preview() -> ft.Control:
        alerts = service.get_alert_history_from_db(limit=3)
        if not alerts:
            return ft.Container(padding=ft.Padding.symmetric(vertical=10), content=_text("No alerts recorded yet", 12, MUTED))
        return ft.Column([alert_row(item, compact=True) for item in alerts], spacing=7)

    def alert_row(item: dict, compact: bool = False) -> ft.Control:
        severity = str(item.get("severity", "INFO")).upper()
        color = RED if severity == "CRITICAL" else AMBER if severity == "WARNING" else GREEN
        when = datetime.fromtimestamp(float(item.get("timestamp", time.time()))).strftime("%H:%M · %d %b")
        return ft.Container(padding=10 if compact else 13, bgcolor="#FAFAF8", border_radius=13,
                            content=ft.Row([ft.Container(width=8, height=8, bgcolor=color, border_radius=8),
                                            ft.Column([_text(item.get("message", "Sensor event"), 12, INK, weight=ft.FontWeight.W_600),
                                                       _text(f"{severity}  ·  {when}", 10, MUTED)], spacing=3, expand=True),
                                            _text(str(item.get("level", "")).replace("_", " "), 9, color, weight=ft.FontWeight.BOLD)], spacing=10))

    def zone_preview(zones: list[dict]) -> ft.Control:
        if not zones:
            return ft.Row([_text("No active search zones", 12, MUTED), ft.Container(expand=True),
                           ft.TextButton("Create zone", on_click=lambda _: show_zone_dialog())])
        return ft.Column([ft.Row([ft.Container(width=34, height=34, bgcolor="#F0F5D9", border_radius=11,
                                                 alignment=ft.Alignment.CENTER, content=_text("⌖", 17, INK)),
                                   ft.Column([_text(zone["name"], 12, INK, weight=ft.FontWeight.W_600),
                                              _text(zone.get("description") or "Active search area", 10, MUTED)], spacing=3, expand=True),
                                   _text("ACTIVE", 9, GREEN, weight=ft.FontWeight.BOLD)], spacing=10)
                            for zone in zones[:2]], spacing=9)

    def sensor_health() -> ft.Control:
        return ft.Column([
            health_row("ESP32-C6", "USB serial bridge", _connected()),
            health_row("LD2410D radar", "24 GHz presence sensor", _connected() and data.get("radar") is not None),
            health_row("MAX30100", "Pulse oximeter", bool(getattr(data.get("vitals"), "is_finger_present", False))),
        ], spacing=10)

    def health_row(name: str, detail: str, ok: bool) -> ft.Control:
        return ft.Row([ft.Container(width=9, height=9, bgcolor=GREEN if ok else "#C7C6C1", border_radius=9),
                       ft.Column([_text(name, 11, INK, weight=ft.FontWeight.W_600), _text(detail, 9, MUTED)], spacing=2, expand=True),
                       _text("ONLINE" if ok else "WAITING", 8, GREEN if ok else MUTED, weight=ft.FontWeight.BOLD)], spacing=9)

    def zones_view() -> ft.Control:
        zones = service.list_zones()
        rows = []
        for zone in zones:
            active = zone.get("status") == "active"
            rows.append(_card(ft.Row([
                ft.Container(width=42, height=42, bgcolor="#F0F5D9" if active else "#F1F0F5", border_radius=13,
                             alignment=ft.Alignment.CENTER, content=_text("⌖", 20, INK)),
                ft.Column([_text(zone["name"], 14, INK, weight=ft.FontWeight.W_600),
                           _text(zone.get("description") or "No field notes added", 11, MUTED),
                           _text(f"Zone #{zone['id']}  ·  {zone.get('status', 'active').upper()}", 9, GREEN if active else MUTED, weight=ft.FontWeight.BOLD)], spacing=4, expand=True),
                ft.TextButton("Set active", on_click=lambda _, zid=zone["id"]: activate_zone(zid)) if active else ft.Container(),
                ft.TextButton("Complete", on_click=lambda _, zid=zone["id"]: complete_zone(zid)) if active else ft.Container(),
            ], spacing=12)))
        if not rows:
            rows = [_card(ft.Column([_text("No search zones yet", 17, INK, weight=ft.FontWeight.W_600),
                                     _text("Create a zone for each area your team is scanning.", 12, MUTED),
                                     ft.Container(height=4), ft.FilledButton("Create first zone", icon=_icon("ADD_ROUNDED"), on_click=lambda _: show_zone_dialog())], spacing=9), padding=24)]
        return ft.Column([header("Field planning", "Search zones", ft.FilledButton("New zone", icon=_icon("ADD_ROUNDED"), on_click=lambda _: show_zone_dialog())),
                          _text("Organize scans by building, floor, or collapsed section.", 12, MUTED), *rows], spacing=12, scroll=ft.ScrollMode.AUTO, expand=True, on_scroll=remember_scroll)

    def activate_zone(zone_id: int) -> None:
        service.set_active_zone(zone_id)
        toast(f"Zone #{zone_id} is now active")
        render()

    def complete_zone(zone_id: int) -> None:
        service.complete_zone(zone_id)
        toast(f"Zone #{zone_id} marked complete")
        render()

    def show_zone_dialog() -> None:
        name = ft.TextField(label="Zone name", hint_text="e.g. Building A · East wing", autofocus=True, border_radius=12)
        description = ft.TextField(label="Field notes", hint_text="Location details for the rescue team", multiline=True, min_lines=2, max_lines=3, border_radius=12)
        def create(_: Any) -> None:
            if not name.value or not name.value.strip():
                name.error_text = "Enter a zone name"
                page.update()
                return
            zone_id = service.create_zone(name.value.strip(), description.value or "")
            service.set_active_zone(zone_id)
            page.pop_dialog()
            toast("Search zone created and activated")
            render()
        dialog = ft.AlertDialog(modal=True, title=_text("Create search zone", 18, INK, weight=ft.FontWeight.BOLD),
                                content=ft.Column([name, description], tight=True, spacing=12),
                                actions=[ft.TextButton("Cancel", on_click=lambda _: page.pop_dialog()),
                                         ft.FilledButton("Create zone", on_click=create)], actions_alignment=ft.MainAxisAlignment.END)
        page.show_dialog(dialog)

    def alerts_view() -> ft.Control:
        alerts = service.get_alert_history_from_db(limit=50)
        body = [alert_row(item) for item in alerts] if alerts else [_card(ft.Column([
            _text("All clear for now", 17, INK, weight=ft.FontWeight.W_600),
            _text("Detection alerts will appear here when the backend reports a state change.", 12, MUTED)], spacing=8), padding=24)]
        return ft.Column([header("Response activity", "Alerts & notifications"),
                          ft.Row([_metric("Recorded alerts", str(len(alerts)), "HISTORY", "#E8E2F8", "!"),
                                  _metric("Current presence", level_info()[0], "LIVE ASSESSMENT", "#F8E7D1", "◎")], spacing=12),
                          _text("Latest first · Review each lead with the full rescue team.", 11, MUTED), *body], spacing=12, scroll=ft.ScrollMode.AUTO, expand=True, on_scroll=remember_scroll)

    def history_view() -> ft.Control:
        readings = service.get_sensor_history(limit=30)
        events = service.get_system_events(limit=15)
        radar_samples = [r for r in reversed(readings) if r.get("source") == "ld2410d"][-12:]
        energy_values = []
        for item in radar_samples:
            raw = item.get("data", {})
            energy = max(float(raw.get("moving_energy", 0)), float(raw.get("stationary_energy", 0))) if isinstance(raw, dict) else 0
            energy_values.append((item, max(0, min(100, energy))))
        chart_bars = []
        for item, energy in energy_values:
            stamp = datetime.fromtimestamp(float(item.get("timestamp", time.time()))).strftime("%H:%M")
            chart_bars.append(ft.Column([
                ft.Container(height=82 * energy / 100, width=15, bgcolor=LIME if energy < 35 else AMBER,
                             border_radius=ft.BorderRadius(5, 5, 0, 0), alignment=ft.Alignment.BOTTOM_CENTER),
                _text(stamp, 8, MUTED),
            ], alignment=ft.MainAxisAlignment.END, horizontal_alignment=ft.CrossAxisAlignment.CENTER, spacing=5, expand=True))
        chart = _card(ft.Column([
            _section_title("Radar energy over recent readings", _text("0–100%", 10, MUTED)),
            ft.Container(height=105, content=ft.Row(chart_bars, spacing=8, alignment=ft.MainAxisAlignment.SPACE_BETWEEN,
                                                    vertical_alignment=ft.CrossAxisAlignment.END)) if chart_bars else _text("Chart will populate when radar readings are stored.", 11, MUTED),
        ], spacing=12))
        reading_rows = []
        for item in readings:
            stamp = datetime.fromtimestamp(float(item.get("timestamp", time.time()))).strftime("%H:%M:%S")
            payload = item.get("data", item.get("data_json", "Reading received"))
            if isinstance(payload, dict):
                payload = " · ".join(f"{k}: {v}" for k, v in list(payload.items())[:4])
            reading_rows.append(ft.Row([_text(stamp, 10, MUTED, font="monospace"),
                                        _text(str(item.get("source", "sensor")).upper(), 9, PURPLE, weight=ft.FontWeight.BOLD),
                                        _text(str(payload)[:90], 10, INK)], spacing=12))
        event_rows = [ft.Row([_text(datetime.fromtimestamp(float(e.get("timestamp", time.time()))).strftime("%d %b · %H:%M"), 9, MUTED),
                              _text(str(e.get("message", "System event")), 10, INK)], spacing=10) for e in events[:10]]
        return ft.Column([header("Review & analysis", "Sensor history"), chart,
                          ft.Row([_card(ft.Column([_section_title("Recent sensor readings"),
                                                  *(reading_rows or [_text("No readings stored yet", 12, MUTED)])], spacing=12), expand=True),
                                  _card(ft.Column([_section_title("System events"),
                                                  *(event_rows or [_text("No events recorded yet", 12, MUTED)])], spacing=12), expand=True)],
                                 spacing=12, vertical_alignment=ft.CrossAxisAlignment.START)], spacing=12, scroll=ft.ScrollMode.AUTO, expand=True, on_scroll=remember_scroll)

    def sensors_view() -> ft.Control:
        ports = service.list_serial_ports()
        port_list = "\n".join(f"{p['port']}  ·  {p['description']}" for p in ports) or "No serial ports found"
        status = data.get("status")
        last = getattr(status, "last_data_at", 0) if status else 0
        last_seen = datetime.fromtimestamp(last).strftime("%H:%M:%S") if last else "No data received"
        return ft.Column([header("Device configuration", "Sensors & connection"),
                          ft.Row([_card(ft.Column([_section_title("ESP32-C6 connection"),
                                                  health_row("USB serial", getattr(status, "port", "Auto-detect") or "Auto-detect", _connected()),
                                                  ft.Divider(color=LINE),
                                                  _text(f"Last data: {last_seen}", 11, MUTED),
                                                  _text(f"Received: {getattr(status, 'bytes_received', 0):,} bytes  ·  Reconnects: {getattr(status, 'reconnect_count', 0)}", 10, MUTED),
                                                  ft.TextButton("Refresh status", on_click=lambda _: refresh_now())], spacing=12), expand=True),
                                  _card(ft.Column([_section_title("Available ports"), _text(port_list, 11, MUTED),
                                                  _text("Start with: python main.py --port <device>", 10, INK, font="monospace")], spacing=12), expand=True)], spacing=12),
                          ft.Row([_card(ft.Column([_section_title("HLK-LD2410D radar"),
                                                  health_row("24 GHz mmWave", "Presence / motion / stationary target", _connected() and data.get("radar") is not None),
                                                  ft.Row([ft.OutlinedButton("Engineering mode", on_click=lambda _: command(service.enable_engineering_mode(), "Engineering mode enabled")),
                                                          ft.OutlinedButton("Restart radar", on_click=lambda _: command(service.restart_radar(), "Radar restart command sent"))], wrap=True)], spacing=14), expand=True),
                                  _card(ft.Column([_section_title("MAX30100 pulse oximeter"),
                                                  health_row("Vitals sensor", "Heart rate and SpO₂", bool(getattr(data.get("vitals"), "is_finger_present", False))),
                                                  ft.Row([_text("Heart rate", 11, MUTED), _text(value_for("heart_rate_bpm", digits=0) + " bpm", 12, INK, weight=ft.FontWeight.W_600),
                                                          _text("SpO₂", 11, MUTED), _text(value_for("spo2_percent", digits=1) + "%", 12, INK, weight=ft.FontWeight.W_600)], spacing=8, wrap=True)], spacing=14), expand=True)], spacing=12)], spacing=12, scroll=ft.ScrollMode.AUTO, expand=True, on_scroll=remember_scroll)

    def command(success: bool, message: str) -> None:
        toast(message if success else "Command not sent · check the ESP32 connection")

    def refresh_now() -> None:
        try:
            data["status"] = service.get_connection_status()
        except Exception:
            pass
        render()

    view_builders = {"Overview": overview, "Search zones": zones_view, "Alerts": alerts_view,
                     "History": history_view, "Sensors": sensors_view}

    def render() -> None:
        try:
            status = service.get_connection_status()
            data["status"] = status
            status_text.value = f"{status.port or 'No ESP32 detected'} · {status.baud_rate:,} baud"
        except Exception:
            status_text.value = "Connection unavailable"
        view = view_builders[selected["page"]]()
        content.content = view
        if app_root["control"] is None:
            app_root["control"] = ft.Row([draw_sidebar(), body_slot], spacing=0, expand=True)
            page.add(app_root["control"])
        else:
            app_root["control"].controls[0] = draw_sidebar()
        page.update()
        offset = scroll_positions[selected["page"]]
        if offset > 0:
            page.run_task(restore_scroll, view, offset)
        app_root["view"] = view

    async def restore_scroll(view: ft.Column, offset: float) -> None:
        await asyncio.sleep(0.05)
        await view.scroll_to(offset=offset, duration=0)

    async def poll() -> None:
        while True:
            try:
                data["status"] = service.get_connection_status()
                data["vitals"] = service.get_last_vitals()
                state = service.get_detection_state()
                if state.last_target is not None:
                    data["radar"] = state.last_target
                render()
            except Exception:
                # Keep the UI alive if the device or database briefly fails.
                pass
            await asyncio.sleep(1.5)

    def on_close(_: Any) -> None:
        try:
            service.stop()
        except Exception:
            pass

    page.on_close = on_close
    try:
        service.start(port=port)
        if not getattr(service.get_connection_status(), "connected", False):
            data["notice"] = "Offline mode · connect an ESP32 to receive live readings"
    except Exception as exc:
        data["notice"] = f"Startup issue: {exc}"
    render()
    page.run_task(poll)
