"""Receive box credentials over the relay and start the Grok Bot runtime."""

from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

GB_HOME = Path(os.environ.get(
    "GROKBOT_HOME", str(Path.home() / ".grok-bot-box"))).expanduser()
CREDS = GB_HOME / "creds.env"
PIDFILE = GB_HOME / "provision.pid"
START_BOX = GB_HOME / "start-box.sh"
LOG_DIR = GB_HOME / "logs"
START_LOG = LOG_DIR / "start-box.log"
IDLE_S = max(1, int(os.environ.get("GROKBOT_PROVISION_IDLE_S", "900")))
PORT = int(os.environ.get("GROKBOT_PROVISION_PORT", "7813"))
MAX_BODY = 64 * 1024
START_BOX_MAX_ATTEMPTS = 3
START_BOX_RETRY_S = 10

ALLOWED_KEYS = frozenset({
    "GROKBOT_GATEWAY_TOKEN",
    "GROKBOT_INFERENCE_CREDENTIAL",
    "GROKBOT_SHARE_CREDENTIAL",
    "GROKBOT_RUNTIME_URL",
    "DEVBOX_WEBAPP_ORIGIN",
    "GROKBOT_LLM_BASE_URL",
    "GROKBOT_LLM_API_KEY",
    "GROKBOT_LLM_MODEL",
    "GROKBOT_USER_JSON",
    "DEVBOX_HARNESS_LLM_TOKEN",
    "DEVBOX_HARNESS_LLM_OPENAI_BASE_URL",
})


def _pid_alive(pid: int | None) -> bool:
    if not pid or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except (OSError, ProcessLookupError):
        return False
    return True


def _pid_from(path: Path) -> int | None:
    try:
        return int(path.read_text().strip())
    except (OSError, ValueError):
        return None


def _write_pid(path: Path, pid: int) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as stream:
        stream.write(f"{pid}\n")
    os.replace(tmp, path)
    os.chmod(path, 0o600)


def _health(port: int, path: str) -> bool:
    try:
        with urllib.request.urlopen(
                f"http://127.0.0.1:{port}{path}", timeout=1) as response:
            return response.status == 200
    except (OSError, urllib.error.URLError, TimeoutError):
        return False


def _services_running() -> bool:
    return _health(7812, "/healthz") and _health(1340, "/health")


def _read_creds() -> dict[str, str]:
    values: dict[str, str] = {}
    try:
        for line in CREDS.read_text().splitlines():
            key, sep, value = line.partition("=")
            if sep and key in ALLOWED_KEYS:
                try:
                    parsed = shlex.split(value)
                except ValueError:
                    continue
                values[key] = parsed[0] if parsed else ""
    except OSError:
        pass
    return values


def _launch_start_box(*, force_restart: bool = False) -> subprocess.Popen | None:
    if not START_BOX.is_file():
        return None
    LOG_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(LOG_DIR, 0o700)
    log_file = START_LOG.open("ab")
    os.chmod(START_LOG, 0o600)
    command = (
        'set -a; . "$1"; set +a; '
        'if [ "$3" = 1 ]; then export GROKBOT_FORCE_RESTART_RUNTIME=1; fi; '
        'exec setsid bash "$2"'
    )
    try:
        return subprocess.Popen(
            ["bash", "-c", command, "grok-bot-provision",
             str(CREDS), str(START_BOX),
             "1" if force_restart else "0"],
            stdin=subprocess.DEVNULL,
            stdout=log_file,
            stderr=subprocess.STDOUT,
            close_fds=True,
        )
    except OSError:
        log_file.close()
        raise


def _replace_creds(values: dict[str, str]) -> None:
    GB_HOME.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(GB_HOME, 0o700)
    fd, temporary = tempfile.mkstemp(
        dir=str(GB_HOME), prefix=".creds-update-")
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w") as stream:
            for key, value in values.items():
                stream.write(f"{key}={shlex.quote(value)}\n")
        os.replace(temporary, CREDS)
        os.chmod(CREDS, 0o600)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


def _resume_or_exit() -> int | None:
    """Avoid starting a second provisioning listener."""
    existing_pid = _pid_from(PIDFILE)
    if _pid_alive(existing_pid) and existing_pid != os.getpid():
        return 0
    return None


def _scrub_line(line: str, secrets: list[str]) -> str:
    for secret in secrets:
        if secret:
            line = line.replace(secret, "[REDACTED]")
    line = re.sub(
        r"(?i)\b((?:GROKBOT|DEVBOX)_[A-Z0-9_]+)\s*([=:])\s*"
        r"(['\"]?)[^\s,'\";]+",
        r"\1\2[REDACTED]",
        line,
    )
    line = re.sub(
        r"(?i)(?:ghp_|github_pat_|cog_|sk-)[A-Za-z0-9_-]{8,}",
        "[REDACTED_CREDENTIAL]",
        line,
    )
    return line


