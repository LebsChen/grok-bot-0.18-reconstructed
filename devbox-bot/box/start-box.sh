#!/usr/bin/env bash
# start-box.sh — blueprint startup command for a Grok Bot box session.
#
# No-op unless GROKBOT_GATEWAY_TOKEN is present (session secret set by
# devbox_bot.desktop on EnsureSandBox). Installs the box runtime pack,
# starts devbox_bot.box on 127.0.0.1:7812, ensures the box exec-daemon on
# 127.0.0.1:1337, and starts Grok Bot host-main on 0.0.0.0:1340; children
# are setsid'd so they outlive the startup step.
#
# Env consumed (never echoed):
#   GROKBOT_GATEWAY_TOKEN        session secret → SAND_GATEWAY_TOKEN
#   GROKBOT_INFERENCE_CREDENTIAL session secret → renewal credential
#   GROKBOT_RUNTIME_URL          runtime pack URL (tar.gz + .sha256)
#   GROKBOT_LLM_BASE_URL/_API_KEY/_MODEL   org secrets → inference
set -euo pipefail
RUNTIME_STAGE=""
START_BOX_STAGE=""
cleanup_runtime_stage() {
  if [ -n "$RUNTIME_STAGE" ] && [ -d "$RUNTIME_STAGE" ]; then
    rm -rf -- "$RUNTIME_STAGE"
  fi
  if [ -n "$START_BOX_STAGE" ] && [ -f "$START_BOX_STAGE" ]; then
    rm -f -- "$START_BOX_STAGE"
  fi
}
trap cleanup_runtime_stage EXIT
trap 'echo "start-box: failed rc=$? line=$LINENO" >&2' ERR

