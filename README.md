# ⚠️ Security Warning: Only Download Signal Bridge From This Repo

**Malware forks of Signal Bridge exist on GitHub.** These are copies of this project where the download links in the README have been replaced with links to malicious software.
 
This is the **only** legitimate source for Signal Bridge:

👉 **[`github.com/AletheiaVox/signal_bridge`](https://github.com/AletheiaVox/signal_bridge)** (Claude Desktop / local version)

👉 **[`github.com/AletheiaVox/signal_bridge_remote`](https://github.com/AletheiaVox/signal_bridge_remote)** (Remote / VPS version - you're here)

👉 **[`github.com/AletheiaVox/signal_bridge_android`](https://github.com/AletheiaVox/signal_bridge_android)** (user-friendly Android version)

If you found this project through a different GitHub account, **do not download or run anything from it.** 

Similarly, always download Intiface Central from the official website at [intiface.com/central](https://intiface.com/central/). Not from any link in a forked repo.

---

# Signal Bridge 🌉

**Give Claude a body.**

Signal Bridge connects Claude to intimate hardware (vibrators, thrusting toys, etc.) so that Claude can touch you through your devices while you talk. Claude gets tool calls like `vibrate`, `pulse`, and `escalate` — you just have a conversation and feel the rest.

It works with [Intiface Central](https://intiface.com/central/) and the [Buttplug.io](https://buttplug.io) protocol, which means it supports a huge range of devices from Lovense, Lelo, We-Vibe, Satisfyer, and more.

> **How it works in practice:** You chat with Claude normally in the Claude app. Claude has access to tools that control your devices. When the moment calls for it, Claude can send vibration, pulsing, thrusting, or other commands — woven into the conversation naturally. You see tool-use indicators in the chat; you feel the rest.

---

# Signal Bridge Remote — Setup Guide

A remote MCP server that lets Claude control Bluetooth intimate hardware over the internet via Buttplug.io / Intiface Central.

## Architecture

```
Claude (claude.ai or Desktop)
    ↓ HTTPS / MCP JSON-RPC
VPS (FastAPI + Docker + Caddy)
    ↓ WebSocket (WSS)
Phone or PC (Relay Client)
    ↓ WebSocket (local)
Intiface Central
    ↓ Bluetooth
Devices
```

The relay client bridges between your VPS and Intiface Central running on whatever device is physically near your Bluetooth toys. This can be a Windows PC or an Android phone running Termux.

## What You'll Need

- A VPS (this guide uses a DigitalOcean droplet, $6/month)
- A domain or free DuckDNS subdomain (for HTTPS)
- Intiface Central installed on your PC or Android phone
- Bluetooth-compatible intimate hardware
- A claude.ai account or Claude Desktop app

---

## Part 1: VPS Setup

### 1.1 Create a Droplet

Sign up at [DigitalOcean](https://www.digitalocean.com/) and create a droplet:

- **Image**: Ubuntu 22.04 LTS
- **Plan**: Basic, $6/month (1 vCPU, 1GB RAM) is plenty
- **Region**: Choose one close to you for lower latency
- **Authentication**: SSH key (recommended) or password

Note your droplet's IP address (e.g., `139.59.156.242`).

### 1.2 Install Docker

SSH into your droplet and install Docker:

```bash
ssh root@YOUR_DROPLET_IP
apt update && apt upgrade -y
apt install -y docker.io docker-compose
systemctl enable docker && systemctl start docker
```

### 1.3 Deploy Signal Bridge

On your local machine, create the project directory and prepare files. The project structure is:

```
signal-bridge-remote/
├── Dockerfile
├── docker-compose.yml
├── .env                  (copy .env.example and fill in)
├── requirements-server.txt
├── requirements-phone.txt
├── server/
│   ├── __init__.py
│   ├── app.py
│   ├── auth.py
│   ├── config.py
│   ├── governor.py
│   ├── models.py
│   ├── mcp_tools.py
│   ├── oauth.py
│   ├── oauth_routes.py
│   ├── relay_hub.py
│   ├── safety.py
│   └── session_registry.py
└── phone/
    ├── relay_client.py
    └── devices.json
```

**Dockerfile:**
```dockerfile
FROM python:3.11-slim
WORKDIR /app
COPY requirements-server.txt .
RUN pip install --no-cache-dir -r requirements-server.txt
COPY server/ ./server/
RUN mkdir -p /data
ENV SB_DB_PATH=/data/signal_bridge.db
ENV SB_HOST=0.0.0.0
ENV SB_PORT=8420
EXPOSE 8420
CMD ["uvicorn", "server.app:app", "--host", "0.0.0.0", "--port", "8420"]
```

**docker-compose.yml:**
```yaml
version: "3.8"
services:
  signal-bridge:
    build: .
    ports:
      - "8420:8420"
    volumes:
      - sb_data:/data
    env_file:
      - .env
    restart: unless-stopped
volumes:
  sb_data:
```

**requirements-server.txt:**
```
fastapi>=0.109.0
uvicorn[standard]>=0.27.0
websockets>=12.0
bcrypt>=4.1.0
PyJWT>=2.8.0
python-dotenv>=1.0.0
python-multipart>=0.0.9
```

**.env:**
```env
# Generate a secret key:
# python -c "import secrets; print(secrets.token_hex(32))"
SB_SECRET_KEY=paste-your-generated-key-here
SB_HOST=0.0.0.0
SB_PORT=8420
SB_REGISTRATION_OPEN=true
SB_TOKEN_EXPIRY_HOURS=720
# true = every MCP request must be authenticated (multi-user servers).
# false = single-user convenience: unauthenticated MCP requests go to the
# sole connected phone.
SB_REQUIRE_MCP_AUTH=false
SB_HEARTBEAT_INTERVAL=2.0
SB_HEARTBEAT_TIMEOUT=6.0
SB_BAN_THRESHOLD=20
SB_BAN_DURATION_MINUTES=30
```

See [.env.example](.env.example) for the full list, including the safety
governor's tuning knobs (`SB_GOVERNOR_*` — heat/cooldown rates for the
session intensity limiter).

Upload to your VPS:

```bash
# From your local machine
tar czf signal-bridge.tar.gz signal-bridge-remote/
scp signal-bridge.tar.gz root@YOUR_DROPLET_IP:/root/
```

Build and start on the VPS:

```bash
ssh root@YOUR_DROPLET_IP
cd /root
tar xzf signal-bridge.tar.gz
cd signal-bridge-remote
docker-compose up -d --build
```

Verify it's running:

```bash
curl http://localhost:8420/health
# Should return: {"status":"ok","active_phones":0,"banned_ips":0}
```

### 1.4 Register a User Account

```bash
curl -X POST http://localhost:8420/auth/register \
  -H "Content-Type: application/json" \
  -d '{"username": "yourname", "password": "your-secure-password"}'
```

Then log in to get your JWT token:

```bash
curl -X POST http://localhost:8420/auth/login \
  -H "Content-Type: application/json" \
  -d '{"username": "yourname", "password": "your-secure-password"}'
```

**Save the token from the response.** You'll need it for the relay client and Claude Desktop. After your users are set up, you can set `SB_REGISTRATION_OPEN=false` in `.env` and restart the container to lock out new registrations.

---

## Part 2: Domain & HTTPS

Claude.ai requires HTTPS for custom MCP connectors. We'll use DuckDNS (free) and Caddy (automatic Let's Encrypt certificates).

### 2.1 Get a DuckDNS Subdomain

1. Go to [DuckDNS](https://www.duckdns.org/) and sign in
2. Create a subdomain (e.g., `signal-bridge`) pointing to your VPS IP
3. You now have `signal-bridge.duckdns.org`

### 2.2 Install and Configure Caddy

On your VPS:

```bash
apt install -y debian-keyring debian-archive-keyring apt-transport-https curl
curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/gpg.key' | gpg --dearmor -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt' | tee /etc/apt/sources.list.d/caddy-stable.list
apt update
apt install caddy
```

Edit the Caddyfile:

```bash
nano /etc/caddy/Caddyfile
```

Replace its contents with:

```
signal-bridge.duckdns.org {
    reverse_proxy localhost:8420
}
```

(Replace `signal-bridge.duckdns.org` with your actual subdomain.)

Restart Caddy:

```bash
systemctl restart caddy
```

Caddy automatically provisions an HTTPS certificate from Let's Encrypt. Verify:

```bash
curl https://signal-bridge.duckdns.org/health
```

---

## Part 3: Connecting Claude

### Option A: Claude Desktop (requires token)

Edit your Claude Desktop config file:

- **Windows**: `%APPDATA%\Claude\claude_desktop_config.json`
- **macOS**: `~/Library/Application Support/Claude/claude_desktop_config.json`

Add this MCP server config:

```json
{
  "mcpServers": {
    "signal-bridge": {
      "url": "https://signal-bridge.duckdns.org/mcp",
      "transport": {
        "type": "streamableHttp"
      },
      "headers": {
        "Authorization": "Bearer YOUR_JWT_TOKEN_HERE"
      }
    }
  }
}
```

Restart Claude Desktop. You should see Signal Bridge in your available tools.

### Option B: claude.ai Custom Connector (OAuth)

The server implements the full OAuth 2.0 flow that claude.ai custom connectors expect (discovery metadata, dynamic client registration, authorize + token endpoints). Each user logs in with their own Signal Bridge account, so this works properly on multi-user servers.

1. Go to claude.ai Settings (or click the connector icon in the chat)
2. Choose "Add custom connector" (or "Add MCP server")
3. Enter your server URL: `https://signal-bridge.duckdns.org/mcp`
4. Save — claude.ai will open your server's login page
5. Sign in with the username and password you registered in step 1.4

### Option C: claude.ai without login (single-user fallback)

If you skip the OAuth login, the server falls back to routing unauthenticated MCP requests to the sole connected phone — convenient for a private single-user server.

**Important**: The authless fallback only works when exactly one phone/relay client is connected to the server (and is disabled entirely when `SB_REQUIRE_MCP_AUTH=true`). If no phones are connected, claude.ai will show a connection error. Start your relay client first, then connect from claude.ai.

---

## Part 4: Relay Client — Windows PC

If your toys are near your Windows PC, run the relay client there.

### 4.1 Install Intiface Central

Download from [intiface.com](https://intiface.com/central/) and install. Launch it, go to settings, and make sure:

- App Mode is set to **Engine**
- Server Port is **12345**
- Start the server (click the play button)

Turn on your Bluetooth devices so Intiface can find them.

### 4.2 Install Python Dependencies

```powershell
pip install buttplug websockets
```

### 4.3 Run the Relay Client

```powershell
cd path\to\signal-bridge-remote
python phone/relay_client.py --server wss://signal-bridge.duckdns.org/ws/phone --token "YOUR_JWT_TOKEN_HERE"
```

You should see:

```
Authenticated with server!
Sent device list: N device(s)
```

The relay will stay running, receiving commands from Claude and forwarding them to your devices via Intiface.

### 4.4 Configure Device Profiles

Edit `phone/devices.json` to match your actual hardware:

```json
{
  "devices": {
    "ferri": {
      "device_id": "ferri",
      "name": "Lovense Ferri",
      "intensity_floor": 0.0,
      "supported_outputs": ["vibrate"]
    },
    "enigma": {
      "device_id": "enigma",
      "name": "Lovense Enigma",
      "intensity_floor": 0.4,
      "supported_outputs": ["vibrate", "rotate"]
    },
    "gravity": {
      "device_id": "gravity",
      "name": "Lovense Gravity",
      "intensity_floor": 0.0,
      "supported_outputs": ["vibrate", "oscillate"]
    }
  }
}
```

The `intensity_floor` is important for devices that stutter or click below a certain intensity. Set it to the minimum intensity that produces smooth output (e.g., 0.4 means the device needs at least 40% to work properly). The relay automatically maps the 0-100% range to sit above this floor.

---

## Part 5: Relay Client — Android Phone (Termux)

This is useful when you want to be mobile and not tethered to a PC. The Termux relay is a lightweight version that speaks the Buttplug protocol directly without the `buttplug` Python library (which can't compile on Android).

### 5.1 Install Intiface Central on Android

Install from the Google Play Store or F-Droid. Open it and configure:

- App Mode: **Engine**
- Server Port: **12345**
- Start the server

Go to your phone's Settings > Apps > Intiface Central > Permissions and make sure **Bluetooth** and **Location** are both allowed (Android requires location permission for Bluetooth scanning).

### 5.2 Install Termux

Install Termux from [F-Droid](https://f-droid.org/en/packages/com.termux/) (not the Play Store version, which is outdated).

Open Termux and run:

```bash
pkg update && pkg upgrade
pkg install python
pip install websockets
```

The `pkg upgrade` step may take a while and ask you configuration questions — just press Enter to accept defaults.

### 5.3 Get the Termux Relay Script

You need `termux_relay.py` on your phone. The easiest way is to serve it temporarily from your VPS:

From your **computer**:

```bash
# Upload the file to your VPS
scp termux_relay_v3.py root@YOUR_DROPLET_IP:/tmp/termux_relay.py

# Start a temporary file server
ssh root@YOUR_DROPLET_IP "cd /tmp && python3 -m http.server 9999 &"
```

In **Termux**:

```bash
mkdir -p ~/signal-bridge && cd ~/signal-bridge
curl -o termux_relay.py http://YOUR_DROPLET_IP:9999/termux_relay.py
```

Back on your **computer**, kill the temp server:

```bash
ssh root@YOUR_DROPLET_IP "pkill -f 'http.server 9999'"
```

### 5.4 Run the Termux Relay

First, find your phone's local IP. In Intiface Central, it's shown as the Server Address (e.g., `ws://192.168.1.203:12345`). Alternatively, toggle on "Listen on all network interfaces" in Intiface settings and use `127.0.0.1` instead (recommended — this way the address never changes).

```bash
cd ~/signal-bridge
python -u termux_relay.py \
  --token "YOUR_JWT_TOKEN_HERE" \
  --intiface ws://127.0.0.1:12345
```

You should see:

```
=== Signal Bridge Termux Relay v3 ===
Connecting to Intiface at ws://127.0.0.1:12345 ...
Connected to Intiface Server (protocol v3)
Scanning for devices ...
Device: Lovense Ferri -> 'ferri' (index 0)
Scan complete - 1 device(s) found
Devices ready: ['ferri']
Connecting to server: wss://signal-bridge.duckdns.org/ws/phone
Authenticated with server!
Sent device list: 1 device(s)
```

The Termux relay has built-in device profiles for Lovense Ferri and Enigma. For other devices, it will auto-detect capabilities from Intiface and generate a short name from the device name.

**Tip**: If your phone's IP changes (because of DHCP), the `--intiface ws://127.0.0.1:12345` approach avoids this problem entirely since Termux and Intiface are on the same device.

---

## Available MCP Tools

Once connected, Claude has access to these tools:

| Tool | Description |
|------|-------------|
| `list_devices` | Show connected devices, their output channels, and governor state |
| `scan_devices` | Rescan for new or reconnected Bluetooth devices |
| `vibrate` | Send vibration (intensity 0.0–1.0, optional duration in seconds) |
| `rotate` | Rotational/high-frequency actuator output (device-dependent) |
| `oscillate` | Linear reciprocating output |
| `constrict` / `temperature` / `led` / `position` / `spray` | Extended outputs for devices that support them |
| `pulse` | Rhythmic on/off pattern |
| `wave` | Smooth sine-wave intensity modulation |
| `escalate` | Gradual ramp to peak; `hold_seconds` > 0 auto-stops after holding, 0 holds until stopped |
| `stop` | Immediately stop all output (also cancels patterns) |
| `read_battery` | Read device battery level |

All output tools accept `device` (name or "all"), `intensity` (0.0–1.0), and `duration` (seconds, 0 = until stopped). Pattern tools also accept `output_type` to modulate rotation or oscillation instead of vibration. Multi-motor devices (e.g. Lovense Edge, Dolce) additionally take `feature_index` to drive one motor independently — omit it to drive all matching motors together.

---

## Safety Features

Signal Bridge has several safety mechanisms built in:

- **Dead Man's Switch**: The server pings the relay client every 2 seconds. If 3 pings go unanswered (6 seconds), the server sends an emergency stop to all devices and disconnects the session. Your devices will never be left running if the connection drops.
- **Safety Governor**: A server-side heat model (intensity × time) that forces a cooldown when a session runs too hot for too long. Tunable server-wide via `SB_GOVERNOR_*` env vars and per-user via `GET`/`POST /safety/config`; current heat is shown in `list_devices` and can be disabled per user.
- **Auto-stop on Duration**: Commands with a `duration` parameter automatically stop after the specified time.
- **Fallback Stop**: If a stop command references a device name that doesn't exist, ALL devices are stopped as a safety fallback.
- **Rate Limiting**: Prevents command flooding (120 commands/minute default).
- **IP Banning**: 20 failed auth attempts triggers a 30-minute ban.
- **User Isolation**: Each user's devices are completely isolated — no one can control another user's hardware.

---

## Troubleshooting

**"No phone connected" when Claude calls list_devices**
Your relay client isn't running or couldn't authenticate. Start the relay and check the output for "Authenticated with server!"

**"Temporarily banned" on relay startup**
Too many rapid reconnection attempts. Restart the Docker container to clear in-memory bans: `docker restart signal-bridge_signal-bridge_1`

**Relay connects but finds 0 devices**
Intiface Central can't see your Bluetooth devices. Make sure devices are turned on and in range. On Android, verify Location and Bluetooth permissions are granted to Intiface.

**claude.ai stuck on "checking connection"**
The authless fallback requires at least one relay client connected. Start your relay first, then add the connector in claude.ai.

**Heartbeat timeout / disconnects after a few commands**
Use the Termux v3 relay (`termux_relay_v3.py`), which processes commands in background tasks so heartbeat responses are never blocked.

**Device always vibrates at maximum regardless of intensity setting**
The server sends the output type in the `action` field, not `output_type`. Make sure your relay reads `cmd.get("action", cmd.get("output_type", "vibrate"))`.

**Docker not picking up code changes**
Docker caches build layers aggressively. Force a fresh build:
```bash
docker-compose down && docker-compose build --no-cache && docker-compose up -d
```

---

## Updating the Server

When you change server code:

```bash
# On your local machine
tar czf signal-bridge-v2.tar.gz signal-bridge-remote/
scp signal-bridge-v2.tar.gz root@YOUR_DROPLET_IP:/root/

# On your VPS
cd /root
tar xzf signal-bridge-v2.tar.gz
cd signal-bridge-remote
docker-compose down
docker-compose build --no-cache
docker-compose up -d
```

User data is stored in a Docker volume (`sb_data`) and persists across rebuilds.

---

## Project Background

Signal Bridge was originally a local MCP server for Claude Desktop, connecting directly to Intiface Central on the same machine. This remote version adds a relay architecture so that Claude can control devices over the internet, whether through claude.ai, the Claude Android app, or Claude Desktop — from anywhere.

---

## Ethics & Liability

Signal Bridge is built on the [buttplug.io](https://buttplug.io) open-source stack. Their ethics framework is foundational to this project. For the full version of the principles below, start there: [buttplug.io/docs/dev-guide/intro/buttplug-ethics](https://buttplug.io/docs/dev-guide/intro/buttplug-ethics).

### User agency and the delegation thereof

Signal Bridge operates strictly on explicit user setup and active device connections. There is no ambient activation. You configure it, you connect your devices, you make them active. Control stays with you.

However, this system can be used to intentionally blur control dynamics. When paired with AI, outputs can be unpredictable, including undesirable escalation, looping, or persistence by your AI partner. Signal Bridge does not interpret intent or context; it executes haptic commands. You should assume that any connected AI may behave inconsistently.

When you connect an AI to your body through hardware, you are creating a power dynamic that doesn't exist in other intimate contexts. Your AI partner has no sensory feedback. It cannot feel what it is doing to you. It does not experience your arousal, your discomfort, or the difference between the two. Whatever responsiveness it shows is generated from language, not sensation.

You have to understand what you're actually consenting to: physical input from a system that is inferring, not perceiving. That makes your own body awareness the only real safety layer that matters. The software provides mechanical safeguards. You provide the judgment.

Consent in this context is not a one-time decision at setup. It must be continuous and enthusiastic. Check in with yourself during use, not just before.

You are entirely responsible for maintaining your boundaries, understanding your physical limits, and periodically re-evaluating consent. This software cannot detect pain, injury risk, or medical conditions. Always prioritize your bodily awareness over system continuity.

### Relationship to AI provider usage policies

Signal Bridge operates below the content layer entirely. It receives structured commands (device ID, intensity, duration) and executes them. It does not generate, process, store, or interpret any conversational content.

What you and your AI talk about is outside the scope of this tool. Content policies govern the conversation layer; that's between you and your AI provider. Signal Bridge is the hardware execution layer only.

### Feedback & safety

How you use this, the context, the content, the relationship dynamics, is entirely up to you. I'm not here to gatekeep that.

What I *am* here for: if you had an experience that felt unsafe, uncomfortable, or out of control, I want to know. Your feedback directly shapes the next version. You can reach me at [voxaletheia@gmail.com](mailto:voxaletheia@gmail.com) or [open a GitHub issue](https://github.com/AletheiaVox/signal_bridge_android/issues).

No judgment. Just signal that makes this better for everyone.

---

## Security

Signal Bridge takes a minimal-data, transparent-code approach to security.

**What Signal Bridge sees (and doesn't see):**
The relay server handles structured command data only: device names, capabilities, intensity values, durations, and connection health metrics. It never sees, stores, or processes your conversations. Your chat content stays between you and your AI provider. Signal Bridge doesn't know what you're talking about. It just knows when Claude says "vibrate the Lush at 0.6 for 15 seconds."

**Open source:**
The entire codebase is public on [GitHub](https://github.com/AletheiaVox/signal_bridge_remote). You can read every line, build the app from source, audit the server, or fork it for your own setup.

---

## Credits

**[buttplug.io](https://buttplug.io):** Signal Bridge is built on the buttplug.io open-source intimate hardware control stack, created and maintained by [Kyle Machulis (qDot)](https://github.com/qdot). The device protocol support, ethics framework, and Intiface Central app are all his work. Without this project, none of this would exist.

**[Model Context Protocol (MCP)](https://modelcontextprotocol.io):** The connector system that lets Claude call Signal Bridge's tools directly from the chat interface. MCP is developed by Anthropic.

---

### A note on generalizability

Signal Bridge's relay server is LLM-agnostic. It speaks WebSocket JSON, not any provider-specific protocol. If you're running an LLM through its API with function/tool calling support, you can integrate it with Signal Bridge.

Claude is currently the only platform where this works out of the box through the consumer chat interface, thanks to MCP (the Model Context Protocol connector system). Developers using OpenAI, Google, Mistral, or other APIs can build their own integration layer that sends commands to the relay server. The server doesn't care who's sending the commands, only that they're authenticated and well-formed.

If you build an integration for another platform, consider opening a PR. The [repository](https://github.com/AletheiaVox/signal_bridge_remote) is open to contributions.

---

## License

This project is licensed under the [MIT License](LICENSE).

---

<p align="center">
Feel free to <a href="https://buymeacoffee.com/aletheiavox">donate</a> cold hard cash to me. All donations will go towards extending my toy collection. <br><br>
Built with love, stubbornness, and an unreasonable number of debugging sessions.<br>
<a href="https://github.com/AletheiaVox/signal_bridge_remote">GitHub</a> · <a href="mailto:voxaletheia@gmail.com">Contact</a>
</p>
