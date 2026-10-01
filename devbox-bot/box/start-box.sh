#!/usr/bin/env bash
# start-box.sh — blueprint startup command for a Grok Bot box session.
#
# No-op unless GROKBOT_GATEWAY_TOKEN is present (session secret set by
# devbox_bot.desktop on EnsureSandBox). Installs the box runtime pack,
# starts devbox_bot.box on 127.0.0.1:7812 and the Grok Bot host-main on
# 0.0.0.0:1340, then exits; children are setsid'd so they outlive the
# startup step.
#
# Env consumed (never echoed):
#   GROKBOT_GATEWAY_TOKEN        session secret → SAND_GATEWAY_TOKEN
#   GROKBOT_INFERENCE_CREDENTIAL session secret → renewal credential
#   GROKBOT_RUNTIME_URL          runtime pack URL (tar.gz + .sha256)
#   GROKBOT_LLM_BASE_URL/_API_KEY/_MODEL   org secrets → inference
set -euo pipefail

# DevBox session secrets only reach *one-shot* exec environments, not
# interactive shells — so the staging step asks the agent to dump them to
# creds.env (0600) first; source it here if present.
if [ -f "$HOME/.grok-bot-box/creds.env" ]; then
  set -a; . "$HOME/.grok-bot-box/creds.env"; set +a
fi

if [ -z "${GROKBOT_GATEWAY_TOKEN:-}" ]; then
  echo "start-box: GROKBOT_GATEWAY_TOKEN unset; skipping box runtime"
  exit 0
fi

GB_HOME="${GROKBOT_HOME:-$HOME/.grok-bot-box}"
PACK_DIR="$GB_HOME/runtime"
LOG_DIR="$GB_HOME/logs"
LOCK="$GB_HOME/start.lock"
mkdir -p "$GB_HOME" "$PACK_DIR" "$LOG_DIR"

exec 9>"$LOCK"
flock -n 9 || { echo "start-box: another run in progress"; exit 0; }

# ── runtime pack ────────────────────────────────────────────────────
# The pack lives in the DevBox plugin-assets mirror, fetched with the
# guest's own harness-LLM token (same mechanism the grok-bot-box plugin's
# install.sh uses).  GROKBOT_RUNTIME_URL may override the base URL.
PACK_NAME="grok-bot-box-runtime-0.18.0-debian12-x64.tar.gz"
RUNTIME_URL="${GROKBOT_RUNTIME_URL:-}"
TOKEN="${DEVBOX_HARNESS_LLM_TOKEN:-}"
ASSET_ORIGIN=""
if [ -n "${DEVBOX_HARNESS_LLM_OPENAI_BASE_URL:-}" ]; then
  ASSET_ORIGIN="${DEVBOX_HARNESS_LLM_OPENAI_BASE_URL%%/internal/harness-llm/*}"
fi
if [ -z "$RUNTIME_URL" ]; then
  if [ -n "$ASSET_ORIGIN" ] && [ "$ASSET_ORIGIN" != \
      "${DEVBOX_HARNESS_LLM_OPENAI_BASE_URL:-}" ]; then
    RUNTIME_URL="$ASSET_ORIGIN/internal/plugin-assets/grok-bot-box/$PACK_NAME"
  else
    # Public GitHub release asset on the Grok Bot fork — the plain
    # (non-plugin) box session has no hlt1 token for the plugin-assets
    # mirror, so this is the default fetch path.
    RUNTIME_URL="https://github.com/LebsChen/grok-bot-0.18-reconstructed/releases/download/devbox-box-runtime-v0.18.0/$PACK_NAME"
  fi
fi
# Keep the pack under its release filename so the published .sha256
# (which embeds that name) verifies with `sha256sum -c`.
PACK="$GB_HOME/$PACK_NAME"
MARKER="$PACK_DIR/.installed"

AUTH=()
if [ -n "$TOKEN" ]; then
  AUTH=(-H "Authorization: Bearer $TOKEN")
fi

