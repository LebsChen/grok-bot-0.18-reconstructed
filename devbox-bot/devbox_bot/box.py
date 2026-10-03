"""devbox_bot.box — guest-side inference endpoint inside the box VM.

Started by ``box/start-box.sh`` on 127.0.0.1:7812. The in-box host-main
is pointed at this process via ``SAND_BACKEND_URL`` /
``CURSOR_API_BASE_URL`` and calls:

* ``POST /sand-box/inference-credential`` and
  ``/sand-box/local-exec-daemon-credential`` — validated against
  ``$GROKBOT_INFERENCE_CREDENTIAL``; returns a short-lived HS256 access
  token bound to this box.
* Connect ``aiserver.v1.InferenceService/Stream`` — translated to the
  configured OpenAI-compatible endpoint (``GROKBOT_LLM_BASE_URL`` /
  ``GROKBOT_LLM_API_KEY`` / ``GROKBOT_LLM_MODEL`` org secrets).
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import hmac
import json
import logging
import os
import re
import secrets
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from .codec import ConnectError, to_json_dict
from .connect import ConnectRouter, serve
from .handlers import models_from_env, register
from .inference import stream_inference

log = logging.getLogger("devbox_bot.box")

CREDENTIAL_TTL_S = 3600
STATE_DIR_NAME = "devbox-bot"
AUTOMATION_INTERVAL_MS = 60_000
AUTOMATION_BACKFILL_MS = 120_000
SAND_SHADOW_MARKER_PREFIX = "sand-shadow:"
SHARE_RELAY_PATHS = {
    "/sand/xuser/poll",
    "/sand/share-rooms/from-agent",
    "/sand/share-rooms",
    "/sand/share-rooms/invite-links",
    "/sand/share-rooms/join",
    "/sand/share-rooms/join/respond",
    "/sand/share-rooms/agents/add",
    "/sand/share-rooms/picture",
    "/sand/share-rooms/agents/remove",
    "/sand/share-rooms/agents/remove-deleted",
    "/sand/share-rooms/leave",
    "/sand/share-state",
    "/sand/xuser/send",
}
MAX_RELAY_BODY = 2 * 1024 * 1024


def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _parse_cron_field(raw: str, minimum: int, maximum: int,
                      *, sunday_alias: bool = False
                      ) -> tuple[frozenset[int], bool]:
    values: set[int] = set()
    wildcard = raw == "*" or raw.startswith("*/")
    for item in raw.split(","):
        if not item:
            raise ValueError("empty cron list item")
        if "/" in item:
            base, step_text = item.split("/", 1)
            try:
                step = int(step_text)
            except ValueError as exc:
                raise ValueError("invalid cron step") from exc
            if step < 1:
                raise ValueError("cron step must be positive")
        else:
            base, step = item, 1
        if base == "*":
            start, end = minimum, maximum
        elif "-" in base:
            parts = base.split("-", 1)
            try:
                start, end = (int(part) for part in parts)
            except ValueError as exc:
                raise ValueError("invalid cron range") from exc
        else:
            try:
                start = int(base)
            except ValueError as exc:
                raise ValueError("invalid cron value") from exc
            end = maximum if "/" in item else start
        if start < minimum or end > maximum or start > end:
            raise ValueError("cron value outside field range")
        for value in range(start, end + 1, step):
            values.add(0 if sunday_alias and value == 7 else value)
    if not values:
        raise ValueError("cron field has no values")
    return frozenset(values), wildcard


class CronSchedule:
    def __init__(self, fields: tuple[frozenset[int], ...],
                 wildcards: tuple[bool, ...], zone: ZoneInfo) -> None:
        (self.minutes, self.hours, self.days, self.months,
         self.weekdays) = fields
        (self.days_any, self.weekdays_any) = wildcards
        self.zone = zone

    def matches(self, scheduled_for_ms: int) -> bool:
        utc = datetime.fromtimestamp(
            scheduled_for_ms / 1000, tz=timezone.utc)
        local = utc.astimezone(self.zone)
        if (local.minute not in self.minutes
                or local.hour not in self.hours
                or local.month not in self.months):
            return False
        cron_weekday = (local.weekday() + 1) % 7
        day_matches = local.day in self.days
        weekday_matches = cron_weekday in self.weekdays
        if self.days_any:
            return weekday_matches
        if self.weekdays_any:
            return day_matches
        return day_matches or weekday_matches


def parse_cron_schedule(expression: str) -> CronSchedule:
    parts = expression.strip().split()
    zone_name = "UTC"
    while parts and (parts[0].startswith("CRON_TZ=")
                     or parts[0].startswith("TZ=")):
        _, zone_name = parts.pop(0).split("=", 1)
        if not zone_name:
            raise ValueError("cron timezone is empty")
    if len(parts) != 5:
        raise ValueError("expected a five-field cron expression")
    fields = (
        _parse_cron_field(parts[0], 0, 59),
        _parse_cron_field(parts[1], 0, 23),
        _parse_cron_field(parts[2], 1, 31),
        _parse_cron_field(parts[3], 1, 12),
        _parse_cron_field(parts[4], 0, 7, sunday_alias=True),
    )
    zone = ZoneInfo(zone_name)
    return CronSchedule(
        tuple(field[0] for field in fields),
        (fields[2][1], fields[4][1]),
        zone,
    )


def cron_matches(expression: str, when: datetime | float) -> bool:
    if isinstance(when, datetime):
        if when.tzinfo is None:
            when = when.replace(tzinfo=timezone.utc)
        timestamp_ms = int(when.timestamp() // 60 * AUTOMATION_INTERVAL_MS)
    else:
        timestamp_ms = int(
            when // AUTOMATION_INTERVAL_MS * AUTOMATION_INTERVAL_MS)
    return parse_cron_schedule(expression).matches(timestamp_ms)


def _cron_expressions(workflow: dict) -> list[str]:
    expressions = []
    for trigger in workflow.get("triggers", []):
        if not isinstance(trigger, dict):
            continue
        oneof = trigger.get("trigger")
        candidate = oneof if isinstance(oneof, dict) else trigger
        cron = candidate.get("cron")
        if isinstance(cron, dict):
            cron = cron.get("cron")
        if isinstance(cron, str) and cron:
            expressions.append(cron)
    return expressions


def _default_automation_state_path() -> Path:
    base = (os.environ.get("XDG_STATE_HOME")
            or os.environ.get("LOCALAPPDATA")
            or str(Path.home() / ".local" / "state"))
    directory = Path(base) / STATE_DIR_NAME
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    return directory / "automations.json"


class AutomationStore:
    def __init__(self, path: Path | None = None) -> None:
        self.path = path or _default_automation_state_path()
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._data = {
            "automations": {},
            "events": {},
            "slots": {},
        }
        if self.path.exists():
            try:
                loaded = json.loads(self.path.read_text(encoding="utf-8"))
                for key in self._data:
                    if isinstance(loaded.get(key), dict):
                        self._data[key] = loaded[key]
            except (OSError, ValueError) as exc:
                log.warning("could not load automation state: %s", exc)
            try:
                os.chmod(self.path, 0o600)
            except OSError:
                pass

    def _save_locked(self) -> None:
        payload = json.dumps(
            self._data, sort_keys=True, separators=(",", ":")).encode()
        fd, temporary = tempfile.mkstemp(
            dir=str(self.path.parent), prefix=".automations-")
        try:
            with os.fdopen(fd, "wb") as stream:
                try:
                    os.fchmod(stream.fileno(), 0o600)
                except (AttributeError, OSError):
                    pass
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.path)
            os.chmod(self.path, 0o600)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)

    def list(self, sand_agent_id: str | None = None) -> list[dict]:
        with self._lock:
            entries = self._data["automations"].values()
            return [
                json.loads(json.dumps(entry))
                for entry in sorted(
                    entries, key=lambda item: item["automationId"])
                if sand_agent_id is None
                or entry.get("sandAgentId") == sand_agent_id
            ]

    def get(self, automation_id: str) -> dict | None:
        with self._lock:
            entry = self._data["automations"].get(automation_id)
            return json.loads(json.dumps(entry)) if entry else None

    def put(self, entry: dict) -> dict:
        with self._lock:
            self._data["automations"][entry["automationId"]] = entry
            self._save_locked()
            return json.loads(json.dumps(entry))

    def update(self, automation_id: str, values: dict) -> dict | None:
        with self._lock:
            entry = self._data["automations"].get(automation_id)
            if entry is None:
                return None
            entry.update(values)
            self._save_locked()
            return json.loads(json.dumps(entry))

    def delete(self, automation_id: str) -> bool:
        with self._lock:
            if self._data["automations"].pop(automation_id, None) is None:
                return False
            self._data["events"] = {
                run_id: event
                for run_id, event in self._data["events"].items()
                if event.get("automationId") != automation_id
            }
            self._save_locked()
            return True

    def _schedules(self, entry: dict) -> list[CronSchedule]:
        schedules = []
        for expression in _cron_expressions(entry.get("workflow", {})):
            try:
                schedules.append(parse_cron_schedule(expression))
            except (ValueError, TypeError, KeyError):
                log.warning("ignoring invalid automation cron expression")
        return schedules

    def materialize_due(self, now_ms: int,
                        *, startup: bool = False) -> list[dict]:
        latest_slot = now_ms // AUTOMATION_INTERVAL_MS * AUTOMATION_INTERVAL_MS
        slots = [latest_slot]
        if startup:
            slots.extend(
                latest_slot - offset * AUTOMATION_INTERVAL_MS
                for offset in (1, 2)
                if now_ms - (
                    latest_slot - offset * AUTOMATION_INTERVAL_MS
                ) <= AUTOMATION_BACKFILL_MS
            )
        created = []
        with self._lock:
            old_slot_count = len(self._data["slots"])
            cutoff = latest_slot - AUTOMATION_BACKFILL_MS
            self._data["slots"] = {
                key: value for key, value in self._data["slots"].items()
                if int(key.rsplit(":", 1)[1]) >= cutoff
            }
            for entry in self._data["automations"].values():
                if not entry.get("enabled"):
                    continue
                schedules = self._schedules(entry)
                for scheduled_for_ms in slots:
                    if not any(schedule.matches(scheduled_for_ms)
                               for schedule in schedules):
                        continue
                    key = (
                        f'{entry["automationId"]}:{scheduled_for_ms}')
                    if key in self._data["slots"]:
                        continue
                    run_id = str(uuid.uuid4())
                    event = {
                        "id": run_id,
                        "sandAgentId": entry["sandAgentId"],
                        "automationId": entry["automationId"],
                        "timestampMs": now_ms,
                        "scheduledForMs": scheduled_for_ms,
                    }
                    description = entry.get("description") or ""
                    revision = description.removeprefix(
                        SAND_SHADOW_MARKER_PREFIX)
                    if (description.startswith(SAND_SHADOW_MARKER_PREFIX)
                            and re.fullmatch(r"[0-9a-f]{64}", revision)):
                        event["definitionRevision"] = revision
                    self._data["events"][run_id] = event
                    self._data["slots"][key] = run_id
                    created.append(event)
            if created or len(self._data["slots"]) != old_slot_count:
                self._save_locked()
        return created

    def poll(self, ack_run_uuids: list[str], now_ms: int
             ) -> tuple[list[dict], int, int]:
        with self._lock:
            acknowledged = 0
            for run_id in ack_run_uuids:
                event = self._data["events"].get(run_id)
                if event and event.get("completed"):
                    del self._data["events"][run_id]
                    acknowledged += 1
            events = [
                {key: value for key, value in event.items()
                 if key not in {"completed", "status", "errorMessage",
                                "completedAtMs"}}
                for event in self._data["events"].values()
                if not event.get("completed")
            ]
            if events:
                delay = 0
            else:
                delay = self._next_due_delay_locked(now_ms)
            if acknowledged:
                self._save_locked()
            return events, min(delay, AUTOMATION_INTERVAL_MS), acknowledged

    def complete(self, run_id: str, status: str,
                 error_message: str | None, now_ms: int) -> bool:
        with self._lock:
            event = self._data["events"].get(run_id)
            if event is None:
                return False
            event.update({
                "completed": True,
                "status": status,
                "errorMessage": error_message or "",
                "completedAtMs": now_ms,
            })
            self._save_locked()
            return True

    def _next_due_delay_locked(self, now_ms: int) -> int:
        automations = [
            (entry, self._schedules(entry))
            for entry in self._data["automations"].values()
            if entry.get("enabled")
        ]
        current_slot = now_ms // AUTOMATION_INTERVAL_MS
        for offset in range(1, 61):
            scheduled_for_ms = (
                current_slot + offset) * AUTOMATION_INTERVAL_MS
            if any(
                schedule.matches(scheduled_for_ms)
                for _, schedules in automations
                for schedule in schedules
            ):
                return max(0, scheduled_for_ms - now_ms)
        return AUTOMATION_INTERVAL_MS

    def start_scheduler(self) -> None:
        with self._lock:
            if self._thread and self._thread.is_alive():
                return
            self._stop.clear()
            self._thread = threading.Thread(
                target=self._scheduler_loop,
                name="grokbot-automation-scheduler",
                daemon=True,
            )
            self._thread.start()

    def stop_scheduler(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=2)
            self._thread = None

    def _scheduler_loop(self) -> None:
        self.materialize_due(int(time.time() * 1000), startup=True)
        while not self._stop.is_set():
            now = time.time()
            next_minute = (int(now // 60) + 1) * 60
            if self._stop.wait(max(0.05, next_minute - now + 0.05)):
                break
            self.materialize_due(int(time.time() * 1000))


class BoxBackend:
    def __init__(self, port: int = 7812,
                 automation_state_path: Path | None = None) -> None:
        self.port = port
        self.automations = AutomationStore(automation_state_path)
        self.gateway_token = os.environ.get("GROKBOT_GATEWAY_TOKEN", "")
        self.credential = os.environ.get(
            "GROKBOT_INFERENCE_CREDENTIAL", "")
        self.share_credential = os.environ.get(
            "GROKBOT_SHARE_CREDENTIAL", "")
        self.webapp_origin = os.environ.get(
            "DEVBOX_WEBAPP_ORIGIN", "").rstrip("/")
        self.llm_base_url = os.environ.get("GROKBOT_LLM_BASE_URL", "")
        self.llm_api_key = os.environ.get("GROKBOT_LLM_API_KEY", "")
        self.llm_model = os.environ.get("GROKBOT_LLM_MODEL", "")
        self.model_map = json.loads(
            os.environ.get("GROKBOT_LLM_MODEL_MAP", "{}") or "{}")
        self.signing_secret = secrets.token_bytes(32)
        self.session_id = os.environ.get("DEVIN_SESSION_ID", "box")
        try:
            self.user = json.loads(
                os.environ.get("GROKBOT_USER_JSON", "{}") or "{}")
        except json.JSONDecodeError:
            self.user = {}

    def _issue_access_token(self) -> dict:
        now = int(time.time())
        header = _b64url(json.dumps(
            {"alg": "HS256", "typ": "JWT"}).encode())
        payload = _b64url(json.dumps({
            "iss": f"grok-bot-box/{self.session_id}",
            "sub": "box-inference",
            "aud": "grok-bot",
            "iat": now, "exp": now + CREDENTIAL_TTL_S,
        }).encode())
        sig = _b64url(hmac.new(
            self.signing_secret, f"{header}.{payload}".encode(),
            hashlib.sha256).digest())
        return {"accessToken": f"{header}.{payload}.{sig}",
                "expiresAtMs": (now + CREDENTIAL_TTL_S) * 1000}

    def _check_bearer(self, ctx: dict) -> None:
        auth = (ctx or {}).get("authorization", "")
        token = auth.removeprefix("Bearer ").strip()
        if not token:
            raise ConnectError("unauthenticated", "sign-in required")
        try:
            _, payload, sig = token.split(".")
            claims = json.loads(base64.urlsafe_b64decode(
                payload + "=="))
        except (ValueError, json.JSONDecodeError) as exc:
            raise ConnectError(
                "unauthenticated", "malformed token") from exc
        expected = _b64url(hmac.new(
            self.signing_secret,
            f"{token.split('.')[0]}.{payload}".encode(),
            hashlib.sha256).digest())
        if not hmac.compare_digest(sig, expected):
            raise ConnectError("unauthenticated", "bad signature")
        if int(claims.get("exp", 0)) <= int(time.time()):
            raise ConnectError("unauthenticated", "token expired")

    def _automation_wire(self, entry: dict) -> dict:
        return {
            "automation_id": entry["automationId"],
            "name": entry["name"],
            "enabled": entry["enabled"],
            "workflow": entry["workflow"],
            "description": entry["description"],
        }

    def _automation_with_owner(self, entry: dict) -> dict:
        return {"workflow": self._automation_wire(entry)}

    def router(self) -> ConnectRouter:
        r = ConnectRouter()

        @r.streaming("aiserver.v1.InferenceService", "Stream")
        def _stream(req, ctx):
            self._check_bearer(ctx)
            if not self.llm_base_url:
                yield {"error": {
                    "message": "GROKBOT_LLM_BASE_URL not set on box",
                    "code": "UNKNOWN", "error_type": 1}}
                return
            yield from stream_inference(
                req, self.llm_base_url, self.llm_api_key, self.model_map,
                default_model=self.llm_model)

        @r.unary("aiserver.v1.InferenceService",
                 "RecordAgentFollowupClassification")
        def _noop1(_req, _ctx):
            return {}

        @r.unary("aiserver.v1.InferenceService",
                 "RecordAgentPostTurnLabeling")
        def _noop2(_req, _ctx):
            return {}

        def _identity(_ctx):
            self._check_bearer(_ctx)
            return self.user

        @r.unary("aiserver.v1.AutomationsService",
                 "CreateSandAutomation")
        def _create_automation(req, ctx):
            _identity(ctx)
            automation_id = req.sand_automation_id or str(uuid.uuid4())
            entry = self.automations.put({
                "automationId": automation_id,
                "sandAgentId": req.sand_agent_id,
                "name": req.name,
                "description": req.description,
                "enabled": req.enabled,
                "workflow": to_json_dict(req.workflow),
            })
            return {"workflow": self._automation_with_owner(entry)}

        @r.unary("aiserver.v1.AutomationsService",
                 "ListSandAutomations")
        def _list_automations(req, ctx):
            _identity(ctx)
            return {
                "workflows": [
                    self._automation_with_owner(entry)
                    for entry in self.automations.list(req.sand_agent_id)
                ],
            }

        @r.unary("aiserver.v1.AutomationsService",
                 "GetSandAutomation")
        def _get_automation(req, ctx):
            _identity(ctx)
            entry = self.automations.get(req.automation_id)
            if entry is None:
                raise ConnectError("not_found", "automation not found")
            return {"workflow": self._automation_with_owner(entry)}

        @r.unary("aiserver.v1.AutomationsService",
                 "UpdateSandAutomation")
        def _update_automation(req, ctx):
            _identity(ctx)
            values = {"enabled": req.enabled}
            if req.name:
                values["name"] = req.name
            if req.description:
                values["description"] = req.description
            if req.HasField("workflow"):
                values["workflow"] = to_json_dict(req.workflow)
            entry = self.automations.update(req.automation_id, values)
            if entry is None:
                raise ConnectError("not_found", "automation not found")
            return {"workflow": self._automation_with_owner(entry)}

        @r.unary("aiserver.v1.AutomationsService",
                 "DeleteSandAutomation")
        def _delete_automation(req, ctx):
            _identity(ctx)
            if not self.automations.delete(req.automation_id):
                raise ConnectError("not_found", "automation not found")
            return {}

        register(r, _identity,
                 lambda: models_from_env(llm_model=self.llm_model))
        return r

    def extra_routes(self) -> dict:
        def _json(handler, status, obj):
            body = json.dumps(obj).encode()
            handler.send_response(status)
            handler.send_header("content-type", "application/json")
            handler.send_header("content-length", str(len(body)))
            handler.end_headers()
            handler.wfile.write(body)

        def _relay_request(handler, on_response=None):
            def reply(status, body):
                if on_response is not None:
                    on_response(status)
                _json(handler, status, body)

            try:
                self._check_bearer({
                    "authorization": handler.headers.get(
                        "authorization", ""),
                })
            except ConnectError as exc:
                reply(exc.status, exc.body())
                return None
            length = int(handler.headers.get("content-length") or 0)
            if length < 0 or length > MAX_RELAY_BODY:
                reply(413, {"error": "request_too_large"})
                return None
            try:
                request = json.loads(handler.rfile.read(length) or b"{}")
            except (ValueError, UnicodeDecodeError):
                reply(400, {"error": "invalid JSON body"})
                return None
            if not isinstance(request, dict):
                reply(400, {"error": "object body required"})
                return None
            return request

        def share_relay(handler):
            path = urllib.parse.urlsplit(handler.path).path

            def log_response(status):
                log.info("grokbot.sharing.relay path=%s status=%d",
                         path, status)

            def reply(status, body):
                log_response(status)
                _json(handler, status, body)

            request = _relay_request(handler, log_response)
            if request is None:
                return
            if path not in SHARE_RELAY_PATHS:
                reply(404, {"error": "not_found"})
                return
            if not self.share_credential or not self.webapp_origin:
                reply(503, {"error": "sharing_not_configured"})
                return
            upstream = urllib.request.Request(
                f"{self.webapp_origin}/api/grokbot{path}",
                data=json.dumps(request).encode(),
                headers={
                    "accept": "application/json",
                    "authorization": f"Bearer {self.share_credential}",
                    "content-type": "application/json",
                    "user-agent": "devbox-bot/0.1",
                },
                method="POST",
            )
            try:
                with urllib.request.urlopen(upstream, timeout=15) as response:
                    status = response.status
                    raw = response.read(MAX_RELAY_BODY + 1)
            except urllib.error.HTTPError as exc:
                status = exc.code
                raw = exc.read(MAX_RELAY_BODY + 1)
            except (OSError, TimeoutError):
                reply(502, {"error": "sharing_service_unavailable"})
                return
            if len(raw) > MAX_RELAY_BODY:
                reply(502, {"error": "sharing_response_too_large"})
                return
            try:
                response_body = json.loads(raw or b"{}")
            except (ValueError, UnicodeDecodeError):
                reply(502, {"error": "invalid_sharing_response"})
                return
            if not isinstance(response_body, dict):
                reply(502, {"error": "invalid_sharing_response"})
                return
            reply(status, response_body)

        def _credential_exchange(h):
            length = int(h.headers.get("content-length") or 0)
            raw = h.rfile.read(length)
            ctype = h.headers.get("content-type", "")
            if "application/json" in ctype:
                try:
                    body = json.loads(raw or b"{}")
                except ValueError:
                    body = {}
            else:
                body = {k: v[0] for k, v in urllib.parse.parse_qs(
                    raw.decode("utf-8", "replace")).items()}
            credential = str(
                body.get("credential")
                or body.get("renewal_credential")
                or h.headers.get("x-renewal-credential", ""))
            if not self.credential or not hmac.compare_digest(
                    credential, self.credential):
                _json(h, 401, {"error": "invalid credential"})
                return
            _json(h, 200, self._issue_access_token())

        def health(h):
            _json(h, 200, {"ok": True})

        def automation_events_poll(h):
            request = _relay_request(h)
            if request is None:
                return
            ack_run_uuids = request.get("ackRunUuids", [])
            if not isinstance(ack_run_uuids, list):
                ack_run_uuids = []
            now_ms = int(time.time() * 1000)
            self.automations.materialize_due(now_ms)
            events, delay_ms, acknowledged = self.automations.poll(
                [item for item in ack_run_uuids if isinstance(item, str)],
                now_ms,
            )
            log.info(
                "grokbot.automation.relay poll pending=%d acknowledged=%d",
                len(events), acknowledged)
            _json(h, 200, {
                "events": events,
                "nextPollAfterMs": delay_ms,
            })

        def automation_run_complete(h):
            request = _relay_request(h)
            if request is None:
                return
            run_id = request.get("runUuid")
            if not isinstance(run_id, str) or not self.automations.complete(
                    run_id, str(request.get("status") or ""),
                    request.get("errorMessage"), int(time.time() * 1000)):
                _json(h, 404, {"error": "unknown runUuid"})
                return
            log.info("grokbot.automation.relay complete")
            _json(h, 200, {})

        def listener_subscriptions(h):
            if _relay_request(h) is None:
                return
            log.info("grokbot.listener.relay subscriptions")
            _json(h, 200, {
                "slack": {"status": "not-linked", "teams": []},
                "github": {"status": "not-connected", "repos": []},
            })

        def listener_events_poll(h):
            if _relay_request(h) is None:
                return
            log.info("grokbot.listener.relay poll")
            _json(h, 200, {"events": []})

        return {
            ("GET", "/healthz"): health,
            ("POST", "/sand-box/inference-credential"):
                _credential_exchange,
            ("POST", "/sand-box/local-exec-daemon-credential"):
                _credential_exchange,
            ("POST", "/sand/automation-events/poll"):
                automation_events_poll,
            ("POST", "/sand/automation-runs/complete"):
                automation_run_complete,
            ("POST", "/sand/listener-subscriptions"):
                listener_subscriptions,
            ("POST", "/sand/listener-events/poll"):
                listener_events_poll,
            **{
                ("POST", path): share_relay
                for path in SHARE_RELAY_PATHS
            },
        }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="devbox_bot.box")
    parser.add_argument("--port", type=int, default=7812)
    args = parser.parse_args(argv)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s")
    backend = BoxBackend(port=args.port)
    backend.automations.start_scheduler()
    server = serve(backend.router(), "127.0.0.1", args.port,
                   extra_routes=backend.extra_routes())
    log.info("devbox-bot box backend on http://127.0.0.1:%d",
             args.port)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        backend.automations.stop_scheduler()
    return 0


if __name__ == "__main__":
    sys.exit(main())