# The provisioning listener writes the allow-listed credentials to this
# mode-0600 file before launching the startup script.
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
START_BOX_FILE="$GB_HOME/start-box.sh"
PROVISION_LISTENER_UPDATED=0
PART_HASH_FILE="$GB_HOME/runtime-download.sha256"
mkdir -p "$GB_HOME" "$LOG_DIR"

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
  echo "start-box: phase=download"
  if [ -z "$RUNTIME_URL" ]; then
    echo "start-box: no runtime URL (GROKBOT_RUNTIME_URL unset and" \
         "no plugin-assets origin)" >&2
    exit 1
  fi
  # A one-byte range reveals the full size without depending on HEAD support.
  SIZE="$(curl --http1.1 -fsSL --connect-timeout 15 --max-time 20 \
      --retry 3 --retry-all-errors --retry-delay 2 --max-filesize 1048576 \
      --range 0-0 -D - -o /dev/null "${AUTH[@]}" "$RUNTIME_URL" \
      | awk 'tolower($1) == "content-range:" {split($3, range, "/"); n=range[2]} END{gsub(/\r/,"",n); print n}' || true)"
  if [ -z "$SIZE" ]; then
    SIZE="$(curl --http1.1 -fsSIL --connect-timeout 15 --max-time 20 \
        --retry 3 --retry-all-errors --retry-delay 2 "${AUTH[@]}" \
        "$RUNTIME_URL" | awk 'tolower($0) ~ /^content-length:/ {n=$2} END{gsub(/\r/,"",n); print n}' || true)"
  fi
  case "$SIZE" in
    ''|*[!0-9]*)
      echo "start-box: could not determine runtime size for disk check" >&2
      exit 1
      ;;
  esac
  if [ "$SIZE" -le 1048576 ]; then
    echo "start-box: runtime size is unexpectedly small ($SIZE bytes)" >&2
    exit 1
  fi
  AVAILABLE_BYTES="$(df -PB1 "$GB_HOME" | awk 'NR == 2 {print $4}')"
  case "$AVAILABLE_BYTES" in
    ''|*[!0-9]*)
      echo "start-box: could not determine free disk space" >&2
      exit 1
      ;;
  esac
  REQUIRED_BYTES=$((SIZE * 6 + 67108864))
  echo "start-box: disk check available=$AVAILABLE_BYTES required=$REQUIRED_BYTES"
  if [ "$AVAILABLE_BYTES" -lt "$REQUIRED_BYTES" ]; then
    echo "start-box: insufficient free disk space for runtime pack" >&2
    exit 1
  fi
  curl --http1.1 -fL --connect-timeout 15 --max-time 60 \
    --retry 3 --retry-all-errors --retry-delay 2 \
    --speed-limit 1 --speed-time 20 \
    "${AUTH[@]}" -o "$PACK.sha256" "$RUNTIME_URL.sha256"
  EXPECTED_PACK_SHA="$(awk 'NR == 1 {print $1}' "$PACK.sha256" | tr -d '\r')"
  case "$EXPECTED_PACK_SHA" in
    ''|*[!0-9a-fA-F]*)
      echo "start-box: invalid runtime checksum sidecar" >&2
      exit 1
      ;;
  esac
  if [ "${#EXPECTED_PACK_SHA}" -ne 64 ]; then
    echo "start-box: invalid runtime checksum length" >&2
    exit 1
  fi
  if [ ! -f "$PART_HASH_FILE" ] \
      || [ "$(cat "$PART_HASH_FILE")" != "$EXPECTED_PACK_SHA" ]; then
    rm -f "$GB_HOME"/.part-0 "$GB_HOME"/.part-1 \
      "$GB_HOME"/.part-2 "$GB_HOME"/.part-3 \
      "$GB_HOME"/.part-4 "$GB_HOME"/.part-5 \
      "$GB_HOME"/.part-6 "$GB_HOME"/.part-7
    printf '%s\n' "$EXPECTED_PACK_SHA" > "$PART_HASH_FILE.tmp.$BASHPID"
    chmod 600 "$PART_HASH_FILE.tmp.$BASHPID"
    mv -f -- "$PART_HASH_FILE.tmp.$BASHPID" "$PART_HASH_FILE"
  fi
  if [ -n "$SIZE" ] && [ "$SIZE" -gt 1048576 ] 2>/dev/null; then
    N=8; PART=$(( (SIZE + N - 1) / N )); pids=()
    for i in $(seq 0 $((N - 1))); do
      s=$((i * PART)); e=$((s + PART - 1))
      [ "$e" -ge "$SIZE" ] && e=$((SIZE - 1))
      ( for try in 1 2 3 4 5; do
          have=0; [ -f "$GB_HOME/.part-$i" ] && have=$(stat -c%s "$GB_HOME/.part-$i")
          want=$((e - s + 1))
          [ "$have" -eq "$want" ] && break
          if [ "$have" -gt "$want" ]; then
            rm -f "$GB_HOME/.part-$i"
            have=0
          fi
          if curl --http1.1 -fL --connect-timeout 15 --speed-limit 5120 --speed-time 120 \
            -r $((s + have))-$e "${AUTH[@]}" \
            -o - "$RUNTIME_URL" >> "$GB_HOME/.part-$i"; then
            have=$(stat -c%s "$GB_HOME/.part-$i")
            [ "$have" -eq "$want" ] && break
          fi
          sleep 2
        done
        have=0; [ -f "$GB_HOME/.part-$i" ] && have=$(stat -c%s "$GB_HOME/.part-$i")
        [ "$have" -eq "$want" ] || {
          echo "start-box: incomplete runtime chunk $i ($have/$want)" >&2
          exit 1
        } ) &
      pids+=($!)
    done
    download_failed=0
    for pid in "${pids[@]}"; do
      wait "$pid" || download_failed=1
    done
    if [ "$download_failed" -ne 0 ]; then
      echo "start-box: runtime chunk download failed" >&2
      exit 1
    fi
    cat "$GB_HOME"/.part-0 "$GB_HOME"/.part-1 \
      "$GB_HOME"/.part-2 "$GB_HOME"/.part-3 \
      "$GB_HOME"/.part-4 "$GB_HOME"/.part-5 \
      "$GB_HOME"/.part-6 "$GB_HOME"/.part-7 > "$PACK"
  else
    curl --http1.1 -fL --connect-timeout 15 \
      --speed-limit 5120 --speed-time 30 \
      --retry 5 --retry-all-errors --retry-delay 2 \
      -C - "${AUTH[@]}" -o "$PACK" "$RUNTIME_URL"
  fi
  (cd "$GB_HOME" && sha256sum -c "$PACK_NAME.sha256")
  tar -tzf "$PACK" >/dev/null
  rm -f "$GB_HOME"/.part-0 "$GB_HOME"/.part-1 \
    "$GB_HOME"/.part-2 "$GB_HOME"/.part-3 \
    "$GB_HOME"/.part-4 "$GB_HOME"/.part-5 \
    "$GB_HOME"/.part-6 "$GB_HOME"/.part-7 "$PART_HASH_FILE"
  echo "start-box: phase=extract"
  RUNTIME_STAGE="$GB_HOME/.runtime-stage.$BASHPID"
  mkdir "$RUNTIME_STAGE"
  tar -xzf "$PACK" -C "$RUNTIME_STAGE"
  if [ ! -d "$RUNTIME_STAGE/devbox_bot" ]; then
    echo "start-box: runtime pack lacks devbox_bot/" >&2
    exit 1
  fi
  if [ ! -x "$RUNTIME_STAGE/node" ]; then
    echo "start-box: packaged node runtime not found in staged pack" >&2
    exit 1
  fi
  if [ -z "$(find "$RUNTIME_STAGE" -type f -name 'host-main.cjs' -print -quit)" ]; then
    echo "start-box: host-main not found in staged pack" >&2
    exit 1
  fi
  if [ ! -f "$RUNTIME_STAGE/opt-sand/box-exec-daemon/main.cjs" ]; then
    echo "start-box: box exec-daemon not found in staged pack" >&2
    exit 1
  fi
  BOX_EXEC_DAEMON_SHIM_STAGE="$RUNTIME_STAGE/opt-sand/exec-daemon/exec-daemon"
  if [ ! -x "$BOX_EXEC_DAEMON_SHIM_STAGE" ]; then
    echo "start-box: per-window exec-daemon shim not found in staged pack" >&2
    exit 1
  fi
  if ! bash -n "$BOX_EXEC_DAEMON_SHIM_STAGE"; then
    echo "start-box: per-window exec-daemon shim is invalid" >&2
    exit 1
  fi
  BOX_SCRIPTS_STAGE="$RUNTIME_STAGE/opt-sand/sand-host/box-scripts"
  for required_script in start-window stop-window box-x11vnc \
      sand-window-router.mjs \
      box-bounded-log start-desktop.sh box-chrome-policy sand-wallpaper \
      sand-wallpaper-tone.mjs; do
    if [ ! -f "$BOX_SCRIPTS_STAGE/$required_script" ]; then
      echo "start-box: required box script missing: $required_script" >&2
      exit 1
    fi
  done
  PACKAGED_START_BOX_STAGE="$RUNTIME_STAGE/opt-sand/grok-bot-box/start-box.sh"
  if [ ! -f "$PACKAGED_START_BOX_STAGE" ]; then
    echo "start-box: packaged startup script not found" >&2
    exit 1
  fi
  if ! bash -n "$PACKAGED_START_BOX_STAGE"; then
    echo "start-box: packaged startup script is invalid" >&2
    exit 1
  fi
  if [ ! -f "$RUNTIME_STAGE/opt-sand/grok-bot-box/provision.py" ]; then
    echo "start-box: packaged provisioning listener not found" >&2
    exit 1
  fi
  PACK_SHA="$(sha256sum "$PACK" | awk '{print $1}')"
  printf '%s\n' "$PACK_SHA" > "$RUNTIME_STAGE/.installed"
  chmod 600 "$RUNTIME_STAGE/.installed"
  OLD_RUNTIME_DIR="$GB_HOME/.runtime-previous.$BASHPID"
  if [ -e "$PACK_DIR" ] || [ -L "$PACK_DIR" ]; then
    mv "$PACK_DIR" "$OLD_RUNTIME_DIR"
  fi
  if ! mv "$RUNTIME_STAGE" "$PACK_DIR"; then
    if { [ -e "$OLD_RUNTIME_DIR" ] || [ -L "$OLD_RUNTIME_DIR" ]; } \
        && [ ! -e "$PACK_DIR" ]; then
      mv "$OLD_RUNTIME_DIR" "$PACK_DIR" \
        || echo "start-box: failed to restore previous runtime" >&2
    fi
    echo "start-box: failed to activate staged runtime pack" >&2
    exit 1
  fi
  RUNTIME_STAGE=""
  if [ -e "$OLD_RUNTIME_DIR" ] || [ -L "$OLD_RUNTIME_DIR" ]; then
    rm -rf -- "$OLD_RUNTIME_DIR" \
      || echo "start-box: could not remove previous runtime backup" >&2
  fi
  rm -f "$PACK" "$PACK.sha256"
  echo "start-box: runtime pack installed sha256=$PACK_SHA"