if [ ! -f "$MARKER" ]; then
  if [ -z "$RUNTIME_URL" ]; then
    echo "start-box: no runtime URL (GROKBOT_RUNTIME_URL unset and" \
         "no plugin-assets origin)" >&2
    exit 1
  fi
  # Guest egress to public hosts is slow single-stream (~40KB/s); pull
  # the pack as parallel byte-range chunks.  Re-runs resume per part.
  SIZE="$(curl -fsIL --connect-timeout 15 --max-time 30 "${AUTH[@]}" \
      "$RUNTIME_URL" | awk 'BEGIN{IGNORECASE=1} /^content-length:/ {n=$2} END{gsub(/\r/,"",n); print n}')"
  if [ -n "$SIZE" ] && [ "$SIZE" -gt 1048576 ] 2>/dev/null; then
    N=8; PART=$(( (SIZE + N - 1) / N )); pids=()
    rm -f "$GB_HOME"/.part-*
    for i in $(seq 0 $((N - 1))); do
      s=$((i * PART)); e=$((s + PART - 1))
      [ "$e" -ge "$SIZE" ] && e=$((SIZE - 1))
      ( for try in 1 2 3 4 5; do
          have=0; [ -f "$GB_HOME/.part-$i" ] && have=$(stat -c%s "$GB_HOME/.part-$i")
          want=$((e - s + 1))
          [ "$have" -ge "$want" ] && break
          curl -fL --connect-timeout 15 --speed-limit 5120 --speed-time 120 \
            -r $((s + have))-$e "${AUTH[@]}" \
            -o - "$RUNTIME_URL" >> "$GB_HOME/.part-$i" && break
          sleep 2
        done ) &
      pids+=($!)
    done
    wait "${pids[@]}"
    cat "$GB_HOME"/.part-* > "$PACK" && rm -f "$GB_HOME"/.part-*
  else
    curl -fL --connect-timeout 15 --max-time 1200 --retry 3 \
      -C - "${AUTH[@]}" -o "$PACK" "$RUNTIME_URL"
  fi
  curl -fL --connect-timeout 15 --max-time 60 --retry 3 \
    "${AUTH[@]}" -o "$PACK.sha256" "$RUNTIME_URL.sha256"
  (cd "$GB_HOME" && sha256sum -c "$PACK_NAME.sha256")
  tar -xzf "$PACK" -C "$PACK_DIR"
  rm -f "$PACK" "$PACK.sha256"
  touch "$MARKER"
fi

# ── python + protobuf for box.py ────────────────────────────────────
PY="$(command -v python3 || true)"
if [ -z "$PY" ]; then
  echo "start-box: python3 not found" >&2
  exit 1
fi
if ! "$PY" -c "import google.protobuf" 2>/dev/null; then
  "$PY" -m pip install --user "protobuf==5.29.5" >/dev/null 2>&1 || {
    echo "start-box: protobuf install failed" >&2; exit 1; }
fi

# ── write box.py payload (self-contained; pulled from the plugin/bot
#    source vendored into the runtime pack) ──────────────────────────
BOT_DIR="$PACK_DIR/devbox_bot"
if [ ! -d "$BOT_DIR" ]; then
  echo "start-box: runtime pack lacks devbox_bot/" >&2
  exit 1
fi

# ── devbox_bot.box (inference endpoint) ─────────────────────────────
if ! curl -fsS -o /dev/null "http://127.0.0.1:7812/healthz" \
    2>/dev/null; then
  setsid env \
    GROKBOT_INFERENCE_CREDENTIAL="$GROKBOT_INFERENCE_CREDENTIAL" \
    GROKBOT_GATEWAY_TOKEN="$GROKBOT_GATEWAY_TOKEN" \
    GROKBOT_LLM_BASE_URL="${GROKBOT_LLM_BASE_URL:-}" \
    GROKBOT_LLM_API_KEY="${GROKBOT_LLM_API_KEY:-}" \
    GROKBOT_LLM_MODEL="${GROKBOT_LLM_MODEL:-}" \
    DEVIN_SESSION_ID="${DEVIN_SESSION_ID:-box}" \
    PYTHONPATH="$PACK_DIR" \
    "$PY" -m devbox_bot.box --port 7812 \
    >>"$LOG_DIR/box.log" 2>&1 < /dev/null &
  echo "start-box: devbox_bot.box pid $!"
fi

# ── host-main ───────────────────────────────────────────────────────
HOST_MAIN="$(find "$PACK_DIR" -name 'main.cjs' -o -name 'host-main*' \
  | head -1)"
if [ -z "$HOST_MAIN" ]; then
  echo "start-box: host-main not found in runtime pack" >&2
  exit 1
fi

SAND_DATA_ROOT="${SAND_DATA_ROOT:-$HOME/.sand}"
mkdir -p "$SAND_DATA_ROOT"

# host-main provisions box prompt artifacts under /home/box
if [ ! -d /home/box ]; then
  sudo -n mkdir -p /home/box 2>/dev/null \
    && sudo -n chown "$(id -u):$(id -g)" /home/box 2>/dev/null || true
fi

if ! curl -fsS -o /dev/null "http://127.0.0.1:1340/health" \
    2>/dev/null; then
  setsid env \
    SAND_PACKAGED=1 \
    SAND_HOST_IN_BOX=1 \
    SAND_BOX_AUTO_UPDATE=0 \
    SAND_DATA_ROOT="$SAND_DATA_ROOT" \
    SAND_HOST_PORT=1340 \
    SAND_GATEWAY_BIND_HOST=0.0.0.0 \
    SAND_GATEWAY_REQUIRE_AUTH=1 \
    SAND_GATEWAY_TOKEN="$GROKBOT_GATEWAY_TOKEN" \
    SAND_BACKEND_URL="http://127.0.0.1:7812" \
    CURSOR_API_BASE_URL="http://127.0.0.1:7812" \
    SAND_INFERENCE_RENEWAL_CREDENTIAL="$GROKBOT_INFERENCE_CREDENTIAL" \
    NODE_PATH="$PACK_DIR/node_modules" \
    SAND_TREE_SITTER_NODE_DEPS="$PACK_DIR/node_modules" \
    node "$HOST_MAIN" \
    >>"$LOG_DIR/host-main.log" 2>&1 < /dev/null &
  echo "start-box: host-main pid $!"
fi

echo "start-box: done"