def _tail_logs(secrets: list[str], limit: int = 40) -> list[str]:
    lines: list[str] = []
    for path in (START_LOG, LOG_DIR / "box.log", LOG_DIR / "host-main.log"):
        try:
            content = path.read_text(errors="replace").splitlines()
        except OSError:
            continue
        lines.extend(f"{path.name}: {_scrub_line(line, secrets)}"
                     for line in content[-limit:])
    return lines[-limit:]


def _phase(logs: list[str], values: dict[str, str]) -> str:
    for line in reversed(logs):
        match = re.search(r"start-box: phase=(download|extract|box|host-main)",
                          line)
        if match:
            return match.group(1)
    return "download" if values else "awaiting"


def _listener_status(server: ProvisionServer | None = None) -> dict:
    values = _read_creds()
    secret_values = list(values.values())
    logs = _tail_logs(secret_values)
    services_up = _services_running()
    start_pid = _pid_from(GB_HOME / "start-box.pid")
    state = ("awaiting" if not values else
             "provisioned" if services_up else "provisioning")
    return {
        "state": state,
        "phase": ("start-box-failed"
                  if server is not None and server.start_box_failed
                  else _phase(logs, values)),
        "start_box_exit_code": (
            server.start_box_exit_code if server is not None else None),
        "start_box_attempts": (
            server.start_box_attempts if server is not None else 0),
        "pids": {
            "listener": {
                "pid": os.getpid(), "alive": True,
            },
            "start_box": {
                "pid": start_pid,
                "alive": _pid_alive(start_pid),
            },
            "box": {
                "pid": _pid_from(GB_HOME / "box.pid"),
                "alive": _pid_alive(_pid_from(GB_HOME / "box.pid")),
            },
            "host_main": {
                "pid": _pid_from(GB_HOME / "host-main.pid"),
                "alive": _pid_alive(_pid_from(GB_HOME / "host-main.pid")),
            },
        },
        "logs": logs,
        "disk_free_bytes": shutil.disk_usage(GB_HOME).free,
        "home_box": _path_status(Path("/home/box")),
        "runtime_pack_installed": (
            GB_HOME / "runtime" / ".installed").exists(),
    }


def _path_status(path: Path) -> dict:
    try:
        stat = path.stat()
    except OSError:
        return {"exists": False}
    return {
        "exists": True,
        "mode": f"{stat.st_mode & 0o777:04o}",
        "uid": stat.st_uid,
        "gid": stat.st_gid,
        "writable": os.access(path, os.W_OK),
    }


class ProvisionServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address, handler):
        super().__init__(address, handler)
        self.started_at = time.monotonic()
        self.accepted = CREDS.exists()
        self.lock = threading.Lock()
        self.start_box_lock = threading.RLock()
        self.start_box_process = None
        self.start_box_exit_code: int | None = None
        self.start_box_attempts = 0
        self.start_box_retry_at: float | None = None
        self.start_box_failed = False

    def launch_start_box(self, *, force_restart: bool = False):
        with self.start_box_lock:
            if self.start_box_process is not None:
                exit_code = self.start_box_process.poll()
                if exit_code is None:
                    return None
                proc = self.start_box_process
                self.start_box_process = None
                self.start_box_exit_code = proc.wait()
                pidfile = GB_HOME / "start-box.pid"
                if _pid_from(pidfile) == proc.pid:
                    pidfile.unlink(missing_ok=True)
            if force_restart:
                self.start_box_attempts = 0
                self.start_box_exit_code = None
                self.start_box_retry_at = None
                self.start_box_failed = False
            if self.start_box_attempts >= START_BOX_MAX_ATTEMPTS:
                return self.start_box_process
            proc = _launch_start_box(force_restart=force_restart)
            if proc is not None:
                self.start_box_attempts += 1
                self.start_box_process = proc
                self.start_box_retry_at = None
                _write_pid(GB_HOME / "start-box.pid", proc.pid)
            return proc

    def supervise_start_box(self) -> None:
        with self.start_box_lock:
            now = time.monotonic()
            proc = self.start_box_process
            if proc is not None:
                exit_code = proc.poll()
                if exit_code is None:
                    return
                self.start_box_exit_code = proc.wait()
                pidfile = GB_HOME / "start-box.pid"
                if _pid_from(pidfile) == proc.pid:
                    pidfile.unlink(missing_ok=True)
                self.start_box_process = None
                if self.start_box_exit_code == 0 or _services_running():
                    self.start_box_retry_at = None
                    return
                if self.start_box_attempts >= START_BOX_MAX_ATTEMPTS:
                    self.start_box_failed = True
                    self.start_box_retry_at = None
                else:
                    self.start_box_retry_at = now + START_BOX_RETRY_S
                return

            if self.start_box_retry_at is None:
                return
            if _services_running():
                self.start_box_retry_at = None
                return
            if now >= self.start_box_retry_at:
                self.start_box_retry_at = None
                self.launch_start_box()