fi

PACKAGED_START_BOX="$PACK_DIR/opt-sand/grok-bot-box/start-box.sh"
if [ -f "$PACKAGED_START_BOX" ] \
    && ! cmp -s "$PACKAGED_START_BOX" "$START_BOX_FILE"; then
  START_BOX_STAGE="$GB_HOME/.start-box-stage.$BASHPID"
  cp -- "$PACKAGED_START_BOX" "$START_BOX_STAGE"
  chmod 700 "$START_BOX_STAGE"
  if ! bash -n "$START_BOX_STAGE"; then
    echo "start-box: packaged startup script is invalid" >&2
    exit 1
  fi
  if mv -f -- "$START_BOX_STAGE" "$START_BOX_FILE"; then
    START_BOX_STAGE=""
    echo "start-box: startup script updated"
  else
    echo "start-box: could not install packaged startup script" >&2
  fi
fi

PACKAGED_PROVISION="$PACK_DIR/opt-sand/grok-bot-box/provision.py"
PROVISION_SCRIPT="$GB_HOME/provision.py"
if ! cmp -s "$PACKAGED_PROVISION" "$PROVISION_SCRIPT"; then
  PROVISION_STAGE="$GB_HOME/.provision.py.stage.$BASHPID"
  cp -- "$PACKAGED_PROVISION" "$PROVISION_STAGE"
  chmod 700 "$PROVISION_STAGE"
  mv -f -- "$PROVISION_STAGE" "$PROVISION_SCRIPT"
  PROVISION_LISTENER_UPDATED=1
