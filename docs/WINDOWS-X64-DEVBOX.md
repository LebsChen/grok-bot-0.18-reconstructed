# Grok Bot 0.18 Reconstructed — Windows x64 desktop on a DevBox plugin/bot backend

Status: implemented. Live proof recorded in the DevBox repo under
`.devin/evidence/a3a995ea-grokbot/plugin-bot-live/` (12/12 smoke steps).

## 1. Problem statement

Grok Bot 0.18 is split into the Electron **desktop** and the **box host**
(`host-main.cjs`) that runs inside a Linux container. Upstream connects them
through Cursor's cloud backend. This project builds the desktop for
**Windows x64** and replaces the **entire Cursor backend** — sign-in,
account, box allocation and inference — with DevBox, using only
interfaces DevBox already exposes. **Zero DevBox code changes**, and not a
Customize plugin: the bridge is the standalone `devbox_bot` package
(vendored here under `devbox-bot/`, canonical source is DevBox repo
`plugin/bot/`).

## 2. Architecture

```
 Windows x64 PC                          DevBox (unchanged)                DevBox guest VM (Debian 12)
┌──────────────────────────┐           ┌─────────────────────┐           ┌──────────────────────────────┐
│ devbox-connect.mjs       │           │ OIDC authorize +    │           │ blueprint dep startup:        │
│  starts devbox_bot.      │  local    │ oauth/token         │           │ stage provision.py + script  │
│  desktop :7811           │  loopback │ v3 sessions API     │           │ listener :7813, relay creds   │
│ Grok Bot.exe             │           │ preview-link caps   │           │  ├─ devbox_bot.box :7812      │
│  SAND_BACKEND_URL ───────┼──────────▶│ org secrets +       │  secrets  │  │  (inference → org LLM)      │
│  CURSOR_API_BASE_URL ────┤  Connect  │ blueprints          │  env      │  └─ host-main :1340 (0.0.0.0) │
│  =127.0.0.1:7811         │           └─────────────────────┘           │   SAND_GATEWAY_TOKEN auth     │
└──────────────────────────┘                                             └──────────────────────────────┘
         desktop ──HTTPS──▶ relay preview capability ──▶ gateway :1340
```

* **Login** — `devbox_bot.desktop` drives DevBox OIDC
  (authorize→callback→poll) and returns the DevBox-issued tokens in the
  client's `{accessToken, refreshToken, authId}` shape. Headless CI mode
  (`DEVBOX_API_KEY`) mints a local JWT instead.
* **Box** — `GrokBotService.EnsureSandBox` find-or-creates a DevBox session
  tagged `grok-bot-box`; the blueprint stages `box/provision.py` and
  `box/start-box.sh`, then starts the stdlib provisioning listener on
  `0.0.0.0:7813`. The desktop relays allow-listed credentials to that
  listener, which writes mode-0600 `creds.env` and launches the startup
  script. It fetches the runtime pack (GitHub release, parallel byte-range
  download + sha256 verify), starts `devbox_bot.box` (:7812) and host-main
  (:1340, `SAND_GATEWAY_BIND_HOST=0.0.0.0`, token auth). `GET /status` on
  the listener exposes startup phase, process pids, and redacted log tail.
  Agent staging is fallback only after 120 seconds of listener
  unreachability; a healthy reused listener in `awaiting` is provisioned
  again. Desktop state records `provision_path`.
* **Inference** — `InferenceService.Stream` is served both by desktop.py
  (direct client calls) and by box.py inside the VM (host-main renewal
  credential → access token → Stream → org-secret LLM provider).
* **Everything else** — DashboardService/AiService/AnalyticsService real or
  catch-all (empty proto + `grokbot.rpc.unhandled` log).

## 3. Running

```powershell
$env:DEVBOX_API_KEY = "cog_..."        # or omit for OIDC sign-in window
node scripts/devbox-connect.mjs --bot-dir devbox-bot
```

`devbox-connect.mjs` finds Python 3.11+, pip-installs
`devbox-bot/requirements.txt`, starts `python -m devbox_bot.desktop` on
127.0.0.1:7811, waits for `/healthz`, then launches `Grok Bot.exe` with
`SAND_BACKEND_URL` / `CURSOR_API_BASE_URL` / `SAND_CURSOR_WEBSITE_URL`
pointed at the local backend. Pre-set `SAND_HOST_GATEWAY_*` remains a
debug-only fallback that bypasses the local backend.

## 4. Windows CI

`.github/workflows/windows-x64.yml` — build + package + CDP screenshot as
before; optional `workflow_dispatch` runs `devbox-backend-smoke.mjs` and
`devbox-gateway-smoke.mjs`. The backend smoke step requires the
`DEVBOX_API_KEY` secret and receives `GROKBOT_LLM_BASE_URL`,
`GROKBOT_LLM_API_KEY`, and `GROKBOT_LLM_MODEL`; only the direct Stream check
may skip, and only when `GROKBOT_LLM_API_KEY` is absent. The vendored
`devbox-bot/` copy is synced from DevBox `plugin/bot/` (sync source commit
noted in `devbox-bot/README.md`).

## 5. Known limits

- Deterministic listener provisioning is implemented but remains pending
  live-box verification. The previous 938-second gateway stall is
  undiagnosed; capture guest `/status` during a repro before attributing it.
- Agent staging remains a fallback for listener unreachability; ordinary
  sessions start only the listener and should exit after its idle timeout
  without downloading the runtime.
- Guest egress to GitHub is slow — the parallel download mitigates it.
- Cloudflare blocks `Python-urllib/*` user agents on the relay; probes
  must set a custom UA.
- host-main needs `/home/box` to exist (script creates it via sudo) and
  enough disk — the tarball is deleted after extraction.
