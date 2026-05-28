"""Discord Remote — manually enabled Discord companion control.

Requires optional dependency:
    python3 -m pip install discord.py

Configuration lives in secrets.conf:
    DISCORD_BOT_TOKEN=...
    DISCORD_CHANNEL_ID=...
    DISCORD_ALLOWED_USERS=123456789,987654321

Commands use a message prefix in the configured channel:
    !wdg status
    !wdg loot
    !wdg tail
    !wdg scan wifi
    !wdg scan bt
    !wdg sdr adsb
    !wdg sdr 433
    !wdg sdr stop
    !wdg stop
    !wdg disable
"""

from __future__ import annotations

import asyncio
import logging
import queue
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from plugins.plugin_base import PluginBase, PluginMenuItem

log = logging.getLogger(__name__)


PREFIX = "!wdg"
MAX_REPLY = 1800


def _load_secrets_conf() -> dict[str, str]:
    result = {}
    path = Path(__file__).parent.parent / "secrets.conf"
    if not path.is_file():
        return result
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if "=" in line:
                key, _, value = line.partition("=")
                result[key.strip()] = value.strip()
    except OSError:
        pass
    return result


def _parse_ids(value: str) -> set[int]:
    ids: set[int] = set()
    for item in value.replace(" ", "").split(","):
        if not item:
            continue
        try:
            ids.add(int(item))
        except ValueError:
            pass
    return ids


@dataclass
class _RemoteRequest:
    user_id: int
    command: str
    reply: Callable[[str], None]


