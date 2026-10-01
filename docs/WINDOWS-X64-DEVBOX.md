# Grok Bot 0.18 Reconstructed — Windows x64 desktop on a DevBox plugin backend

Status: design + implementation guide. Every claim below is tagged with its
evidence class: **[artifact]** (bytes of the preserved 0.18.0 releases),
**[probe]** (observed protocol on a running process), **[source]** (this
repository's reconstructed source), **[plan]** (not yet verified).

## 1. Problem statement

Grok Bot 0.18 is split into two halves:

* the **desktop** — an Electron app (`dist/electron-main`, `dist/renderer`,
  `dist/node-agent-coordinator`, preload, native deps) that the user runs;
* the **box host** — `dist/host/host-main.cjs`, which runs agents, tools, MCP,
  plugins, rooms and automations and serves one HTTP + SSE *gateway* on
  port 1340 inside a Linux container (Cursor's `sand-box` image) **[artifact]**.

Upstream connects the two through Cursor's cloud ("anyrun") box service or a
local Docker container. This project keeps the desktop unchanged in behaviour,
builds it for **Windows x64**, and replaces the box service with a **DevBox
session VM** in which the box host runs as a **DevBox plugin** (`grok-bot-box`).
DevBox only contributes the middle layer: plugin lifecycle, an authenticated
relay, capability tokens. The Devin production bundle and the guest image are
not modified.

## 2. Architecture

```
 Windows x64 PC                          DevBox control plane              DevBox node / guest VM (Debian 12)
┌───────────────────────────┐   HTTPS   ┌──────────────────────┐          ┌──────────────────────────────────────┐
│ devbox-connect.mjs        │──────────▶│ PUT /api/preview-link │ mint     │ plugin grok-bot-box (MCP: gbbox_*)   │
│  (API key → capability)   │◀──────────│  /{devin_id}?local_   │ v1.cap   │   ├─ install.sh → runtime pack       │
│                           │           │  port=1340|1341       │          │   ├─ node22 host-main.cjs :1340      │
│ Grok Bot.exe (0.18 recon.)│           └──────────────────────┘          │   │    (Bearer gateway token)        │
│  SAND_HOST_GATEWAY_URL ───┼──HTTPS───▶ node relay  s-<sid>-1340.<relay> ─┼──▶│ GET /health  POST /api/<cmd>    │
│  SAND_HOST_GATEWAY_TOKEN  │  header x-anyrun-network-token: v1.<cap>   │   │ GET /events (SSE, streamed)      │
│  ..._NETWORK_TOKEN        │  header Authorization: Bearer <gateway>    │   └─ descriptor :1341 GET /descriptor│
└───────────────────────────┘                                            └──────────────────────────────────────┘
```

## 3. Component ownership

| Component | Owner | Change policy |
|---|---|---|
| Devin production bundle (webapp) | Devin | unchanged (domain only) |
| DevBox control plane (`app/`) | DevBox | reuse `preview-link`; no new public route |
| DevBox node relay (`remote/node_relay.py`) | DevBox | header capability + SSE streaming for `preview:*` |
| Guest image (Debian 12) | Devin | unchanged; plugin installs into `$HOME` |
| `grok-bot-box` plugin | DevBox `examples/plugins/grok-bot-box` | new |
| Box runtime pack | built by `tools/build_grokbot_box_runtime.sh` | derived artifact, stored in object store `plugin-assets/grok-bot-box/` |
| Desktop (Windows) | this repo | `bootstrap:win`, `package:win`, `devbox:connect` |

## 4. Plugin manifest and lifecycle

```
grok-bot-box/
  .devin-plugin/plugin.json      name, description, version, skills/agents dirs
  .mcp.json                      stdio MCP server: python3 scripts/gbbox_mcp.py
  scripts/install.sh             fetch + sha256-verify + extract runtime pack (idempotent)
  scripts/gbbox_mcp.py           tools: gbbox_ensure, gbbox_start, gbbox_status, gbbox_stop
  scripts/descriptor.py          :1341 GET /descriptor, /healthz
  skills/grok-bot-box/SKILL.md   when/how the agent starts the box and calls browser_preview
```

Lifecycle: session start → agent loads plugin → `gbbox_start` (install if
needed, create gateway token `$PLUGIN_DATA/gateway-token` mode 0600, spawn host
and descriptor, wait for `/health`) → agent calls `browser_preview` for 1340
(registers the hosted service, mints capability) → user runs
`devbox-connect` on Windows → `gbbox_stop` or session end kills both processes
(the guest is destroyed with the session; durable host data lives in
`$PLUGIN_DATA/sand-data`).

## 5. Guest runtime layout

Runtime pack `grok-bot-box-runtime-0.18.0-debian12-x64.tar.gz`:

```
node                       Node v22.14.0 linux-x64 (= /exec-daemon/node of sand-box image) [artifact]
opt-sand/deps/             /opt/sand/deps from the sand-box image, with
                           tree-sitter rebuilt on Debian 12 (GLIBCXX ≤ 3.4.30) [probe]
                           cursor-proclist .node removed (needs GLIBC 2.38; host degrades to
                           os.* metrics — verified the gateway still serves) [probe]
opt-sand/sand-host/        image sand-host dir; host-main.cjs replaced by the 0.18.0 Windows
                           ASAR's dist/host/host-main.cjs so desktop and box host are the same
                           release (this mirrors LocalDockerHostConnector bind-mounting the
                           app's own host-main.cjs over the image copy) [artifact]
MANIFEST.json              sha256 of every input
```

Why rebuilt: the image is Debian 13 (glibc 2.41); the DevBox Linux guest is
Debian 12 (glibc 2.36). `tree_sitter_runtime_binding.node` needs
`GLIBCXX_3.4.31` and fails `ERR_DLOPEN_FAILED`; shipping trixie libstdc++ fails
on `GLIBC_2.38` **[probe]**.

Host environment (same as `LocalDockerHostConnector` **[source]**):
`SAND_PACKAGED=1 SAND_HOST_IN_BOX=1 SAND_BOX_AUTO_UPDATE=0
SAND_GATEWAY_BIND_HOST=127.0.0.1 SAND_HOST_PORT=1340
SAND_GATEWAY_TOKEN=<file> SAND_GATEWAY_REQUIRE_AUTH=1
SAND_DATA_ROOT=$PLUGIN_DATA/sand-data NODE_PATH=<deps>
SAND_TREE_SITTER_NODE_DEPS=<deps>`. Binding to 127.0.0.1 + require-auth means
the only ingress is the relay.

## 6. Desktop ↔ DevBox protocol

Gateway (unchanged upstream, `source/host/gateway-server.ts`):

| Request | Auth | Observed |
|---|---|---|
| `GET /health` | none | 200 JSON `{ok,pid,isBusy,…}` **[probe]** |
| `POST /api/<command>` JSON | Bearer gateway token | 401 without, 200 with **[probe]** |
| `GET /events` `Accept: text/event-stream` | Bearer | 401 without, incremental frames with **[probe]** |
| any with browser `Origin` | — | 403 **[probe]** |

Relay hop: `https://s-<session>-1340.<relay authority>/<path>`. The relay
authenticates the capability, strips its own header and forwards
`Authorization` untouched to `127.0.0.1:1340` in the guest.

## 7. Authentication, issuance, rotation

Two independent secrets:

1. **Relay capability** (`v1.` HMAC token, scope `preview:<port>`, bound to
   session id + guest IP, TTL `DEVBOX_NODE_RELAY_CAPABILITY_TTL`, default 3600 s).
   Issued by `PUT /api/preview-link/{devin_id}?local_port=` to an authenticated
   org member (cookie or DevBox API key `cog_`/`apk_`). The desktop already
   sends `x-anyrun-network-token` on every gateway request
   (`GATEWAY_NETWORK_TOKEN_HEADER` in the 0.18.0 `main.cjs` **[artifact]**), so
   the relay accepts the capability from that header for `preview:*` services.
2. **Gateway token** — random 32 bytes generated in the guest, never leaves
   `$PLUGIN_DATA` except through the descriptor service on 1341, which is itself
   behind a `preview:1341` capability.

Rotation: capabilities expire; `devbox-connect` re-mints and relaunches. The
gateway token rotates with `gbbox_stop` + `gbbox_start` (new file). Neither
secret is written to plugin state visible to boards, to `.devin`, or to logs.

## 8. Session / plugin identity

One box host per DevBox session; identity = DevBox `devin_id`. The plugin id is
`account-upload:org:grok-bot-box` (org-scoped upload, editable in Customize).
The desktop account scope comes from its own sign-in; the env-descriptor
connector does not require a Cursor account to connect **[source]**.

## 9. HTTP and SSE streaming

The relay previously buffered (`upstream.content`) with a 60 s timeout, which
makes `/events` unusable. For `preview:*`: requests are sent with
`stream=True`; `text/event-stream` responses are returned as a
`StreamingResponse` over `aiter_raw()` with no read timeout, no compression,
`Cache-Control: no-cache`, `X-Accel-Buffering: no`. Other preview responses are
read fully and follow the old path (Location rewrite, compression, cookies).
Non-preview services are unchanged.

## 10. Inference routing

The 0.18.0 host (artifact) only talks to Cursor's inference backend
(`api2.cursor.sh`); the OpenRouter / Codex / Claude Code providers exist only in
this repository's *reconstructed* host source **[source]** and are not in the
artifact host **[artifact: no `openrouter.ai` string in host-main.cjs]**.
Therefore: agent turns through the DevBox box require a Cursor sign-in in the
desktop, exactly as upstream. Routing inference through DevBox's LLM proxy is
phase 4 (requires the clean-source host activation, see §15).

## 11. Windows x64 packaging

Inputs: `research-archives/original/0.18.0/windows-x64/Grok_Bot_0.18.0_Setup.exe`
(LFS, sha256 `464079a1…f7e437e`) → NSIS `$PLUGINSDIR/app-64.7z` → `app/`
(`Grok Bot.exe`, `resources/app.asar` sha256 `38e85c0e…66e0`,
`resources/app.asar.unpacked`) **[artifact]**.

Fuses of `Grok Bot.exe` **[artifact, @electron/fuses read]**:
`EnableEmbeddedAsarIntegrityValidation` Disabled, `OnlyLoadAppFromAsar`
Disabled → replacing `app.asar` does not require patching the exe.

Target selection: `GROK_BOT_TARGET=win32-x64` (default `darwin-arm64`, so
existing macOS behaviour and checks are byte-for-byte unchanged).
`scripts/with-target.mjs win32-x64 <script>` sets it portably.

| Step | Command | Result |
|---|---|---|
| bootstrap | `npm run bootstrap:win` | `.cache/runtime/win32-x64/app`, payload hydrated into `.cache/source-payloads/win32-x64/app` |
| package | `npm run package:win` | `dist/Grok Bot 0.18 Reconstructed-win32-x64/` (unmodified exe, rebuilt ASAR, `productName` = `Grok Bot 0.18 Reconstructed` → separate `%APPDATA%` profile) + `reconstructed-package.json` |
| connect | `npm run devbox:connect -- --session <devin_id>` | launches the package with gateway env |

Extraction uses `7z` (`GROK_BOT_7Z` override; preinstalled on GitHub
`windows-latest`). The ASAR build runs on any OS; launching requires Windows.

Packaging mode: `package-windows.mjs` uses the plain `buildAsar` path —
the Windows upstream payload (win32 `sourceAppDir`) plus runtime
`deps`/`native` and the reconstructed electron-main with a `productName`
override. The macOS fidelity gate is **not applicable**: the release
fidelity audit pins the darwin-arm64 renderer, and the Windows renderer
inventory differs (it is missing 5 SVGs from
`frontend/manifests/renderer-runtime-assets.json`), so feeding the win32
payload through `buildFidelityReconstructedAsar` fails the audit by
design. `reconstructed-package.json` records this as `buildMode`
`windows-payload-plus-reconstructed-electron-main`.

## 12. Failure recovery

| Failure | Detection | Recovery |
|---|---|---|
| host crash | `gbbox_status` / `/health` fails | `gbbox_start` restarts with same token + data root |
| capability expired | relay 401/403 | re-run `devbox-connect` |
| SSE drop | gateway-client reconnect backoff **[source]** | automatic |
| guest destroyed | preview-link 404/503 | new session, `gbbox_start` |
| pack checksum mismatch | install.sh | abort, re-fetch |

## 13. Security / trust boundaries

* Gateway binds the guest interface (`GBBOX_BIND_HOST`, default `0.0.0.0`)
  so the relay can reach it; every request still requires the Bearer
  gateway token (`SAND_GATEWAY_REQUIRE_AUTH=1`) — the relay is the only
  ingress. The descriptor on :1341 additionally restricts peers to
  loopback plus the guest's IPv4 default gateway (the node host running
  the relay; extendable via `GBBOX_DESCRIPTOR_ALLOW`).
* Relay capability is per-session, per-port, short-lived; header is stripped
  before forwarding.
* Browser origins are rejected by the gateway (403) so preview pages cannot
  drive it.
* API keys stay on the Windows machine (env `DEVBOX_API_KEY`), never passed to
  the desktop process.
* No credentials in `.devin`, plugin state, screenshots, CI logs (masked).

## 14. Compatibility matrix

| Desktop | Box host | Guest | Status |
|---|---|---|---|
| macOS arm64 reconstructed | local Docker sand-box | — | upstream path, unchanged |
| Windows x64 reconstructed | DevBox plugin, 0.18.0 host-main | Debian 12 | this work |
| Windows x64 | local Docker | — | upstream connector; requires Docker Desktop (not tested) |
| Linux desktop | — | — | not built |

## 15. Implementation phases

1. Relay: header capability + SSE streaming (DevBox).
2. Plugin `grok-bot-box` + runtime pack + descriptor (DevBox).
3. Windows target: bootstrap/package/connect + `windows-x64` CI (this repo).
4. Inference via DevBox LLM proxy (needs clean-source host; out of scope now).

## 16. Test plan and evidence requirements

| Layer | Test | Evidence |
|---|---|---|
| relay unit | header auth, foreign scope rejected, SSE first frame before upstream completes, non-SSE unchanged | pytest output |
| plugin in real session | `gbbox_start` in a DevBox session, `/health` through relay, 401 without Bearer, 200 with, SSE frames with timing | HTTP bodies, timings |
| desktop build | `npm run check`, `npm run package:win` on Linux (cross) | file inventory + sha256 |
| Windows runtime | GitHub `windows-latest`: bootstrap:win, package:win, launch `Grok Bot.exe` with gateway env, CDP screenshot, guest-side access log shows desktop requests | CI log, screenshot artifact, relay/host log lines |

Host-only probes are not browser or Windows evidence.

## 17. Known limitations

* Agent turns need Cursor sign-in (§10).
* `cursor-proclist` disabled on Debian 12 → process metrics fall back.
* Capability lifetime bounds a desktop run (default 1 h) until a refresh hook
  (`issueLocalExecDaemonCredential`-style) is wired.
* Code signing: the Windows package is unsigned (exe unchanged from the
  Anysphere-signed original, but the ASAR differs; Authenticode covers only
  the exe).

## 18. Rollback

* Relay: the change is confined to `preview:*`; revert the commit and
  `tools/deploy-devbox.sh --only relay`.
* Plugin: uninstall `grok-bot-box` in Customize; nothing else depends on it.
* Desktop: delete `dist/…-win32-x64`; macOS targets never read win32 paths.
