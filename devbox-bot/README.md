# devbox_bot — Grok Bot ↔ DevBox bridge (plugin/bot)

A standalone backend that lets the Grok Bot desktop (Cursor fork) run
entirely against a DevBox deployment — sign-in, account, box allocation
and inference — **with zero DevBox code changes** and *not* as a
Customize plugin. It only talks to interfaces DevBox already exposes:

- OIDC `authorize` + `oauth/token` on `https://auth.devinai.net`
- v3 session APIs on `https://app.devinai.net`
- `PUT /api/preview-link/{session}` capability minting
- org secrets + blueprints (admin APIs)

## Architecture

```
Grok Bot.exe (Windows desktop)
   │  SAND_BACKEND_URL=CURSOR_API_BASE_URL=CURSOR_WEBSITE_URL
   │  = http://127.0.0.1:7811
   ▼
devbox_bot.desktop  ── Connect RPCs (aiserver.v1/agent.v1) ──┐
   │  /loginDeepControl, /auth/poll, /oauth/token            │ DevBox
   │  /sand-box/* (desktop-side inference only)              │ APIs
   ▼                                                        ▼
DevBox v3 session "grok-bot-box" (Debian_12 box)
   ├─ blueprint startup → box/start-box.sh
   │    ├─ devbox_bot.box  on 127.0.0.1:7812  (inference endpoint)
   │    └─ host-main       on 0.0.0.0:1340    (SAND_* env)
   └─ preview-link capability → relay gateway → desktop
```

`devbox_bot.desktop` is the "core domain" the packaged app is pointed
at. It implements the client's Connect services
(`DashboardService`/`AiService`/`GrokBotService`/`InferenceService`/
`AnalyticsService`/`AgentService` catch-all) over the DevBox APIs above.

`devbox_bot.box` runs inside the box VM (started by the blueprint
startup command) and serves `/sand-box/inference-credential` +
`InferenceService.Stream` so the in-box host-main's model calls end at
your OpenAI-compatible endpoint instead of Cursor.

## Run

```bash
pip install -r plugin/bot/requirements.txt
python -m devbox_bot.desktop --port 7811
```

Then launch the packaged app with:

```
SAND_BACKEND_URL=http://127.0.0.1:7811
CURSOR_API_BASE_URL=http://127.0.0.1:7811
CURSOR_WEBSITE_URL=http://127.0.0.1:7811
```

## Configuration

Desktop (local env):

| var | purpose |
|---|---|
| `DEVBOX_API_KEY` | headless/CI mode: any poll completes with a local token mapped to this key |
| `GROKBOT_LLM_BASE_URL` / `_API_KEY` / `_MODEL` | desktop-side inference endpoint |
| `GROKBOT_LLM_MODEL_MAP` | JSON `{"client-model":"provider-model"}` |
| `GROKBOT_MODELS` | comma-separated model catalog for AvailableModels |
| `GROKBOT_RUNTIME_URL` | box runtime tar.gz URL (`.sha256` fetched alongside) |

Box (in the VM, set by session/org secrets):

| var | purpose |
|---|---|
| `GROKBOT_GATEWAY_TOKEN` | per-box gateway token (session secret) |
| `GROKBOT_INFERENCE_CREDENTIAL` | renewal credential box.py validates |
| `GROKBOT_LLM_*` | org secrets → guest inference |

## DevBox setup

`plugin/bot/tools/configure_devbox.py` creates, idempotently:

1. org secrets `GROKBOT_LLM_BASE_URL` / `GROKBOT_LLM_API_KEY` /
   `GROKBOT_LLM_MODEL`
2. a repo-target blueprint for `LebsChen/grok-bot-0.18-reconstructed`
   whose startup command is `box/start-box.sh`

```bash
python plugin/bot/tools/configure_devbox.py \
    --org <org> --api-key <admin key> \
    --llm-base-url ... --llm-api-key ... --llm-model ...
```

`EnsureSandBox` then creates a v3 session with `repos=[<repo>]`,
`session_secrets={GROKBOT_GATEWAY_TOKEN, GROKBOT_INFERENCE_CREDENTIAL,
GROKBOT_RUNTIME_URL}` and `secret_ids=<the three org secrets>`, and the
blueprint startup command runs `start-box.sh` inside the fresh VM.

## Security

- State file (`~/.local/state/devbox-bot/state.json` or
  `%LOCALAPPDATA%\devbox-bot\state.json`) is mode 0600 and maps
  issued tokens → DevBox credentials; box tokens are per-box random.
- `DEVBOX_API_KEY` headless mode is *refused* unless the env var is set;
  it is for single-user/CI only.
- `devbox_bot.box` binds 127.0.0.1; the host-main gateway requires its
  own `SAND_GATEWAY_TOKEN` + relay network capability.
- Secrets are passed via env/session-secrets, never argv or logs.

## Limits

- Each box start costs one agent turn (the v3 session prompt).
- Startup commands run only at session bootstrap — recreating a box
  deletes the session and reallocates.
- The OIDC interactive flow requires a DevBox web session in the
  browser the app opens; headless mode exists for CI.
- Unhandled Connect methods return a valid empty response and are
  logged once as `grokbot.rpc.unhandled <Service>/<Method>`; query
  `GET /debug/unhandled` on the desktop backend for the list.

## Regenerating descriptors

`devbox_bot/descriptors.json` is generated from the fork's protobuf-es
runtime types:

```bash
GROKBOT_FORK_ROOT=/path/to/grok-bot \
    node plugin/bot/tools/gen_descriptors.mjs
```