fi

EXEC_DAEMON_SHIM="$PACK_DIR/opt-sand/exec-daemon/exec-daemon"
if [ ! -x "$EXEC_DAEMON_SHIM" ]; then
  echo "start-box: per-window exec-daemon shim is missing" >&2
  exit 1
fi
EXEC_DAEMON_DIRECTORY="/exec-daemon"
if [ ! -d "$EXEC_DAEMON_DIRECTORY" ]; then
  if [ -e "$EXEC_DAEMON_DIRECTORY" ] || [ -L "$EXEC_DAEMON_DIRECTORY" ]; then
    echo "start-box: /exec-daemon exists and is not a directory" >&2
    exit 1
  fi
  if [ -w / ]; then
    mkdir -m 0755 -- "$EXEC_DAEMON_DIRECTORY"
  else
    sudo -n install -d -m 0755 -- "$EXEC_DAEMON_DIRECTORY" || {
      echo "start-box: could not create /exec-daemon" >&2
      exit 1
    }
  fi
fi
# Repair a previously-created /exec-daemon that is not traversable by
# this user (a restrictive umask during an earlier creation leaves it
# root-owned mode 0700, so per-window start-window fails with
# "Permission denied" launching /exec-daemon/exec-daemon).
if [ -d "$EXEC_DAEMON_DIRECTORY" ] && [ ! -x "$EXEC_DAEMON_DIRECTORY" ]; then
  chmod 0755 -- "$EXEC_DAEMON_DIRECTORY" 2>/dev/null \
    || sudo -n chmod 0755 -- "$EXEC_DAEMON_DIRECTORY" || {
      echo "start-box: /exec-daemon is not accessible" >&2
      exit 1
    }