class Handler(BaseHTTPRequestHandler):
    server: ProvisionServer

    def log_message(self, _format, *_args):
        return

    def _json(self, status: int, payload: dict) -> None:
        body = json.dumps(payload, separators=(",", ":")).encode()
        self.send_response(status)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path = self.path.split("?", 1)[0]
        self.server.supervise_start_box()
        if path == "/health":
            status = _listener_status(self.server)
            self._json(200, {"state": status["state"]})
        elif path == "/status":
            self._json(200, _listener_status(self.server))
        else:
            self._json(404, {"error": "not_found"})

    def do_POST(self):
        path = self.path.split("?", 1)[0]
        if path not in {"/provision", "/provision/update"}:
            self._json(404, {"error": "not_found"})
            return
        with self.server.lock:
            updating = path == "/provision/update"
            if updating and (not self.server.accepted or not CREDS.exists()):
                self._json(409, {"error": "not_provisioned"})
                return
            if not updating and (self.server.accepted or CREDS.exists()):
                self._json(409, {"error": "already_provisioned"})
                return
            try:
                length = int(self.headers.get("content-length") or 0)
            except ValueError:
                length = MAX_BODY + 1
            if length <= 0 or length > MAX_BODY:
                self._json(400, {"error": "invalid_body"})
                return
            try:
                body = json.loads(self.rfile.read(length))
            except (json.JSONDecodeError, UnicodeDecodeError):
                self._json(400, {"error": "invalid_json"})
                return
            if not isinstance(body, dict):
                self._json(400, {"error": "object_required"})
                return
            unknown = sorted(set(body) - ALLOWED_KEYS)
            if unknown:
                self._json(400, {"error": "unknown_keys", "keys": unknown})
                return
            if not body.get("GROKBOT_GATEWAY_TOKEN"):
                self._json(400, {"error": "gateway_token_required"})
                return
            if any(not isinstance(v, str) or "\n" in v or "\r" in v
                   for v in body.values()):
                self._json(400, {"error": "string_values_required"})
                return
            if updating:
                if not body.get("GROKBOT_SHARE_CREDENTIAL"):
                    self._json(400, {"error": "share_credential_required"})
                    return
                with self.server.start_box_lock:
                    active = self.server.start_box_process
                    if active is not None and active.poll() is None:
                        self._json(409, {"error": "startup_in_progress"})
                        return
                    previous = _read_creds()
                    try:
                        old_user = json.loads(
                            previous.get("GROKBOT_USER_JSON", "{}"))
                        new_user = json.loads(
                            body.get("GROKBOT_USER_JSON", "{}"))
                    except (TypeError, ValueError):
                        self._json(400, {"error": "invalid_user_identity"})
                        return
                    if (not isinstance(old_user, dict)
                            or not isinstance(new_user, dict)
                            or old_user.get("user_id")
                            != new_user.get("user_id")):
                        self._json(403, {"error": "identity_mismatch"})
                        return
                    merged = {**previous, **body}
                    try:
                        _replace_creds(merged)
                        proc = self.server.launch_start_box(
                            force_restart=True)
                    except OSError:
                        _replace_creds(previous)
                        self._json(503, {"error": "start_failed"})
                        return
                    if proc is None:
                        _replace_creds(previous)
                        self._json(409, {"error": "startup_in_progress"})
                        return
                self._json(202, {
                    "state": "updating",
                    "startBoxAttempts": self.server.start_box_attempts,
                })
                return
            GB_HOME.mkdir(mode=0o700, parents=True, exist_ok=True)
            os.chmod(GB_HOME, 0o700)
            fd = os.open(CREDS, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            try:
                with os.fdopen(fd, "w") as stream:
                    for key, value in body.items():
                        stream.write(f"{key}={shlex.quote(value)}\n")
                os.chmod(CREDS, 0o600)
            except Exception:
                CREDS.unlink(missing_ok=True)
                raise
            self.server.accepted = True
            try:
                self.server.launch_start_box()
            except OSError:
                self._json(503, {"error": "start_failed"})
                return
            self._json(202, {"state": "provisioning"})


def main() -> int:
    os.umask(0o077)
    GB_HOME.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(GB_HOME, 0o700)
    LOG_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(LOG_DIR, 0o700)
    resume = _resume_or_exit()
    if resume is not None:
        return resume
    try:
        fd = os.open(PIDFILE, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:
        existing_pid = _pid_from(PIDFILE)
        if _pid_alive(existing_pid):
            return 0
        PIDFILE.unlink(missing_ok=True)
        fd = os.open(PIDFILE, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    with os.fdopen(fd, "w") as stream:
        stream.write(f"{os.getpid()}\n")
    server = ProvisionServer(("0.0.0.0", PORT), Handler)
    server.timeout = 0.5
    if CREDS.exists() and not _services_running():
        server.launch_start_box()
    try:
        while True:
            server.handle_request()
            server.supervise_start_box()
            if (not server.accepted
                    and time.monotonic() - server.started_at >= IDLE_S):
                break
    finally:
        server.server_close()
        if _pid_from(PIDFILE) == os.getpid():
            PIDFILE.unlink(missing_ok=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
