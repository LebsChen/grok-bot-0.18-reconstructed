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
│  starts devbox_bot.      │  local    │ oauth/token         │           │  stage box/start-box.sh       │
│  desktop :7811           │  loopback │ v3 sessions API     │           │ agent prompt → run script     │
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
  tagged `grok-bot-box`; a blueprint `dep` startup command stages
  `box/start-box.sh` and the session prompt runs it once (secrets env only
  reaches exec tool calls, so the agent dumps it to `creds.env` first).
  The script fetches the runtime pack (GitHub release, parallel byte-range
  download + sha256 verify), starts `devbox_bot.box` (:7812) and host-main
  (:1340, `SAND_GATEWAY_BIND_HOST=0.0.0.0`, token auth).
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
`devbox-gateway-smoke.mjs` with the `DEVBOX_API_KEY` Actions secret. The
vendored `devbox-bot/` copy is synced from DevBox `plugin/bot/` (sync
source commit noted in `devbox-bot/README.md`).

## 5. Known limits

- Each box start costs one agent turn (startup commands only run at
  session bootstrap; secrets env only exists in exec tool calls).
- Guest egress to GitHub is slow — the parallel download mitigates it.
- Cloudflare blocks `Python-urllib/*` user agents on the relay; probes
  must set a custom UA.
- host-main needs `/home/box` to exist (script creates it via sudo) and
  enough disk — the tarball is deleted after extraction.