class DiscordRemote(PluginBase):
    NAME = "Discord Remote"
    VERSION = "0.1"
    AUTHOR = "phutur1st"

    def __init__(self):
        super().__init__()
        self.has_overlay = True
        self._overlay_active = False
        self._menu_sel = 0
        self._log: list[tuple[str, int]] = []
        self._enabled = False
        self._starting = False
        self._thread: threading.Thread | None = None
        self._requests: queue.Queue[_RemoteRequest] = queue.Queue()
        self._token = ""
        self._channel_id = 0
        self._allowed_users: set[int] = set()
        self._loop = None
        self._client = None
        self._load_config()

    def menu_items(self) -> list[PluginMenuItem]:
        return [
            PluginMenuItem("d", "Discord Remote", "open_overlay"),
        ]

    @property
    def overlay_active(self) -> bool:
        return self._overlay_active

    def on_load(self, app) -> None:
        super().on_load(app)
        self._log.append(("Disabled by default", 13))

    def on_unload(self) -> None:
        self._disable()

    def open_overlay(self):
        self._overlay_active = True
        self._menu_sel = 0
        self._load_config()
        if not self._configured():
            self._log_add("Set token, channel, and allowed users in secrets.conf", 10)
        else:
            self._log_add("Config loaded", 11)

    def on_update(self) -> None:
        self._drain_requests()
        if not self._overlay_active:
            return
        import pyxel

        if pyxel.btnp(pyxel.KEY_ESCAPE):
            self._overlay_active = False
            if self.app:
                self.app._esc_consumed_frame = pyxel.frame_count
            return

        items = self._overlay_items()
        if pyxel.btnp(pyxel.KEY_UP) and self._menu_sel > 0:
            self._menu_sel -= 1
        if pyxel.btnp(pyxel.KEY_DOWN):
            self._menu_sel = min(self._menu_sel + 1, len(items) - 1)
        if pyxel.btnp(pyxel.KEY_RETURN) and items:
            action = items[min(self._menu_sel, len(items) - 1)][0]
            self._exec(action)

    def draw(self, x: int, y: int, w: int, h: int) -> None:
        if not self._overlay_active:
            return
        import pyxel

        pyxel.rect(0, 0, w, h, 0)
        pyxel.rect(0, 0, w, 12, 1)
        state = "ENABLED" if self._enabled else "DISABLED"
        if self._starting:
            state = "STARTING"
        color = 11 if self._enabled else 10 if self._starting else 8
        pyxel.text(4, 3, "DISCORD REMOTE", 3)
        pyxel.text(w - 80, 3, state, color)

        items = self._overlay_items()
        y0 = 22
        for i, (_action, label) in enumerate(items):
            sel = i == self._menu_sel
            prefix = "\x10" if sel else " "
            pyxel.text(8, y0, f"{prefix} {label}", 7 if sel else 13)
            y0 += 10

        pyxel.line(240, 16, 240, h - 16, 1)
        pyxel.text(248, 20, "-- Remote Log --", 3)
        ly = 34
        max_lines = (h - 50) // 8
        for text, c in self._log[-max_lines:]:
            pyxel.text(248, ly, text[:52], c)
            ly += 8

        pyxel.text(8, h - 34, f"Channel: {self._channel_id or 'not set'}", 13)
        pyxel.text(8, h - 24, f"Allowed users: {len(self._allowed_users)}", 13)
        pyxel.text(8, h - 12, "[ENTER] Execute  [ESC] Back", 13)

    def _overlay_items(self) -> list[tuple[str, str]]:
        if self._enabled or self._starting:
            return [("disable", "Disable Discord Remote")]
        return [("enable", "Enable Discord Remote")]

    def _exec(self, action: str) -> None:
        if action == "enable":
            self._enable()
        elif action == "disable":
            self._disable()

    def _load_config(self) -> None:
        conf = _load_secrets_conf()
        self._token = conf.get("DISCORD_BOT_TOKEN", "")
        try:
            self._channel_id = int(conf.get("DISCORD_CHANNEL_ID", "0") or "0")
        except ValueError:
            self._channel_id = 0
        self._allowed_users = _parse_ids(conf.get("DISCORD_ALLOWED_USERS", ""))

    def _configured(self) -> bool:
        return bool(self._token and self._channel_id and self._allowed_users)

    def _enable(self) -> None:
        if self._enabled or self._starting:
            return
        self._load_config()
        if not self._configured():
            self._log_add("Missing Discord config in secrets.conf", 8)
            return
        self._starting = True
        self._thread = threading.Thread(
            target=self._discord_worker,
            name="discord-remote",
            daemon=True,
        )
        self._thread.start()
        self._log_add("Starting Discord remote", 10)

    def _disable(self) -> None:
        self._enabled = False
        self._starting = False
        client = self._client
        loop = self._loop
        if client is not None and loop is not None:
            try:
                asyncio.run_coroutine_threadsafe(client.close(), loop)
            except Exception:
                pass
        self._client = None
        self._loop = None
        self._log_add("Discord remote disabled", 10)

    def _discord_worker(self) -> None:
        try:
            import discord
        except ImportError:
            self._starting = False
            self._log_add("Install optional dependency: discord.py", 8)
            return

        intents = discord.Intents.default()
        intents.message_content = True

        plugin = self
        client = discord.Client(intents=intents)
        self._client = client

        @client.event
        async def on_ready():
            plugin._enabled = True
            plugin._starting = False
            plugin._log_add(f"Connected as {client.user}", 11)

        @client.event
        async def on_message(message):
            if message.author.bot:
                return
            if message.channel.id != plugin._channel_id:
                return
            if message.author.id not in plugin._allowed_users:
                plugin._audit(message.author.id, message.content, "rejected")
                return
            content = message.content.strip()
            if not content.lower().startswith(PREFIX):
                return
            command = content[len(PREFIX):].strip().lower()
            if not command:
                await message.channel.send(plugin._help_text())
                return

            def reply(text: str) -> None:
                text = text[:MAX_REPLY] or "OK"
                try:
                    asyncio.run_coroutine_threadsafe(
                        message.channel.send(text), plugin._loop)
                except Exception:
                    pass

            plugin._requests.put(_RemoteRequest(
                user_id=message.author.id,
                command=command,
                reply=reply,
            ))

        try:
            self._loop = asyncio.new_event_loop()
            asyncio.set_event_loop(self._loop)
            self._loop.run_until_complete(client.start(self._token))
        except Exception as exc:
            self._log_add(f"Discord error: {exc}", 8)
        finally:
            self._enabled = False
            self._starting = False
            self._client = None
            self._loop = None

    def _drain_requests(self) -> None:
        for _ in range(8):
            try:
                req = self._requests.get_nowait()
            except queue.Empty:
                return
            result = self._handle_remote_command(req.user_id, req.command)
            req.reply(result)

    def _handle_remote_command(self, user_id: int, command: str) -> str:
        self._audit(user_id, command, "accepted")
        parts = command.split()
        head = parts[0] if parts else ""
        if head in ("help", "?"):
            return self._help_text()
        if head == "status":
            return self._status_text()
        if head == "loot":
            return self._loot_text()
        if head == "tail":
            return self._tail_text()
        if head == "scan":
            target = parts[1] if len(parts) > 1 else ""
            return self._scan(target)
        if head == "sdr":
            target = parts[1] if len(parts) > 1 else ""
            return self._sdr(target)
        if head == "stop":
            return self._stop_all()
        if head == "disable":
            self._disable()
            return "Discord remote disabled. Re-enable locally from PLUGINS."
        return f"Unknown command: {command}\n\n{self._help_text()}"

    def _status_text(self) -> str:
        app = self.app
        if not app:
            return "App not ready"
        ops = []
        if getattr(app, "wifi_scanning", False):
            ops.append("wifi wardrive")
        if getattr(app, "ble_scanning", False):
            ops.append("bt wardrive")
        if getattr(app, "_wifi_scan_only", False):
            ops.append("wifi scan")
        if getattr(app, "_ble_scan_only", False):
            ops.append("bt scan")
        if getattr(app.state, "portal_running", False):
            ops.append("evil portal")
        if getattr(app.state, "evil_twin_running", False):
            ops.append("evil twin")
        gps = "fix" if getattr(app, "gps_fix", False) else "no fix"
        return "\n".join([
            "Watch Dogs Go status",
            f"ESP32: {'connected' if getattr(app, '_esp32', False) else 'offline'}",
            f"GPS: {gps} sats:{getattr(app, 'gps_sats', 0)}",
            f"Level: {getattr(app, 'level', '?')} {getattr(app, 'level_title', '')}",
            f"Running: {', '.join(ops) if ops else 'idle'}",
        ])

    def _loot_text(self) -> str:
        app = self.app
        if not app:
            return "App not ready"
        try:
            app._load_loot_totals()
        except Exception:
            pass
        t = getattr(app, "_loot_totals", {})
        passwords = t.get("passwords", 0) + t.get("et_captures", 0)
        return "\n".join([
            "Loot totals",
            f"Sessions: {t.get('sessions', 0)}",
            f"WiFi nets: {t.get('wifi', 0)}",
            f"BT devices: {t.get('bt', 0)}",
            f"Handshakes: {t.get('pcap', 0)}",
            f"HC22000: {t.get('hs', 0)}",
            f"Credential captures: {passwords}",
        ])

    def _tail_text(self) -> str:
        app = self.app
        if not app:
            return "App not ready"
        try:
            with app._term_lock:
                lines = list(app.terminal_lines[-12:])
        except Exception:
            lines = []
        if not lines:
            return "Terminal is empty"
        try:
            from watchdogs.privacy import mask_line
            lines = [mask_line(line) for line in lines]
        except Exception:
            pass
        return "Recent terminal lines:\n" + "\n".join(lines)[-MAX_REPLY:]

    def _scan(self, target: str) -> str:
        app = self.app
        if not app:
            return "App not ready"
        if app._anything_running_on_esp():
            return "Refusing to start scan while another ESP32 operation is running"
        if target == "wifi":
            app._start_scan_cmd("scan_networks", "wifi_scan", "Remote WiFi Scan")
            return "Started WiFi scan"
        if target in ("bt", "ble"):
            app._start_scan_cmd("scan_bt", "ble_scan", "Remote BT Scan")
            return "Started BT scan"
        return "Usage: !wdg scan wifi | !wdg scan bt"

    def _sdr(self, target: str) -> str:
        app = self.app
        if not app:
            return "App not ready"
        if not getattr(app, "_sdr_enabled", False):
            return "SDR is off. Enable it locally first: SYSTEM > SDR"
        if target == "status":
            if not app._sdr.running:
                return "SDR is enabled; no receiver is running"
            parts = [f"SDR mode: {app._sdr.mode}"]
            if "adsb" in app._sdr.mode:
                n_pos = sum(1 for a in app._sdr.aircraft.values()
                            if a.has_position)
                parts.append(
                    f"ADS-B aircraft: {n_pos}/{len(app._sdr.aircraft)} positioned")
            if "433" in app._sdr.mode:
                parts.append(
                    f"433 MHz sensors: {app._sdr.total_sensors_seen}")
            return "\n".join(parts)
        if target in ("adsb", "ads-b"):
            if app._sdr.running and "adsb" in app._sdr.mode:
                return "ADS-B is already running"
            app._sdr_adsb()
            return "Started ADS-B receiver"
        if target in ("433", "433mhz"):
            if app._sdr.running and "433" in app._sdr.mode:
                return "433 MHz scanner is already running"
            app._sdr_433()
            return "Started 433 MHz scanner"
        if target == "stop":
            if not app._sdr.running:
                return "No SDR receiver is running"
            app._sdr.stop()
            app.msg("[Discord] SDR stop", 10)
            app._term_add("[SDR] Remote stop", raw=True)
            return "Stopped SDR receivers"
        return "Usage: !wdg sdr status | !wdg sdr adsb | !wdg sdr 433 | !wdg sdr stop"

    def _stop_all(self) -> str:
        app = self.app
        if not app:
            return "App not ready"
        try:
            app._send("stop")
            app.wifi_scanning = False
            app.ble_scanning = False
            app._wifi_scan_only = False
            app._ble_scan_only = False
            app.sniffing = False
            app.capturing_hs = False
            app.state.portal_running = False
            app.state.evil_twin_running = False
            app.msg("[Discord] Remote stop", 10)
            return "Stop sent"
        except Exception as exc:
            return f"Stop failed: {exc}"

    def _help_text(self) -> str:
        return "\n".join([
            "Commands:",
            "!wdg status",
            "!wdg loot",
            "!wdg tail",
            "!wdg scan wifi",
            "!wdg scan bt",
            "!wdg sdr status",
            "!wdg sdr adsb",
            "!wdg sdr 433",
            "!wdg sdr stop",
            "!wdg stop",
            "!wdg disable",
        ])

    def _audit(self, user_id: int, command: str, result: str) -> None:
        line = f"user={user_id} result={result} cmd={command}"
        self._log_add(line, 13)
        if self.app and getattr(self.app, "loot", None):
            try:
                self.app.loot.log_attack_event(f"DISCORD_REMOTE: {line}")
            except Exception:
                pass

    def _log_add(self, text: str, color: int = 13) -> None:
        self._log.append((text, color))
        if len(self._log) > 120:
            self._log = self._log[-120:]
        if self.app and hasattr(self.app, "_term_lock"):
            self.app._term_add(f"[Discord] {text}", raw=True)