fi
if [ -e "$EXEC_DAEMON_DIRECTORY/exec-daemon" ] \
    && [ ! -L "$EXEC_DAEMON_DIRECTORY/exec-daemon" ]; then
  echo "start-box: preserving real /exec-daemon/exec-daemon"
else
  if [ -w "$EXEC_DAEMON_DIRECTORY" ]; then
    ln -sfn -- "$EXEC_DAEMON_SHIM" \
      "$EXEC_DAEMON_DIRECTORY/exec-daemon"
  else
    sudo -n ln -sfn -- "$EXEC_DAEMON_SHIM" \
      "$EXEC_DAEMON_DIRECTORY/exec-daemon" || {
        echo "start-box: could not link /exec-daemon/exec-daemon" >&2
        exit 1
      }
  fi
fi
RG_BINARY="$(command -v rg || true)"
if [ -z "$RG_BINARY" ] \
    && [ -x "$PACK_DIR/node_modules/@vscode/ripgrep/bin/rg" ]; then
  RG_BINARY="$PACK_DIR/node_modules/@vscode/ripgrep/bin/rg"
fi
if [ -n "$RG_BINARY" ]; then
  if [ -e "$EXEC_DAEMON_DIRECTORY/rg" ] \
      && [ ! -L "$EXEC_DAEMON_DIRECTORY/rg" ]; then
    echo "start-box: preserving real /exec-daemon/rg"
  elif [ -w "$EXEC_DAEMON_DIRECTORY" ]; then
    ln -sfn -- "$RG_BINARY" "$EXEC_DAEMON_DIRECTORY/rg"
  else
    sudo -n ln -sfn -- "$RG_BINARY" "$EXEC_DAEMON_DIRECTORY/rg" || {
      echo "start-box: could not link /exec-daemon/rg" >&2
      exit 1
    }
  fi
else
  echo "start-box: no ripgrep executable available; /exec-daemon/rg omitted" >&2
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
BOX_SCRIPTS_DIR="$PACK_DIR/opt-sand/sand-host/box-scripts"
if [ ! -d "$BOX_SCRIPTS_DIR" ]; then
  echo "start-box: runtime pack lacks box-scripts/" >&2
  exit 1
fi
WINDOW_ROUTER_SCRIPT="$BOX_SCRIPTS_DIR/sand-window-router.mjs"
for box_script in "$BOX_SCRIPTS_DIR"/*; do
  [ -f "$box_script" ] || continue
  script_name="${box_script##*/}"
  script_destination="/usr/local/bin/$script_name"
  if [ -w /usr/local/bin ]; then
    ln -sfn -- "$box_script" "$script_destination"
  else
    sudo -n ln -sfn -- "$box_script" "$script_destination" || {
      echo "start-box: could not install /usr/local/bin/$script_name" >&2
      exit 1
    }
  fi
done
if [ ! -x /usr/local/bin/box-xvfb ] && [ -x /usr/bin/Xvfb ]; then
  if [ -w /usr/local/bin ]; then
    ln -sfn -- /usr/bin/Xvfb /usr/local/bin/box-xvfb
  else
    sudo -n ln -sfn -- /usr/bin/Xvfb /usr/local/bin/box-xvfb || {
      echo "start-box: could not install /usr/local/bin/box-xvfb" >&2
      exit 1
    }
  fi
fi

# ── host-main ───────────────────────────────────────────────────────
HOST_MAIN="$(find "$PACK_DIR" -type f -name 'host-main.cjs' -print -quit)"
if [ -z "$HOST_MAIN" ]; then
  echo "start-box: host-main not found in runtime pack" >&2
  exit 1
fi
NODE_BIN="$PACK_DIR/node"
if [ ! -x "$NODE_BIN" ]; then
  echo "start-box: packaged node runtime not found" >&2
  exit 1
fi

SAND_DATA_ROOT="${SAND_DATA_ROOT:-$HOME/.sand}"
mkdir -p "$SAND_DATA_ROOT"
BOX_EXEC_DAEMON="$PACK_DIR/opt-sand/box-exec-daemon/main.cjs"
if [ ! -f "$BOX_EXEC_DAEMON" ]; then
  echo "start-box: box exec-daemon not found in runtime pack" >&2
  exit 1
