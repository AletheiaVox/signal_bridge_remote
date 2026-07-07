"""End-to-end verification of the signal-bridge-remote server:
boot, health, MCP protocol fixes, full OAuth round-trip, safety config.

Run from anywhere:  python tests/verify_server.py
Needs the server deps plus httpx (for fastapi.testclient). No hardware,
no network — everything runs in-process against a throwaway database.
"""
import os
import sys
import tempfile
from pathlib import Path

os.environ["SB_SECRET_KEY"] = "test-secret-key-for-verification-only"
os.environ["SB_DB_PATH"] = os.path.join(tempfile.mkdtemp(), "test.db")

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from fastapi.testclient import TestClient  # noqa: E402
from server.app import app  # noqa: E402

PASS = []
FAIL = []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(("  ok  " if cond else "  FAIL") + f" {name}" + (f" — {detail}" if detail and not cond else ""))


with TestClient(app) as client:
    # ── Boot & health ────────────────────────────────────────────────
    r = client.get("/health")
    check("health endpoint", r.status_code == 200 and r.json().get("status") == "ok", r.text)

    # ── MCP protocol fixes ───────────────────────────────────────────
    # Notification (no id) must get bare 202, even unauthenticated
    r = client.post("/mcp", json={"jsonrpc": "2.0", "method": "notifications/initialized"})
    check("notification -> 202", r.status_code == 202, f"got {r.status_code}")

    # Unauthenticated request with id, no phone -> 401
    r = client.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})
    check("unauthenticated initialize -> 401", r.status_code == 401, f"got {r.status_code}")

    # ── OAuth discovery ──────────────────────────────────────────────
    r = client.get("/.well-known/oauth-authorization-server")
    meta = r.json() if r.status_code == 200 else {}
    check("oauth metadata", r.status_code == 200 and "authorization_endpoint" in meta, r.text[:200])

    # ── User + client registration ───────────────────────────────────
    r = client.post("/auth/register", json={"username": "testuser", "password": "hunter2hunter2"})
    check("user registration", r.status_code == 200, r.text[:200])

    r = client.post("/oauth/register", json={
        "client_name": "Verify Client",
        "redirect_uris": ["http://localhost:9999/callback"],
    })
    creds = r.json() if r.status_code == 201 else {}
    check("client registration", r.status_code == 201 and "client_id" in creds, r.text[:200])

    # ── Authorize: login page renders ────────────────────────────────
    r = client.get("/oauth/authorize", params={
        "client_id": creds.get("client_id", ""),
        "redirect_uri": "http://localhost:9999/callback",
        "response_type": "code",
        "state": "xyzzy",
    })
    check("authorize login page", r.status_code == 200 and "password" in r.text.lower(), r.text[:200])

    # ── Authorize: login submits, code issued ────────────────────────
    r = client.post("/oauth/authorize", data={
        "username": "testuser",
        "password": "hunter2hunter2",
        "client_id": creds.get("client_id", ""),
        "redirect_uri": "http://localhost:9999/callback",
        "state": "xyzzy",
    }, follow_redirects=False)
    loc = r.headers.get("location", "")
    check("authorize -> redirect with code", r.status_code == 302 and "code=" in loc and "state=xyzzy" in loc, f"{r.status_code} {loc[:120]}")
    auth_code = ""
    if "code=" in loc:
        from urllib.parse import parse_qs, urlparse
        auth_code = parse_qs(urlparse(loc).query).get("code", [""])[0]

    # ── Token exchange (JSON body) ───────────────────────────────────
    r = client.post("/oauth/token", json={
        "grant_type": "authorization_code",
        "code": auth_code,
        "client_id": creds.get("client_id", ""),
        "client_secret": creds.get("client_secret", ""),
    })
    tok = r.json() if r.status_code == 200 else {}
    access = tok.get("access_token", "")
    check("token exchange", r.status_code == 200 and access, r.text[:200])

    # ── Token endpoint parses urlencoded forms (request.form() path —
    #    the code path that needed python-multipart on pinned Starlette) ──
    r2 = client.post("/oauth/authorize", data={
        "username": "testuser", "password": "hunter2hunter2",
        "client_id": creds.get("client_id", ""),
        "redirect_uri": "http://localhost:9999/callback", "state": "s2",
    }, follow_redirects=False)
    from urllib.parse import parse_qs, urlparse
    code2 = parse_qs(urlparse(r2.headers.get("location", "")).query).get("code", [""])[0]
    r2 = client.post("/oauth/token", data={
        "grant_type": "authorization_code",
        "code": code2,
        "client_id": creds.get("client_id", ""),
        "client_secret": creds.get("client_secret", ""),
    })
    check("token exchange via urlencoded form", r2.status_code == 200 and r2.json().get("access_token"), f"{r2.status_code} {r2.text[:120]}")

    # ── Authenticated MCP: initialize with version negotiation ───────
    hdrs = {"Authorization": f"Bearer {access}"}
    r = client.post("/mcp", headers=hdrs, json={
        "jsonrpc": "2.0", "id": 2, "method": "initialize",
        "params": {"protocolVersion": "2025-06-18"},
    })
    body = r.json() if r.status_code == 200 else {}
    ver = body.get("result", {}).get("protocolVersion", "")
    sess = r.headers.get("mcp-session-id", "")
    check("initialize echoes supported version", r.status_code == 200 and ver == "2025-06-18", f"{r.status_code} ver={ver}")
    check("session id issued", bool(sess))

    r = client.post("/mcp", headers=hdrs, json={
        "jsonrpc": "2.0", "id": 3, "method": "initialize",
        "params": {"protocolVersion": "1999-01-01"},
    })
    ver = r.json().get("result", {}).get("protocolVersion", "") if r.status_code == 200 else ""
    check("unsupported version falls back", ver == "2025-03-26", f"ver={ver}")

    # ── tools/list: feature_index present, neutral terminology ───────
    r = client.post("/mcp", headers=hdrs, json={"jsonrpc": "2.0", "id": 4, "method": "tools/list"})
    tools = {t["name"]: t for t in r.json().get("result", {}).get("tools", [])} if r.status_code == 200 else {}
    vib = tools.get("vibrate", {})
    props = vib.get("inputSchema", {}).get("properties", {})
    check("tools/list returns tools", len(tools) >= 10, f"{len(tools)} tools")
    check("vibrate has feature_index", "feature_index" in props)
    check("vibrate requires device", vib.get("inputSchema", {}).get("required") == ["device"])
    esc = tools.get("escalate", {})
    check("escalate hold contract documented", "hold indefinitely" in esc.get("description", ""))
    all_text = str(tools)
    check("neutral terminology", "clitoral" not in all_text and "thrusting" not in all_text)

    # ── tools/call without phone: graceful error, not a crash ────────
    r = client.post("/mcp", headers=hdrs, json={
        "jsonrpc": "2.0", "id": 5, "method": "tools/call",
        "params": {"name": "list_devices", "arguments": {}},
    })
    txt = str(r.json()) if r.status_code == 200 else r.text
    check("tools/call no-phone graceful", r.status_code == 200 and "No phone connected" in txt, txt[:150])

    # ── Safety config round-trip (governor_enabled bool fix) ─────────
    r = client.get("/safety/config", headers=hdrs)
    check("GET /safety/config", r.status_code == 200 and r.json().get("governor_enabled") is True, r.text[:200])
    r = client.post("/safety/config", headers=hdrs, json={"governor_enabled": False, "heat_rate": 2.5})
    check("POST /safety/config", r.status_code == 200, r.text[:200])
    r = client.get("/safety/config", headers=hdrs)
    j = r.json() if r.status_code == 200 else {}
    check("governor_enabled returns JSON false (not 0)", j.get("governor_enabled") is False, r.text[:200])
    r = client.post("/safety/config", headers=hdrs, json={"governor_enabled": True})
    r = client.get("/safety/config", headers=hdrs)
    j = r.json() if r.status_code == 200 else {}
    check("governor re-enables (one-way ratchet fixed)", j.get("governor_enabled") is True, r.text[:200])

print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
sys.exit(1 if FAIL else 0)
