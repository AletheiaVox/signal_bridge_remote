# Changelog

## v1.1 — 2026-07-07

This release brings the remote server up to date with everything that
shipped in the [Android edition](https://github.com/AletheiaVox/signal_bridge_android)
since March, plus fixes found while porting. If you run a server from an
older clone, rebuild the Docker image (`docker-compose build --no-cache`)
and update your relay client — both sides changed.

### Added

- **OAuth 2.0 support.** Full flow for claude.ai custom connectors:
  discovery metadata, dynamic client registration, a login page at
  `/oauth/authorize`, and a token endpoint. Each user signs in with their
  own account — no more sharing one token or relying on the single-phone
  fallback. The fallback still works for single-user servers and can be
  disabled with `SB_REQUIRE_MCP_AUTH=true`.
- **Safety governor.** Server-side heat model (intensity × time) that
  forces a cooldown when a session runs hot for too long. Server defaults
  via `SB_GOVERNOR_*` env vars, per-user overrides via
  `GET`/`POST /safety/config`, current state surfaced in `list_devices`
  and piggybacked on heartbeat pings.
- **`feature_index` on every output and pattern tool.** Multi-motor
  devices (Lovense Edge, Dolce, …) can now have each motor driven
  independently. Implemented in both relay clients; a bad index returns a
  helpful error listing the valid ones.
- **`CHANGELOG.md`, `.env.example`, `.gitignore`, `.gitattributes`.**

### Changed

- **Neutral engineering terminology** in tool schemas and
  `devices.json`, matching the Android edition. Content filters on some
  LLM platforms refused tools whose schemas contained explicit anatomical
  language; the reworded schemas work across providers. `list_devices`
  now also shows what each output channel does.
- Duplicate same-model devices get suffixed short names (`lush`,
  `lush_2`) instead of silently replacing each other.
- Relay clients no longer push an unsolicited device list after
  authenticating; the server requests a scan on connect and the scan
  response covers it.

### Fixed

- **MCP protocol compliance:** JSON-RPC notifications (e.g.
  `notifications/initialized`) now get the bare `202` the spec requires
  instead of a malformed error, and `initialize` echoes the client's
  `protocolVersion` when supported. Fixes connection failures with strict
  clients.
- **OAuth token endpoint** 500'd when a client POSTed the token request
  as `multipart/form-data` (missing `python-multipart` dependency).
- **`governor_enabled`** now round-trips as a real JSON boolean; strict
  clients (the Android app) previously couldn't re-enable the governor
  after disabling it.
- **Patterns with `duration=0` silently did nothing.** They now run
  until an explicit stop, matching plain commands.
- **`escalate` hold contract:** `hold_seconds` ≤ 0 holds at peak until
  stopped, > 0 auto-stops after the hold — and an error mid-ramp can no
  longer leave the device running at the last intensity it reached.
- **Pattern collisions:** starting a pattern cancels the previous
  pattern on that device (they used to fight over the actuator), and a
  direct command now supersedes a running pattern instead of being
  overwritten by it.
- **Stale auto-stops:** a `duration` auto-stop from an earlier command
  could fire minutes later and silently kill output a newer command had
  started. Auto-stops are now tracked per channel and cancelled when
  something newer takes over.
- Pattern timing now uses a monotonic clock; an NTP wall-clock jump
  could stretch, truncate, or instantly end a pattern.
- Repo hygiene: removed accidentally committed compiled bytecode
  (`__pycache__`) and a stray deploy tarball.

## v1.0 — 2026-03-14

Initial public release: FastAPI relay server (MCP over HTTPS, JWT auth,
rate limiting, dead man's switch), Windows/desktop relay client, and the
Termux relay for Android.