fi
BOX_WORKSPACE_ROOT="$SAND_DATA_ROOT/box-workspace"
mkdir -p "$BOX_WORKSPACE_ROOT" "$SAND_DATA_ROOT/box-terminals"
if [ ! -e /workspace ]; then
  if [ -w / ]; then
    ln -sfn "$BOX_WORKSPACE_ROOT" /workspace
  else
    sudo -n ln -sfn "$BOX_WORKSPACE_ROOT" /workspace || {
      echo "start-box: could not link /workspace to the box workspace" >&2
      exit 1
    }
  fi
fi

box_exec_daemon_listening() {
  curl --connect-timeout 1 --max-time 2 -sS -o /dev/null \
    "http://127.0.0.1:1337/" 2>/dev/null
}

box_backend_healthy() {
  curl -fsS -o /dev/null "http://127.0.0.1:7812/healthz" 2>/dev/null
}

host_main_healthy() {
  curl -fsS -o /dev/null "http://127.0.0.1:1340/health" 2>/dev/null
}

box_window_router_listening() {
  curl --connect-timeout 1 --max-time 2 -sS -o /dev/null \
    "http://127.0.0.1:1339/" 2>/dev/null
}

provision_listener_healthy() {
  curl --connect-timeout 1 --max-time 2 -fsS -o /dev/null \
    "http://127.0.0.1:7813/status" 2>/dev/null
}

stop_owned_runtime_process() {
  local pid_file="$1" expected="$2" label="$3" pid command_line
  if [ ! -f "$pid_file" ]; then
    if { [ "$label" = "box" ] && box_backend_healthy; } \
        || { [ "$label" = "exec-daemon" ] && box_exec_daemon_listening; } \
        || { [ "$label" = "host-main" ] && host_main_healthy; } \
        || { [ "$label" = "window-router" ] \
          && box_window_router_listening; }; then
      echo "start-box: cannot safely restart $label without its pid file" >&2
      return 1
    fi
    return 0
  fi
  pid="$(cat "$pid_file")"
  case "$pid" in
    ''|*[!0-9]*)
      echo "start-box: invalid $label pid file" >&2
      return 1
      ;;
  esac
  if ! kill -0 "$pid" 2>/dev/null; then
    rm -f "$pid_file"
    return 0
  fi
  command_line="$(tr '\0' ' ' < "/proc/$pid/cmdline" 2>/dev/null || true)"
  if [[ "$command_line" != *"$expected"* ]]; then
    echo "start-box: refusing to stop unrecognized $label process" >&2
    return 1
  fi
  kill -TERM "$pid" 2>/dev/null || {
    echo "start-box: could not stop $label process" >&2
    return 1
  }
  for _ in $(seq 1 50); do
    if ! kill -0 "$pid" 2>/dev/null; then
      rm -f "$pid_file"
      return 0
    fi
    sleep 0.1
  done
  echo "start-box: $label process did not stop cleanly" >&2
  return 1
}

if [ "${GROKBOT_FORCE_RESTART_RUNTIME:-0}" = "1" ]; then
  echo "start-box: phase=credential-update"
  stop_owned_runtime_process "$GB_HOME/host-main.pid" "$HOST_MAIN" \
    "host-main" || exit 1
  stop_owned_runtime_process "$GB_HOME/window-router.pid" \
    "$WINDOW_ROUTER_SCRIPT" "window-router" || exit 1
  stop_owned_runtime_process "$GB_HOME/box-exec-daemon.pid" \
    "$BOX_EXEC_DAEMON" "exec-daemon" || exit 1
  stop_owned_runtime_process "$GB_HOME/box.pid" \
    "devbox_bot.box" "box" || exit 1
fi

