# Discord Remote Plugin

Control Watch Dogs Go remotely from a Discord channel.

---

## 1. Create a Discord Application

1. Go to [discord.com/developers/applications](https://discord.com/developers/applications)
2. Click **New Application**, give it a name (e.g. `WDG-Remote`), click **Create**
3. Go to the **Bot** tab on the left sidebar
4. Click **Add Bot** → **Yes, do it!**
5. Under **Token**, click **Reset Token** and copy the token — you'll need this shortly
6. Under **Privileged Gateway Intents**, enable **Message Content Intent**
7. Click **Save Changes**

---

## 2. Invite the Bot to Your Server

1. Go to the **OAuth2 → URL Generator** tab
2. Under **Scopes**, check `bot`
3. Under **Bot Permissions**, check:
   - `Read Messages / View Channels`
   - `Send Messages`
4. Copy the generated URL, open it in a browser, and invite the bot to your server

---

## 3. Get Your Channel ID and User IDs

Discord IDs require **Developer Mode** to be enabled:

1. Open Discord → **User Settings → Advanced → Developer Mode** → toggle on

**Channel ID:**
Right-click the channel you want the bot to listen in → **Copy Channel ID**

**User IDs** (one per allowed user):
Right-click a username → **Copy User ID**

---

## 4. Configure secrets.conf

Add the following to `secrets.conf` in the project root (copy from `secrets.conf.example`):

```
DISCORD_BOT_TOKEN=your_bot_token_here
DISCORD_CHANNEL_ID=123456789012345678
DISCORD_ALLOWED_USERS=111111111111111111,222222222222222222

# Optional — set to 1 to enable push notifications to the channel:
# DISCORD_PUSH_EVENTS=1
```

- `DISCORD_ALLOWED_USERS` is a comma-separated list of user IDs permitted to send commands
- The bot will silently ignore messages from anyone not on the list
- `DISCORD_PUSH_EVENTS=1` enables the bot to post to the channel automatically (see below)

---

## 5. Enable the Plugin In-Game

1. Open the game and press **TAB** to open the menu
2. Navigate to **PLUGINS** and select **Discord Remote**
3. Select **Enable Discord Remote**
4. The log panel on the right will confirm the bot is connected

The bot runs in the background — you don't need to keep the overlay open.
If the connection drops the plugin will reconnect automatically with backoff.

---

## Commands

| Command | Description |
|---|---|
| `!wdg status` | Game state — ESP32, GPS, running ops |
| `!wdg sys` | Host device — uptime, CPU load, temp, RAM, disk, battery, IP |
| `!wdg loot` | Loot totals for the current session |
| `!wdg tail` | Last 12 lines from the terminal |
| `!wdg menu` | List all remote-accessible menu categories |
| `!wdg menu <cat>` | List items in a category (shows live `●` status) |
| `!wdg menu <cat> <n>` | Run a menu item |
| `!wdg menu <cat> <n> <args>` | Run an item that requires input (BSSID, MAC, SSID) |
| `!wdg scan wifi` | Start/stop WiFi scan |
| `!wdg scan bt` | Start/stop BLE scan |
| `!wdg sdr status` | SDR receiver status |
| `!wdg sdr adsb` | Start ADS-B receiver |
| `!wdg sdr 433` | Start 433 MHz scanner |
| `!wdg sdr stop` | Stop SDR receivers |
| `!wdg stop` | Stop all running operations |
| `!wdg disable` | Disable the bot (re-enable from PLUGINS on device) |

### Menu categories

```
!wdg menu scan        — WiFi Scan, BLE Scan, BT Tracker, AirTag Scan
!wdg menu sniff       — WiFi/BT Wardrive, Packet Sniffer, HS Capture
!wdg menu attack      — Deauth, Blackout, Evil Portal, SAE Flood, ...
!wdg menu addons      — ADS-B Radar, 433 MHz Scanner
!wdg menu system      — Stop All, Reboot ESP32, GPS/LoRa/SDR/USB toggles, ...
```

Items requiring arguments show a usage hint when called without them:

```
!wdg menu scan 3                            → BT Tracker: requires MAC address
!wdg menu scan 3 AA:BB:CC:DD:EE:FF          → starts BT Tracker on that MAC
!wdg menu attack 1 AA:BB:CC:DD:EE:FF 6      → Deauth on BSSID, channel 6
!wdg menu attack 7 FreeWifi                 → Evil Portal with SSID "FreeWifi"
```

---

## Push Notifications

With `DISCORD_PUSH_EVENTS=1` the bot posts to the channel automatically:

| Event | What gets posted |
|---|---|
| WiFi scan round complete | Grouped list of new BSSIDs seen (already-seen skipped) |
| BT scan round complete | Grouped list of new MACs seen (already-seen skipped) |
| Handshake captured | Immediate alert with network name |
| Evil Portal / Evil Twin credential | Spoiler-wrapped credential data |

The overlay status bar shows `push:ON` or `push:off` so you can confirm the setting at a glance.

---

## Install the Dependency

```bash
pip install discord.py
```
