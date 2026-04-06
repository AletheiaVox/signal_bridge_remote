# ⚠️ Security Warning: Only Download Signal Bridge From This Repo

**Malware forks of Signal Bridge exist on GitHub.** These are copies of this project where the download links in the README have been replaced with links to malicious software.
 
This is the **only** legitimate source for Signal Bridge:

👉 **[`github.com/AletheiaVox/signal_bridge`](https://github.com/AletheiaVox/signal_bridge)** (Claude Desktop / local version)

👉 **[`github.com/AletheiaVox/signal_bridge_remote`](https://github.com/AletheiaVox/signal_bridge_remote)** (Remote / VPS version - you're here)

👉 **[`github.com/AletheiaVox/signal_bridge_android`](https://github.com/AletheiaVox/signal_bridge_android)** (user-friendly Android version)

If you found this project through a different GitHub account, **do not download or run anything from it.** 

Similarly, always download Intiface Central from the official website at [intiface.com/central](https://intiface.com/central/). Not from any link in a forked repo.

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
├── .env
├── requirements-server.txt
├── server/
│   ├── __init__.py
│   ├── app.py
│   ├── auth.py
│   ├── config.py
│   ├── models.py
│   ├── mcp_tools.py
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
SB_HEARTBEAT_INTERVAL=2.0
SB_HEARTBEAT_TIMEOUT=6.0
SB_BAN_THRESHOLD=20
SB_BAN_DURATION_MINUTES=30
```

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

### Option B: claude.ai Custom Connector (authless)

Claude.ai supports custom MCP connectors without authentication, using a fallback mechanism: when only one phone is connected, all MCP requests are routed to that phone automatically.

1. Go to claude.ai Settings (or click the connector icon in the chat)
2. Choose "Add custom connector" (or "Add MCP server")
3. Enter your server URL: `https://signal-bridge.duckdns.org/mcp`
4. Leave authentication as "None"
5. Save

**Important**: The authless fallback only works when exactly one phone/relay client is connected to the server. If no phones are connected, claude.ai will show a connection error. Start your relay client first, then connect from claude.ai.

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
| `list_devices` | Show connected devices and their capabilities |
| `scan_devices` | Rescan for new or reconnected Bluetooth devices |
| `vibrate` | Send vibration (intensity 0.0–1.0, optional duration in seconds) |
| `rotate` | Rotation or sonic output (device-dependent) |
| `oscillate` | Thrusting/oscillation output |
| `pulse` | Rhythmic on/off pattern |
| `wave` | Smooth sine-wave intensity modulation |
| `escalate` | Gradual ramp from 0 to peak, with optional hold |
| `stop` | Immediately stop all output (also cancels patterns) |
| `read_battery` | Read device battery level |

All output tools accept `device` (name or "all"), `intensity` (0.0–1.0), and `duration` (seconds, 0 = until stopped). Pattern tools also accept `output_type` to modulate rotation or oscillation instead of vibration.

---

## Safety Features

Signal Bridge has several safety mechanisms built in:

- **Dead Man's Switch**: The server pings the relay client every 2 seconds. If 3 pings go unanswered (6 seconds), the server sends an emergency stop to all devices and disconnects the session. Your devices will never be left running if the connection drops.
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

Built with love, stubbornness, and an unreasonable number of debugging sessions.