# ── devbox_bot.box (inference endpoint) ─────────────────────────────
echo "start-box: phase=box"
if ! box_backend_healthy; then
  (  # secrets exported, never on the env argv (visible via /proc)
    export GROKBOT_INFERENCE_CREDENTIAL="$GROKBOT_INFERENCE_CREDENTIAL"
    export GROKBOT_GATEWAY_TOKEN="$GROKBOT_GATEWAY_TOKEN"
    export GROKBOT_LLM_BASE_URL="${GROKBOT_LLM_BASE_URL:-}"
    export GROKBOT_LLM_API_KEY="${GROKBOT_LLM_API_KEY:-}"
    export GROKBOT_LLM_MODEL="${GROKBOT_LLM_MODEL:-}"
    export GROKBOT_USER_JSON="${GROKBOT_USER_JSON:-}"
    export GROKBOT_SHARE_CREDENTIAL="${GROKBOT_SHARE_CREDENTIAL:-}"
    export DEVBOX_WEBAPP_ORIGIN="${DEVBOX_WEBAPP_ORIGIN:-https://app.devinai.net}"
    export DEVIN_SESSION_ID="${DEVIN_SESSION_ID:-box}"
    export PYTHONPATH="$PACK_DIR"
    exec 9>&-
    exec setsid "$PY" -m devbox_bot.box --port 7812 \
      >>"$LOG_DIR/box.log" 2>&1 < /dev/null
  ) &
  printf '%s\n' "$!" > "$GB_HOME/box.pid"
  chmod 600 "$GB_HOME/box.pid"
  echo "start-box: devbox_bot.box pid $!"
fi

if ! box_exec_daemon_listening; then
  echo "start-box: phase=exec-daemon"
  (
    export SAND_BOX_EXEC_DAEMON_PORT=1337
    export SAND_BOX_WORKSPACE_ROOT="$BOX_WORKSPACE_ROOT"
    export SAND_BOX_TERMINALS_DIRECTORY="$SAND_DATA_ROOT/box-terminals"
    exec 9>&-
    exec setsid "$NODE_BIN" --disable-warning=ExperimentalWarning \
      "$BOX_EXEC_DAEMON" >>"$LOG_DIR/host-main.log" 2>&1 < /dev/null
  ) &
  DAEMON_PID=$!
  printf '%s\n' "$DAEMON_PID" > "$GB_HOME/box-exec-daemon.pid"
  chmod 600 "$GB_HOME/box-exec-daemon.pid"
  DAEMON_READY=0
  for _ in $(seq 1 200); do
    if box_exec_daemon_listening; then
      DAEMON_READY=1
      break
    fi
    if ! kill -0 "$DAEMON_PID" 2>/dev/null; then
      break
    fi
    sleep 0.1
  done
  if [ "$DAEMON_READY" -ne 1 ]; then
    echo "start-box: exec-daemon failed to listen on 127.0.0.1:1337" >&2
    exit 1
  fi
  echo "start-box: box-exec-daemon pid $DAEMON_PID"
fi

if [ ! -f "$WINDOW_ROUTER_SCRIPT" ]; then
  echo "start-box: sand-window-router is missing from the runtime pack" >&2
  exit 1
fi
if ! box_window_router_listening; then
  echo "start-box: phase=window-router"
  (
    exec 9>&-
    exec setsid "$NODE_BIN" "$WINDOW_ROUTER_SCRIPT" 1339 1337 14000 \
      >>"$LOG_DIR/window-router.log" 2>&1 < /dev/null
  ) &
  WINDOW_ROUTER_PID=$!
  printf '%s\n' "$WINDOW_ROUTER_PID" > "$GB_HOME/window-router.pid"
  chmod 600 "$GB_HOME/window-router.pid"
  WINDOW_ROUTER_READY=0
  for _ in $(seq 1 200); do
    if box_window_router_listening; then
      WINDOW_ROUTER_READY=1
      break
    fi
    if ! kill -0 "$WINDOW_ROUTER_PID" 2>/dev/null; then
      break
    fi
    sleep 0.1
  done
  if [ "$WINDOW_ROUTER_READY" -ne 1 ]; then
    echo "start-box: sand-window-router failed to listen on 127.0.0.1:1339" >&2
    exit 1
  fi
  echo "start-box: sand-window-router pid $WINDOW_ROUTER_PID"
fi

