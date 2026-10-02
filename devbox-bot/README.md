# devbox_bot — Grok Bot ↔ DevBox bridge (plugin/bot)

> Vendored from DevBox `plugin/bot/` at source commit `26af73b1` (2026-10-02).

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
   ├─ blueprint startup → provision.py listener (:7813)
   │    └─ relay credentials → mode-0600 creds.env → start-box.sh
   │    ├─ devbox_bot.box  on 127.0.0.1:7812  (inference endpoint)
   │    └─ host-main       on 0.0.0.0:1340    (SAND_* env)
   ├─ preview-link :7813 → provisioning listener
   └─ preview-link :1340 → relay gateway → desktop
```

`devbox_bot.desktop` is the "core domain" the packaged app is pointed
at. It implements the client's Connect services
(`DashboardService`/`AiService`/`GrokBotService`/`InferenceService`/
`AnalyticsService`/`AgentService` catch-all) over the DevBox APIs above.

`provision.py` is a stdlib-only listener started by the org blueprint.
It accepts allow-listed credentials over guest port 7813, writes
`creds.env` with mode 0600, and starts `start-box.sh` without an agent
turn. `GET /health` reports `awaiting`, `provisioning`, or `provisioned`;
`GET /status` reports the current startup phase, process pids, disk and
`/home/box` details, and a redacted tail of the startup logs.
`start-box.sh` uses the packaged Node runtime and HTTP/1.1 retry flags for
runtime-pack downloads.

`devbox_bot.box` runs inside the box VM and serves `/sand-box/inference-credential` +
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

`plugin/bot/tools/configure_devbox.py` creates or updates:

1. org secrets `DEVBOX_API_KEY`, `GROKBOT_LLM_BASE_URL` /
   `GROKBOT_LLM_API_KEY` / `GROKBOT_LLM_MODEL`
2. an org-target blueprint that stages `start-box.sh` and `provision.py`
   and starts the listener on `0.0.0.0:7813` (`--provision-idle-s`
   defaults to 900 seconds)

Set `DEVBOX_API_KEY` and the three `GROKBOT_LLM_*` variables in the process
environment (for example, source a mode-0600 env file without printing it),
then run:

```bash
python plugin/bot/tools/configure_devbox.py --org <org>
```
```

`EnsureSandBox` creates a v3 session with the credentials in both
`session_secrets` and a POST to the listener through a preview-link
capability for port 7813. It then waits for the gateway on port 1340.
If the listener is unreachable continuously for 120 seconds only, the
desktop sends an agent-staging follow-up and records `provision_path:
"agent"`; otherwise it records `provision_path: "provision"`. Reuse
re-provisions if the listener reports `awaiting`.

## Live verification

The post-rotation fresh-box check reached gateway health in 20.049 seconds
with `provision_path=provision`; guest `/status` was captured during
provisioning. An ordinary non-box session showed the listener idling out
without a runtime download. The strict 12-step smoke currently records
10/12: direct inference was rejected with `API_KEY_QUOTA_EXHAUSTED`, and the
guest transcript endpoint returned 404, leaving the no-`exec` assertion
unverified. See `.devin/evidence/5cbcbfbd-grokbot-provision/` for details.

## Security

- State file (`~/.local/state/devbox-bot/state.json` or
  `%LOCALAPPDATA%\devbox-bot\state.json`) is mode 0600 and stores token/
  refresh maps plus per-box metadata keyed by `org:user`; box tokens are
  per-box random. Never dump or log this file.
- `DEVBOX_API_KEY` headless mode is *refused* unless the env var is set;
  it is for single-user/CI only.
- `devbox_bot.box` binds 127.0.0.1; the host-main gateway requires its
  own `SAND_GATEWAY_TOKEN` + relay network capability.
- Secrets are passed via env/session-secrets, never argv or logs.

## Limits

- Ordinary sessions start only the short-lived listener; absent a
  provisioning POST it exits after the configured idle timeout without
  downloading the runtime.
- `BOX_PROMPT` does not start services. Agent staging is only the
  120-second listener-unreachable fallback.
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