# host-main provisions box prompt artifacts under /home/box
if [ ! -d /home/box ]; then
  sudo -n install -d -m 0755 /home/box 2>/dev/null \
    && sudo -n chown "$(id -u):$(id -g)" /home/box 2>/dev/null || true
fi

if ! curl -fsS -o /dev/null "http://127.0.0.1:1340/health" \
    2>/dev/null; then
  echo "start-box: phase=host-main"
  (  # secrets exported, never on the env argv (visible via /proc)
    export SAND_PACKAGED=1 SAND_HOST_IN_BOX=1 SAND_BOX_AUTO_UPDATE=0
    export SAND_DATA_ROOT="$SAND_DATA_ROOT"
    export SAND_HOST_PORT=1340
    export SAND_GATEWAY_BIND_HOST=0.0.0.0
    export SAND_GATEWAY_REQUIRE_AUTH=1
    export SAND_GATEWAY_TOKEN="$GROKBOT_GATEWAY_TOKEN"
    export SAND_USE_EXISTING_BOX_EXEC_DAEMON=1
    export SAND_BACKEND_URL="http://127.0.0.1:7812"
    export CURSOR_API_BASE_URL="http://127.0.0.1:7812"
    export SAND_INFERENCE_RENEWAL_CREDENTIAL="$GROKBOT_INFERENCE_CREDENTIAL"
    export NODE_PATH="$PACK_DIR/node_modules"
    export SAND_TREE_SITTER_NODE_DEPS="$PACK_DIR/node_modules"
    exec 9>&-
    exec setsid "$NODE_BIN" --disable-warning=ExperimentalWarning "$HOST_MAIN" \
      >>"$LOG_DIR/host-main.log" 2>&1 < /dev/null
  ) &
  printf '%s\n' "$!" > "$GB_HOME/host-main.pid"
  chmod 600 "$GB_HOME/host-main.pid"
  echo "start-box: host-main pid $!"
fi

if [ "$PROVISION_LISTENER_UPDATED" -eq 1 ] \
    || ! provision_listener_healthy; then
  PROVISION_PID_FILE="$GB_HOME/provision.pid"
  PROVISION_PID=""
  if [ -f "$PROVISION_PID_FILE" ]; then
    PROVISION_PID="$(cat "$PROVISION_PID_FILE")"
  fi
  case "$PROVISION_PID" in
    ''|*[!0-9]*)
      PROVISION_PID=""
      ;;
  esac
  if [ -n "$PROVISION_PID" ] && kill -0 "$PROVISION_PID" 2>/dev/null; then
    PROVISION_COMMAND="$(tr '\0' ' ' \
      < "/proc/$PROVISION_PID/cmdline" 2>/dev/null || true)"
    if [[ "$PROVISION_COMMAND" != *"$PROVISION_SCRIPT"* ]]; then
      echo "start-box: refusing to replace an unrecognized provision listener" >&2
      exit 1
    fi
    kill -TERM "$PROVISION_PID" || {
      echo "start-box: could not stop the old provision listener" >&2
      exit 1
    }
    for _ in $(seq 1 50); do
      if ! kill -0 "$PROVISION_PID" 2>/dev/null; then
        break
      fi
      sleep 0.1
    done
    if kill -0 "$PROVISION_PID" 2>/dev/null; then
      echo "start-box: old provision listener did not stop cleanly" >&2
      exit 1
    fi
  fi
  (
    exec 9>&-
    export GROKBOT_PROVISION_IDLE_S="${GROKBOT_PROVISION_IDLE_S:-900}"
    exec nohup setsid python3 "$PROVISION_SCRIPT"
  ) >>"$LOG_DIR/provision.log" 2>&1 < /dev/null &
  PROVISION_READY=0
  for _ in $(seq 1 100); do
    if provision_listener_healthy; then
      PROVISION_READY=1
      break
    fi
    sleep 0.2
  done
  if [ "$PROVISION_READY" -ne 1 ]; then
    echo "start-box: provisioning listener failed to listen on 127.0.0.1:7813" >&2
    exit 1
  fi
  echo "start-box: refreshed provisioning listener"
fi

echo "start-box: done"
