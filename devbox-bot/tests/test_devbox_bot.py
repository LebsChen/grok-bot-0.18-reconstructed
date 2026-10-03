"""Tests for devbox_bot (plugin/bot). Pure stdlib + protobuf; no DevBox
imports — the bot only uses DevBox's *existing* HTTP APIs."""

from __future__ import annotations

import base64
import hashlib
import http.client
import http.server
import importlib.util
import io
import json
import logging
import os
import re
import socket
import subprocess
import stat
import sys
import tarfile
import threading
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from devbox_bot import codec as codec_mod
from devbox_bot.box import (
    AutomationStore,
    BoxBackend,
    cron_matches,
)
from devbox_bot.codec import ConnectError
from devbox_bot.connect import (
    ConnectRouter,
    make_handler,
    reset_unhandled,
    unhandled_methods,
)
from devbox_bot.desktop import BOX_PROMPT, DesktopBackend, _pkce_ok
from devbox_bot.devbox_api import ApiError
from devbox_bot.handlers import DEVBOX_GATE_OVERRIDES, register
from devbox_bot.inference import request_to_openai, stream_inference


def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _serve(handler_cls):
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0),
                                             handler_cls)
    server.daemon_threads = True
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    return server, server.server_address[1]


def _req(port, method, path, body=b"", headers=None):
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}{path}", data=body or None,
        method=method, headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status, resp.read(), dict(resp.headers)
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read(), dict(exc.headers)


def _statsig_config():
    router = ConnectRouter()
    register(router, lambda _ctx: {"user_id": "statsig-user-123"},
             list)
    status, _, body = router.dispatch(
        "aiserver.v1.AnalyticsService", "BootstrapStatsig",
        "application/json", b"{}")
    assert status == 200
    return json.loads(json.loads(body)["config"])


def test_statsig_bootstrap_includes_all_gates_and_identity():
    config = _statsig_config()
    gates = config["feature_gates"]

    assert len(gates) == 608
    assert config["response_format"] == "init-v1"
    assert config["generator"] == "devbox"
    assert config["hash_used"] == "none"
    assert config["has_updates"] is True
    assert config["dynamic_configs"] == {}
    assert config["layer_configs"] == {}
    assert config["param_stores"] == {}
    assert config["user"]["userID"] == "statsig-user-123"
    assert all(
        set(gate) == {
            "name", "value", "rule_id", "id_type", "secondary_exposures"}
        and gate["name"] == name
        and gate["rule_id"] == "devbox"
        and gate["id_type"] == "userID"
        and gate["secondary_exposures"] == []
        for name, gate in gates.items())
    assert {name for name in DEVBOX_GATE_OVERRIDES
            if gates[name]["value"]} == DEVBOX_GATE_OVERRIDES
    assert gates["sand_auto_review"]["value"] is False
    assert gates["sand_global_search"]["value"] is True


def test_statsig_bootstrap_environment_opt_out_and_unknown_gate(
        monkeypatch, caplog):
    monkeypatch.setenv(
        "GROKBOT_FEATURE_GATES", "!sand_multiplayer,unknown_test_gate")
    with caplog.at_level(logging.WARNING, logger="devbox_bot.handlers"):
        config = _statsig_config()

    assert config["feature_gates"]["sand_multiplayer"]["value"] is False
    assert "unknown_test_gate" in caplog.text


@pytest.mark.parametrize(
    ("expression", "when", "expected"),
    [
        ("*/5 * * * *", datetime(2026, 2, 2, 9, 15, tzinfo=timezone.utc),
         True),
        ("*/5 * * * *", datetime(2026, 2, 2, 9, 16, tzinfo=timezone.utc),
         False),
        ("0 9 * * 1-5", datetime(2026, 2, 2, 9, 0, tzinfo=timezone.utc),
         True),
        ("0 9 * * 1-5", datetime(2026, 2, 7, 9, 0, tzinfo=timezone.utc),
         False),
        ("0,15,30,45 8-10 * * *",
         datetime(2026, 2, 2, 9, 30, tzinfo=timezone.utc), True),
        ("CRON_TZ=America/Los_Angeles 0 9 * * 1-5",
         datetime(2026, 2, 2, 17, 0, tzinfo=timezone.utc), True),
    ],
)
def test_cron_schedule_matcher(expression, when, expected):
    assert cron_matches(expression, when) is expected


def _automation_http(tmp_path):
    backend = BoxBackend(
        automation_state_path=tmp_path / "automations.json")
    handler = make_handler(backend.router(), backend.extra_routes())
    server, port = _serve(handler)
    token = backend._issue_access_token()["accessToken"]
    headers = {
        "content-type": "application/json",
        "authorization": f"Bearer {token}",
    }

    def rpc(method, body, request_headers=headers):
        status, raw, _ = _req(
            port, "POST",
            f"/aiserver.v1.AutomationsService/{method}",
            json.dumps(body).encode(), request_headers)
        return status, json.loads(raw or b"{}")

    def route(path, body, request_headers=headers):
        status, raw, _ = _req(
            port, "POST", path, json.dumps(body).encode(),
            request_headers)
        return status, json.loads(raw or b"{}")

    return backend, server, port, rpc, route


def _automation_request():
    return {
        "name": "Daily digest",
        "description": f"sand-shadow:{'a' * 64}",
        "enabled": True,
        "sandAgentId": "agent-one",
        "sandAutomationId": "automation-one",
        "workflow": {
            "triggers": [{
                "cron": {"cron": "CRON_TZ=UTC */5 * * * *"},
            }],
            "prompts": [{"prompt": "Reply with DAILY"}],
        },
    }


def test_cloud_automation_crud_round_trip_and_durable_state(tmp_path):
    _backend, server, _, rpc, _ = _automation_http(tmp_path)
    state_path = tmp_path / "automations.json"
    try:
        status, created = rpc(
            "CreateSandAutomation", _automation_request())
        assert status == 200
        assert created["workflow"]["workflow"]["automation_id"] == \
            "automation-one"
        assert state_path.stat().st_mode & 0o777 == 0o600

        status, listed = rpc(
            "ListSandAutomations", {"sandAgentId": "agent-one"})
        assert status == 200
        automation = listed["workflows"][0]["workflow"]
        assert automation["automation_id"] == "automation-one"
        assert automation["description"] == f"sand-shadow:{'a' * 64}"
        assert automation["enabled"] is True

        status, fetched = rpc(
            "GetSandAutomation", {"automationId": "automation-one"})
        assert status == 200
        assert fetched["workflow"]["workflow"]["name"] == "Daily digest"

        status, updated = rpc(
            "UpdateSandAutomation", {
                "automationId": "automation-one",
                "description": "updated",
                "enabled": False,
            })
        assert status == 200
        assert updated["workflow"]["workflow"]["description"] == "updated"
        status, listed = rpc(
            "ListSandAutomations", {"sandAgentId": "agent-one"})
        assert status == 200
        assert not listed["workflows"][0]["workflow"].get(
            "enabled", False)

        restored = AutomationStore(state_path)
        assert restored.get("automation-one")["description"] == "updated"
        status, deleted = rpc(
            "DeleteSandAutomation", {"automationId": "automation-one"})
        assert status == 200
        assert deleted == {}
        assert AutomationStore(state_path).get("automation-one") is None
        status, listed = rpc(
            "ListSandAutomations", {"sandAgentId": "agent-one"})
        assert status == 200
        assert listed.get("workflows", []) == []
    finally:
        server.shutdown()
        server.server_close()


def test_cloud_automation_fire_poll_complete_and_ack_lifecycle(tmp_path):
    backend, server, _, _, route = _automation_http(tmp_path)
    current = datetime.now(timezone.utc)
    target_month = current.month % 12 + 1
    target_year = current.year + (target_month == 1)
    scheduled_for = datetime(
        target_year, target_month, 2, 9, 15, tzinfo=timezone.utc)
    now_ms = int(scheduled_for.timestamp() * 1000)
    try:
        backend.automations.put({
            "automationId": "automation-one",
            "sandAgentId": "agent-one",
            "name": "Daily digest",
            "description": f"sand-shadow:{'b' * 64}",
            "enabled": True,
            "workflow": {
                "triggers": [{
                    "cron": {
                        "cron": f"CRON_TZ=UTC 15 9 2 {target_month} *",
                    },
                }],
            },
        })
        assert len(backend.automations.materialize_due(now_ms)) == 1
        status, polled = route(
            "/sand/automation-events/poll", {"ackRunUuids": []})
        assert status == 200
        assert len(polled["events"]) == 1
        event = polled["events"][0]
        assert event["sandAgentId"] == "agent-one"
        assert event["automationId"] == "automation-one"
        assert event["scheduledForMs"] == now_ms
        assert event["definitionRevision"] == "b" * 64

        status, unknown = route(
            "/sand/automation-runs/complete", {
                "runUuid": "unknown-run",
                "status": "succeeded",
            })
        assert status == 404
        assert "unknown runUuid" in unknown["error"]

        status, completed = route(
            "/sand/automation-runs/complete", {
                "runUuid": event["id"],
                "status": "succeeded",
                "errorMessage": "",
            })
        assert status == 200
        assert completed == {}
        status, pending = route(
            "/sand/automation-events/poll", {"ackRunUuids": []})
        assert status == 200
        assert pending["events"] == []
        status, acknowledged = route(
            "/sand/automation-events/poll", {
                "ackRunUuids": [event["id"]],
            })
        assert status == 200
        assert acknowledged["events"] == []
        assert acknowledged["nextPollAfterMs"] <= 60_000
        assert event["id"] not in backend.automations._data["events"]
    finally:
        server.shutdown()
        server.server_close()


def test_cloud_automation_auth_and_listener_relay_routes(tmp_path):
    _backend, server, _, rpc, route = _automation_http(tmp_path)
    unauthenticated = {"content-type": "application/json"}
    try:
        status, _ = rpc(
            "CreateSandAutomation", _automation_request(),
            request_headers=unauthenticated)
        assert status == 401
        status, _ = route(
            "/sand/automation-events/poll", {"ackRunUuids": []},
            request_headers=unauthenticated)
        assert status == 401
        status, subscriptions = route(
            "/sand/listener-subscriptions", {
                "slackChannels": [],
                "githubRepos": [],
                "githubKinds": [],
            })
        assert status == 200
        assert subscriptions["slack"] == {
            "status": "not-linked", "teams": []}
        assert subscriptions["github"] == {
            "status": "not-connected", "repos": []}
        status, events = route(
            "/sand/listener-events/poll", {"ackIds": []})
        assert status == 200
        assert events["events"] == []
    finally:
        server.shutdown()
        server.server_close()


def test_automation_scheduler_does_not_duplicate_slots_or_backfill_old(
        tmp_path):
    store = AutomationStore(tmp_path / "automations.json")
    entry = {
        "automationId": "automation-one",
        "sandAgentId": "agent-one",
        "name": "Every minute",
        "description": "",
        "enabled": True,
        "workflow": {
            "triggers": [{"cron": {"cron": "CRON_TZ=UTC * * * * *"}}],
        },
    }
    store.put(entry)
    slot_ms = int(datetime(
        2026, 2, 2, 9, 15, tzinfo=timezone.utc).timestamp() * 1000)
    first = store.materialize_due(slot_ms, startup=True)
    assert {event["scheduledForMs"] for event in first} == {
        slot_ms - 120_000, slot_ms - 60_000, slot_ms,
    }
    assert store.materialize_due(slot_ms, startup=True) == []
    restored = AutomationStore(tmp_path / "automations.json")
    assert restored.materialize_due(slot_ms, startup=True) == []

    three_minutes_later = slot_ms + 3 * 60_000
    backfilled = restored.materialize_due(
        three_minutes_later, startup=True)
    scheduled_for = {event["scheduledForMs"] for event in backfilled}
    assert scheduled_for == {
        three_minutes_later - 120_000,
        three_minutes_later - 60_000,
        three_minutes_later,
    }


# ── PKCE / login ────────────────────────────────────────────────────

def test_pkce_ok():
    verifier = "v" * 43
    challenge = _b64url(hashlib.sha256(verifier.encode()).digest())
    assert _pkce_ok(challenge, verifier)
    assert not _pkce_ok(challenge, "wrong")


@pytest.fixture()
def desktop(tmp_path):
    backend = DesktopBackend(port=0, api_key=None,
                             state_path=tmp_path / "state.json")
    backend.devbox_origin = "http://127.0.0.1:9"  # unused in unit tests
    handler = make_handler(backend.router(), backend.extra_routes())
    server, port = _serve(handler)
    backend.port = port
    yield backend, port
    server.shutdown()


def test_poll_flow(desktop):
    backend, port = desktop
    uuid = "u1"
    verifier = "verifier-abc"
    challenge = _b64url(hashlib.sha256(verifier.encode()).digest())
    backend.pending[uuid] = {"challenge": challenge, "tokens": None,
                             "expires": 9e15}
    status, body, _ = _req(port, "GET",
                           f"/auth/poll?uuid={uuid}&verifier={verifier}")
    assert status == 404
    backend.pending[uuid]["tokens"] = {
        "accessToken": "at", "refreshToken": "rt", "authId": "u"}
    status, body, _ = _req(port, "GET",
                           f"/auth/poll?uuid={uuid}&verifier={verifier}")
    assert status == 200
    assert json.loads(body)["accessToken"] == "at"
    # one-shot
    status, _, _ = _req(port, "GET",
                        f"/auth/poll?uuid={uuid}&verifier={verifier}")
    assert status == 404


def test_poll_wrong_verifier(desktop):
    backend, port = desktop
    backend.pending["u2"] = {"challenge": _b64url(b"x"),
                             "tokens": {"accessToken": "a"},
                             "expires": 9e15}
    status, _, _ = _req(port, "GET", "/auth/poll?uuid=u2&verifier=bad")
    assert status == 403


def test_api_key_mode_off_by_default(desktop):
    """Without DEVBOX_API_KEY the headless approve path must not
    auto-complete."""
    backend, port = desktop
    status, _, _ = _req(
        port, "POST", "/loginDeepControl/approve",
        b'{"uuid":"u3","challenge":"c"}',
        {"content-type": "application/json",
         "authorization": "Bearer cog_fake"})
    assert status in (400, 401, 403) or (
        status == 200 and not backend.pending.get("u3", {}).get(
            "tokens"))


# ── refresh ─────────────────────────────────────────────────────────

def test_refresh_passthrough(desktop, monkeypatch):
    backend, port = desktop
    backend.api_key = None
    calls = {}

    class FakeOidc:
        @staticmethod
        def oidc_token(origin, form, timeout=30):
            calls["form"] = form
            if form.get("refresh_token") == "good":
                return 200, {"access_token": "new-at",
                             "refresh_token": "new-rt"}
            return 400, {"error": "invalid_grant"}

    monkeypatch.setattr(
        "devbox_bot.devbox_api.DevBoxApi.oidc_token",
        FakeOidc.oidc_token)
    status, body, _ = _req(
        port, "POST", "/oauth/token",
        json.dumps({"client_id": "KbZUR41cY7W6zRSdpSUJ7I7mLYBKOCmB",
                    "grant_type": "refresh_token",
                    "refresh_token": "good"}).encode(),
        {"content-type": "application/json"})
    assert status == 200
    assert json.loads(body)["access_token"] == "new-at"
    status, body, _ = _req(
        port, "POST", "/oauth/token",
        json.dumps({"client_id": "KbZUR41cY7W6zRSdpSUJ7I7mLYBKOCmB",
                    "grant_type": "refresh_token",
                    "refresh_token": "bad"}).encode(),
        {"content-type": "application/json"})
    assert json.loads(body)["shouldLogout"] is True


def test_headless_refresh_returns_oauth_token_fields(tmp_path, monkeypatch):
    backend = DesktopBackend(
        port=0, api_key="cog-test", state_path=tmp_path / "state.json")
    backend.state.data["refresh"] = {"gbr_local_refresh": "old-access"}
    monkeypatch.setattr(
        backend, "_headless_tokens",
        lambda: {"accessToken": "new-access",
                 "refreshToken": "new-refresh", "authId": "user"})
    handler = make_handler(backend.router(), backend.extra_routes())
    server, port = _serve(handler)
    try:
        status, body, _ = _req(
            port, "POST", "/oauth/token",
            json.dumps({"client_id": "KbZUR41cY7W6zRSdpSUJ7I7mLYBKOCmB",
                        "grant_type": "refresh_token",
                        "refresh_token": "gbr_local_refresh"}).encode(),
            {"content-type": "application/json"})
        tokens = json.loads(body)
        assert status == 200
        assert tokens["access_token"] == "new-access"
        assert tokens["refresh_token"] == "new-refresh"
        assert tokens["token_type"] == "Bearer"
        assert "accessToken" not in tokens
    finally:
        server.shutdown()


# ── Connect framing ─────────────────────────────────────────────────

def test_unary_proto_roundtrip(desktop):
    _, port = desktop
    reset_unhandled()
    req = codec_mod.codec().new("aiserver.v1.GetMeRequest")
    # unauthenticated → Connect error JSON
    status, body, _ = _req(
        port, "POST", "/aiserver.v1.DashboardService/GetMe",
        codec_mod.codec().encode(req),
        {"content-type": "application/proto"})
    assert status == 401
    assert json.loads(body)["code"] == "unauthenticated"


def test_catch_all_empty(desktop):
    _, port = desktop
    reset_unhandled()
    status, _, _ = _req(
        port, "POST", "/aiserver.v1.AiService/ServerTime",
        b"", {"content-type": "application/proto"})
    assert status == 200
    assert "aiserver.v1.AiService/ServerTime" in unhandled_methods()


def test_streaming_catch_all(desktop):
    _, port = desktop
    reset_unhandled()
    body = codec_mod.connect_envelope(b"")
    status, resp, _ = _req(
        port, "POST", "/agent.v1.AgentService/RunSSE",
        body, {"content-type": "application/connect+proto"})
    assert status == 200
    # end-stream frame only
    assert resp[0] == 0x02


# ── inference translation ───────────────────────────────────────────

class _FakeSSE(io.RawIOBase):
    def __init__(self, lines):
        self._it = iter([l.encode() + b"\n" for l in lines])

    def readable(self):
        return True

    def readinto(self, b):
        try:
            data = next(self._it)
        except StopIteration:
            return 0
        b[:len(data)] = data
        return len(data)

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def test_stream_inference(monkeypatch):
    sse = _FakeSSE([
        'data: {"choices":[{"delta":{"content":"he"}}]}',
        'data: {"choices":[{"delta":{"reasoning_content":"th"}}]}',
        ('data: {"choices":[{"delta":{"tool_calls":[{"index":0,'
         '"id":"c1","function":{"name":"f",'
         '"arguments":"{}"}}]},"finish_reason":"tool_calls"}]}'),
        ('data: {"usage":{"prompt_tokens":3,"completion_tokens":2,'
         '"total_tokens":5}}'),
        'data: [DONE]',
    ])
    monkeypatch.setattr(urllib.request, "urlopen",
                        lambda *a, **k: sse)
    req = codec_mod.codec().new(
        "aiserver.v1.InferenceStreamRequest",
        model_id="m",
        messages=[{"role": 1, "text": "hi"}])
    frames = list(stream_inference(req, "http://x", "k"))
    kinds = [f.WhichOneof("response") for f in frames]
    assert "invocation_id" in kinds and "text_part" in kinds
    assert "thinking_part" in kinds and "tool_call_part" in kinds
    usage = [f for f in frames
             if f.WhichOneof("response") == "usage"]
    assert usage and usage[0].usage.total_tokens == 5


def test_stream_inference_preserves_tool_call_arguments(monkeypatch, caplog):
    events = [
        {"choices": [{"delta": {"tool_calls": [
            {"index": 0, "id": "call-0",
             "function": {"name": "SendMessage"}},
            {"index": 1, "id": "call-1",
             "function": {"name": "TodoWrite"}},
        ]}, "finish_reason": ""}]},
        {"choices": [{"delta": {"tool_calls": [
            {"index": 0, "function": {"arguments": '{"content":'}},
            {"index": 1, "function": {"arguments": '{"todos":'}},
        ]}, "finish_reason": ""}]},
        {"choices": [{"delta": {"tool_calls": [
            {"index": 0, "function": {"arguments": '"hello"}'}},
            {"index": 1, "function": {"arguments": '[]}' }},
        ]}, "finish_reason": ""}]},
        {"choices": [{"delta": {}, "finish_reason": "tool_calls"}]},
        {"choices": [{"delta": {}, "finish_reason": "tool_calls"}]},
    ]
    sse = _FakeSSE([f"data: {json.dumps(event)}" for event in events]
                   + ["data: [DONE]"])
    monkeypatch.setattr(urllib.request, "urlopen",
                        lambda *a, **k: sse)
    caplog.set_level("INFO", logger="devbox_bot.inference")

    frames = list(stream_inference(
        _inference_request("m"), "http://x", "k"))
    parts = [
        frame.tool_call_part
        for frame in frames
        if frame.WhichOneof("response") == "tool_call_part"
    ]
    starts = [part for part in parts
              if not part.is_complete and part.tool_name]
    deltas = [part for part in parts
              if not part.is_complete and not part.tool_name]
    completed = [part for part in parts if part.is_complete]

    assert [(part.tool_index, part.tool_name, part.tool_call_id,
             part.args) for part in starts] == [
        (0, "SendMessage", "call-0", ""),
        (1, "TodoWrite", "call-1", ""),
    ]
    assert [(part.tool_index, part.tool_call_id, part.args)
            for part in deltas] == [
        (0, "call-0", '{"content":'),
        (1, "call-1", '{"todos":'),
        (0, "call-0", '"hello"}'),
        (1, "call-1", '[]}'),
    ]
    assert all(not part.tool_name for part in deltas)
    assert [(part.tool_index, part.tool_name, part.tool_call_id)
            for part in completed] == [
        (0, "SendMessage", "call-0"),
        (1, "TodoWrite", "call-1"),
    ]
    assert [json.loads(part.args) for part in completed] == [
        {"content": "hello"},
        {"todos": []},
    ]
    assert "tools=SendMessage,TodoWrite" in caplog.text
    assert "hello" not in caplog.text


def test_stream_inference_buffers_args_until_tool_start(monkeypatch):
    events = [
        {"choices": [{"delta": {"tool_calls": [
            {"index": 0, "function": {"arguments": '{"x":'}},
        ]}, "finish_reason": ""}]},
        {"choices": [{"delta": {"tool_calls": [
            {"index": 0, "id": "call-0",
             "function": {"name": "SendMessage", "arguments": "1}"}},
        ]}, "finish_reason": ""}]},
        {"choices": [{"delta": {}, "finish_reason": "tool_calls"}]},
    ]
    sse = _FakeSSE([f"data: {json.dumps(event)}" for event in events]
                   + ["data: [DONE]"])
    monkeypatch.setattr(urllib.request, "urlopen",
                        lambda *a, **k: sse)

    frames = list(stream_inference(
        _inference_request("m"), "http://x", "k"))
    parts = [
        frame.tool_call_part
        for frame in frames
        if frame.WhichOneof("response") == "tool_call_part"
    ]

    assert [(part.tool_name, part.args, part.is_complete)
            for part in parts] == [
        ("SendMessage", "", False),
        ("", '{"x":1}', False),
        ("SendMessage", '{"x":1}', True),
    ]


def test_stream_inference_logs_only_safe_request_metadata(monkeypatch, caplog):
    sse = _FakeSSE([
        ('data: {"choices":[{"delta":{"content":"private reply"},'
         '"finish_reason":"stop"}]}'),
        ('data: {"usage":{"prompt_tokens":1,"completion_tokens":1,'
         '"total_tokens":2}}'),
        'data: [DONE]',
    ])
    monkeypatch.setattr(urllib.request, "urlopen",
                        lambda *a, **k: sse)
    caplog.set_level("INFO", logger="devbox_bot.inference")
    req = codec_mod.codec().new(
        "aiserver.v1.InferenceStreamRequest",
        model_id="unknown-model",
        messages=[{"role": 1, "text": "private prompt"}])

    list(stream_inference(
        req, "http://x", "private-key", default_model="kimi-k3"))

    assert "requested=unknown-model upstream=kimi-k3 messages=1 tools=0 " \
        "body_bytes=" in caplog.text
    assert ("text_parts=1 thinking_parts=0 tool_calls=0 "
            "finish_reason=stop usage=y") in caplog.text
    assert "private prompt" not in caplog.text
    assert "private reply" not in caplog.text
    assert "private-key" not in caplog.text


def test_stream_inference_truncated_without_done_emits_error(
        monkeypatch, caplog):
    events = [
        {"choices": [{"delta": {"content": "hi"}, "finish_reason": ""}]},
        {"choices": [{"delta": {"tool_calls": [
            {"index": 0, "id": "call-0",
             "function": {"name": "Read", "arguments": '{"p":'}},
        ]}, "finish_reason": ""}]},
    ]
    sse = _FakeSSE([f"data: {json.dumps(event)}" for event in events])
    monkeypatch.setattr(urllib.request, "urlopen",
                        lambda *a, **k: sse)
    caplog.set_level("WARNING", logger="devbox_bot.inference")

    frames = list(stream_inference(
        _inference_request("m"), "http://x", "k"))

    last = frames[-1]
    assert last.WhichOneof("response") == "error"
    assert last.error.code == "OVERLOADED"
    assert last.error.error_type == 7
    assert "without finish_reason" in last.error.message
    assert all(not part.is_complete for part in (
        frame.tool_call_part for frame in frames
        if frame.WhichOneof("response") == "tool_call_part"))
    assert all(not part.is_final for part in (
        frame.text_part for frame in frames
        if frame.WhichOneof("response") == "text_part"))
    assert "upstream stream truncated: done=False pending_tools=1" \
        in caplog.text


def test_stream_inference_done_with_pending_tool_emits_error(monkeypatch):
    events = [
        {"choices": [{"delta": {"tool_calls": [
            {"index": 0, "id": "call-0",
             "function": {"name": "Read", "arguments": '{"p":'}},
        ]}, "finish_reason": ""}]},
    ]
    sse = _FakeSSE([f"data: {json.dumps(event)}" for event in events]
                   + ["data: [DONE]"])
    monkeypatch.setattr(urllib.request, "urlopen",
                        lambda *a, **k: sse)

    frames = list(stream_inference(
        _inference_request("m"), "http://x", "k"))

    last = frames[-1]
    assert last.WhichOneof("response") == "error"
    assert last.error.code == "OVERLOADED"
    assert last.error.error_type == 7
    assert "without finish_reason" in last.error.message
    assert all(not part.is_complete for part in (
        frame.tool_call_part for frame in frames
        if frame.WhichOneof("response") == "tool_call_part"))


def test_stream_inference_text_only_done_without_finish_reason_ok(
        monkeypatch):
    sse = _FakeSSE([
        'data: {"choices":[{"delta":{"content":"done"},'
        '"finish_reason":""}]}',
        'data: [DONE]',
    ])
    monkeypatch.setattr(urllib.request, "urlopen",
                        lambda *a, **k: sse)

    frames = list(stream_inference(
        _inference_request("m"), "http://x", "k"))

    assert all(frame.WhichOneof("response") != "error" for frame in frames)


def _inference_request(model_id):
    return codec_mod.codec().new(
        "aiserver.v1.InferenceStreamRequest",
        model_id=model_id,
        messages=[{"role": 1, "text": "hi"}])


def test_inference_uses_default_model_for_unknown_id():
    body = request_to_openai(
        _inference_request("unknown"), default_model="kimi-k3")
    assert body["model"] == "kimi-k3"


def test_inference_model_map_precedes_default_model():
    body = request_to_openai(
        _inference_request("client-model"),
        {"client-model": "mapped-model"}, default_model="kimi-k3")
    assert body["model"] == "mapped-model"


def test_inference_without_default_preserves_requested_model():
    body = request_to_openai(_inference_request("requested-model"))
    assert body["model"] == "requested-model"


def test_inference_empty_requested_id_uses_default_model():
    body = request_to_openai(
        _inference_request(""), default_model="kimi-k3")
    assert body["model"] == "kimi-k3"


def test_runtime_pack_includes_every_devbox_bot_module(tmp_path):
    package_dir = Path(__file__).resolve().parent.parent / "devbox_bot"
    base = tmp_path / "base.tar.gz"
    output = tmp_path / "runtime.tar.gz"
    second_output = tmp_path / "runtime-second.tar.gz"
    box_exec_daemon = tmp_path / "box-exec-daemon.cjs"
    box_exec_daemon.write_bytes(b"bundled exec-daemon")
    with tarfile.open(base, "w:gz") as archive:
        for name, payload in (
                ("MANIFEST.json", b"{}"),
                ("devbox_bot/stale.py", b"stale"),
                ("opt-sand/box-exec-daemon/main.cjs", b"old daemon"),
                ("opt-sand/exec-daemon/exec-daemon", b"old shim"),
                ("opt-sand/sand-host/box-scripts/box-x11vnc",
                 b"old box-x11vnc"),
                ("opt-sand/grok-bot-box/start-box.sh", b"old start-box")):
            info = tarfile.TarInfo(name)
            info.size = len(payload)
            archive.addfile(info, io.BytesIO(payload))

    script = package_dir.parent / "tools" / "repack_runtime_pack.py"
    subprocess.run([
        sys.executable, str(script), str(base), str(output),
        "--source", str(package_dir), "--box-exec-daemon",
        str(box_exec_daemon),
    ], check=True, capture_output=True, text=True)
    subprocess.run([
        sys.executable, str(script), str(base), str(second_output),
        "--source", str(package_dir), "--box-exec-daemon",
        str(box_exec_daemon),
    ], check=True, capture_output=True, text=True)
    assert hashlib.sha256(output.read_bytes()).digest() == \
        hashlib.sha256(second_output.read_bytes()).digest()

    source_files = {
        f"devbox_bot/{path.relative_to(package_dir).as_posix()}":
            hashlib.sha256(path.read_bytes()).hexdigest()
        for path in package_dir.rglob("*")
        if path.is_file() and "__pycache__" not in path.parts
        and ".pytest_cache" not in path.parts
        and path.suffix != ".pyc"
    }
    with tarfile.open(output, "r:gz") as archive:
        members = archive.getmembers()
        packed = {
            member.name.removeprefix("./"):
                hashlib.sha256(archive.extractfile(member).read()).hexdigest()
            for member in members
            if member.isfile()
            and member.name.removeprefix("./").startswith("devbox_bot/")
        }
        assert "devbox_bot/stale.py" not in packed
        assert packed == source_files
        daemon_members = [
            member for member in members
            if member.name.removeprefix("./")
            == "opt-sand/box-exec-daemon/main.cjs"
        ]
        assert len(daemon_members) == 1
        assert daemon_members[0].mode == 0o644
        assert archive.extractfile(daemon_members[0]).read() == \
            box_exec_daemon.read_bytes()
        shim_members = [
            member for member in members
            if member.name.removeprefix("./")
            == "opt-sand/exec-daemon/exec-daemon"
        ]
        shim_path = package_dir.parent / "box" / "exec-daemon"
        assert len(shim_members) == 1
        assert shim_members[0].mode == 0o755
        assert archive.extractfile(shim_members[0]).read() == \
            shim_path.read_bytes()
        start_box_members = [
            member for member in members
            if member.name.removeprefix("./")
            == "opt-sand/grok-bot-box/start-box.sh"
        ]
        assert len(start_box_members) == 1
        assert start_box_members[0].mode == 0o644
        assert archive.extractfile(start_box_members[0]).read() == \
            (package_dir.parent / "box" / "start-box.sh").read_bytes()
        box_x11vnc_members = [
            member for member in members
            if member.name.removeprefix("./")
            == "opt-sand/sand-host/box-scripts/box-x11vnc"
        ]
        box_x11vnc_path = package_dir.parent / "box" / "box-x11vnc"
        assert len(box_x11vnc_members) == 1
        assert box_x11vnc_members[0].mode == 0o755
        assert archive.extractfile(box_x11vnc_members[0]).read() == \
            box_x11vnc_path.read_bytes()


def test_box_x11vnc_translates_args_and_uses_xtigervnc_password_file(
        tmp_path):
    package_dir = Path(__file__).resolve().parent.parent
    shim = package_dir / "box" / "box-x11vnc"
    cmdline = tmp_path / "xtigervnc.cmdline"
    password_file = tmp_path / "vnc.passwd"
    password_file.write_bytes(b"stub")
    cmdline.write_bytes(b"\0".join((
        b"/usr/bin/Xtigervnc",
        b":0",
        b"-rfbport",
        b"5901",
        b"-PasswordFile",
        os.fsencode(password_file),
    )))
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    capture = tmp_path / "x0vncserver-args.json"
    server_stub = bin_dir / "x0vncserver"
    server_stub.write_text(
        "#!/usr/bin/env python3\n"
        "import json, os, sys\n"
        "with open(os.environ['BOX_X11VNC_CAPTURE'], 'w') as stream:\n"
        "    json.dump(sys.argv[1:], stream)\n")
    server_stub.chmod(0o755)
    env = os.environ.copy()
    env.update({
        "PATH": f"{bin_dir}:/usr/bin:/bin",
        "BOX_X11VNC_TEST_CMDLINE": str(cmdline),
        "BOX_X11VNC_CAPTURE": str(capture),
    })

    subprocess.run([
        str(shim), "-display", ":2", "-localhost", "-nopw", "-shared",
        "-forever", "-noxdamage", "-rfbport", "5902", "-quiet",
    ], check=True, capture_output=True, text=True, env=env)

    assert json.loads(capture.read_text()) == [
        "-display", ":2",
        "-rfbport", "5902",
        "-PasswordFile", str(password_file),
        "-localhost", "no",
        "-SecurityTypes", "VncAuth",
        "-AlwaysShared=1",
    ]


def test_box_x11vnc_fails_when_xtigervnc_password_file_is_missing(
        tmp_path):
    package_dir = Path(__file__).resolve().parent.parent
    shim = package_dir / "box" / "box-x11vnc"
    cmdline = tmp_path / "xtigervnc.cmdline"
    cmdline.write_bytes(
        b"\0".join((b"/usr/bin/Xtigervnc", b":0", b"-rfbport", b"5901")))
    result = subprocess.run([
        str(shim), "-display", ":2", "-rfbport", "5902",
    ], check=False, capture_output=True, text=True, env={
        **os.environ,
        "BOX_X11VNC_TEST_CMDLINE": str(cmdline),
    })
    assert result.returncode != 0
    assert "could not find a readable Xtigervnc -PasswordFile" \
        in result.stderr


def test_per_window_exec_daemon_shim_parses_arguments_and_sets_environment(
        tmp_path):
    package_dir = Path(__file__).resolve().parent.parent
    pack = tmp_path / "pack"
    shim_dir = pack / "opt-sand" / "exec-daemon"
    shim_dir.mkdir(parents=True)
    shim_path = shim_dir / "exec-daemon"
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    source = (package_dir / "box" / "exec-daemon").read_text()
    source = source.replace(
        "realpath -e /workspace", f"realpath -e '{workspace}'")
    shim_path.write_text(source)
    shim_path.chmod(0o755)
    daemon_entry = pack / "opt-sand" / "box-exec-daemon" / "main.cjs"
    daemon_entry.parent.mkdir(parents=True)
    daemon_entry.write_text("void 0;\n")
    node = pack / "node"
    node.write_text(
        "#!/usr/bin/env python3\n"
        "import json, os, sys\n"
        "print(json.dumps({"
        "'argv': sys.argv[1:], "
        "'port': os.environ.get('SAND_BOX_EXEC_DAEMON_PORT'), "
        "'token': os.environ.get('SAND_BOX_EXEC_DAEMON_AUTH_TOKEN'), "
        "'workspace': os.environ.get('SAND_BOX_WORKSPACE_ROOT'), "
        "'terminals': os.environ.get('SAND_BOX_TERMINALS_DIRECTORY'), "
        "'display': os.environ.get('DISPLAY')}))\n")
    node.chmod(0o755)
    data_root = tmp_path / "sand-data"
    result = subprocess.run([
        str(shim_path), "serve", "--port", "14002",
        "--pty-websocket-port", "13602", "--auth-token", "local",
        "--rg-path", "/exec-daemon/rg", "--computer-use-enabled",
        "--mcp-meta-tool-enabled", "--origin-cli-enabled",
        "--orbitd-proxy-socket", "/run/orbitd-proxy/rpc.sock",
        "--unexpected-option", "sensitive-value",
    ], check=True, capture_output=True, text=True, env={
        **os.environ,
        "DISPLAY": ":88",
        "SAND_DATA_ROOT": str(data_root),
    })
    payload = json.loads(result.stdout)
    assert payload["port"] == "14002"
    assert payload["token"] == "local"
    assert payload["workspace"] == str(workspace.resolve())
    assert payload["terminals"] == str(data_root / "box-terminals-14002")
    assert payload["display"] == ":88"
    assert payload["argv"][-1] == str(daemon_entry)
    assert result.stderr.strip() == "--unexpected-option"
    assert "sensitive-value" not in result.stderr


def test_per_window_exec_daemon_shim_resolves_missing_home_and_user(tmp_path):
    package_dir = Path(__file__).resolve().parent.parent
    pack = tmp_path / "pack"
    shim_dir = pack / "opt-sand" / "exec-daemon"
    shim_dir.mkdir(parents=True)
    shim_path = shim_dir / "exec-daemon"
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    source = (package_dir / "box" / "exec-daemon").read_text()
    source = source.replace(
        "realpath -e /workspace", f"realpath -e '{workspace}'")
    shim_path.write_text(source)
    shim_path.chmod(0o755)

    daemon_entry = pack / "opt-sand" / "box-exec-daemon" / "main.cjs"
    daemon_entry.parent.mkdir(parents=True)
    daemon_entry.write_text("void 0;\n")
    node = pack / "node"
    node.write_text(
        "#!/usr/bin/env python3\n"
        "import json, os\n"
        "print(json.dumps({"
        "'home': os.environ.get('HOME'), "
        "'user': os.environ.get('USER'), "
        "'terminals': os.environ.get('SAND_BOX_TERMINALS_DIRECTORY'), "
        "'data_root': os.environ.get('SAND_DATA_ROOT')}))\n")
    node.chmod(0o755)

    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    mkdir_capture = tmp_path / "mkdir-args.json"
    fake_mkdir = fake_bin / "mkdir"
    fake_mkdir.write_text(
        "#!/usr/bin/env python3\n"
        "import json, os, sys\n"
        "with open(os.environ['MKDIR_CAPTURE'], 'w') as output:\n"
        "    json.dump(sys.argv[1:], output)\n")
    fake_mkdir.chmod(0o755)
    passwd_home = subprocess.run(
        ["getent", "passwd", str(os.getuid())],
        check=True, capture_output=True, text=True,
    ).stdout.split(":")[5]
    passwd_user = subprocess.run(
        ["id", "-un"], check=True, capture_output=True, text=True,
    ).stdout.strip()
    assert passwd_home

    result = subprocess.run([
        "/usr/bin/env", "-i",
        f"PATH={fake_bin}:/usr/bin:/bin",
        f"MKDIR_CAPTURE={mkdir_capture}",
        str(shim_path), "serve", "--port", "14002",
        "--pty-websocket-port", "13602", "--auth-token", "local",
        "--rg-path", "/exec-daemon/rg",
    ], capture_output=True, text=True, check=False)

    assert result.returncode == 0, result.stderr
    assert "HOME: unbound variable" not in result.stderr
    payload = json.loads(result.stdout)
    assert payload["home"] == passwd_home
    assert payload["user"] == passwd_user
    assert payload["data_root"] is None
    assert payload["terminals"] == (
        Path(passwd_home) / ".sand" / "box-terminals-14002").as_posix()
    assert json.loads(mkdir_capture.read_text()) == [
        "-p", "--", payload["terminals"]]


@pytest.mark.parametrize("argv", [
    [],
    ["serve", "--port", "14002"],
    ["serve", "--auth-token", "local"],
    ["run", "--port", "14002", "--auth-token", "local"],
])
def test_per_window_exec_daemon_shim_requires_serve_and_required_flags(
        tmp_path, argv):
    package_dir = Path(__file__).resolve().parent.parent
    shim = package_dir / "box" / "exec-daemon"
    result = subprocess.run(
        [str(shim), *argv], capture_output=True, text=True, check=False)
    assert result.returncode == 2


# ── EnsureSandBox against a fake DevBox API ─────────────────────────

class _FakeDevBoxApi:
    """Stands in for DevBoxApi inside _ensure_box; records calls."""

    def __init__(self, ready_after=1, sessions=None):
        self.token = "tok"
        self.sessions = sessions or {}
        self.created = []
        self.deleted = []
        self.messages = []
        self.calls = []
        self.keep_alive_calls = []
        self.keep_alive_error = None
        self.asset_token_error = None
        self.asset_token_response = {
            "token": "session-asset-token",
            "base_url": "https://app.devinai.net/internal/harness-llm/v1",
            "expires_at": 1234567890,
        }
        self.asset_token_json_body = None
        self.ready_after = ready_after
        self._ensure_calls = 0

    def current_membership(self):
        return {"user_id": "user-devbox", "org_id": "org-devbox"}

    def create_session(self, org_id, prompt, **kw):
        sid = f"devin-fake-{len(self.created)}"
        self.created.append((org_id, prompt, kw))
        self.sessions[sid] = {"status": "running"}
        return {"session_id": sid}

    def get_session(self, org_id, sid):
        return self.sessions[sid]

    def delete_session(self, org_id, sid):
        self.deleted.append(sid)

    def send_message(self, org_id, sid, message):
        self.messages.append((org_id, sid, message))
        return {}

    def list_secrets(self, org_id):
        return []

    def call(self, method, path, **kw):
        self.calls.append((method, path))
        if path.endswith("/keep-alive"):
            self.keep_alive_calls.append((method, path, kw.get("json_body")))
            if self.keep_alive_error:
                raise self.keep_alive_error
            return {"session_keep_alive": True}
        if path == "/api/grokbot/sand/share-credential":
            return {
                "credential": "share-credential-test",
                "authId": "u",
                "expiresAtMs": int(time.time() * 1000)
                + 365 * 24 * 60 * 60 * 1000,
            }
        if path == "/api/grokbot/sand/asset-token":
            self.asset_token_json_body = kw.get("json_body")
            if self.asset_token_error:
                raise self.asset_token_error
            return self.asset_token_response
        if path.startswith("/api/preview-link/"):
            self._ensure_calls += 1
            if self._ensure_calls < self.ready_after:
                raise RuntimeError("not ready")
            return {"url": "https://p-1340.example.dev/",
                    "token": "cap-x"}
        raise RuntimeError(path)


class _FakeHTTPResp:
    status = 200

    def read(self):
        return b'{"state":"awaiting"}'

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _patch_gateway(monkeypatch, backend, api):
    monkeypatch.setattr(backend, "api_for", lambda ctx: api)
    monkeypatch.setattr(backend, "_identity",
                        lambda api: {"user_id": "u",
                                     "org_id": "org-devbox"})
    monkeypatch.setattr(urllib.request, "urlopen",
                        lambda *a, **k: _FakeHTTPResp())
    monkeypatch.setattr(backend, "_provision_relay",
                        lambda *a, **k: (0, {}))
    monkeypatch.setattr(
        "devbox_bot.desktop.PROVISION_WAIT_S", 0.01)
    return api


def test_gateway_coords_include_relay_vnc_urls_and_serialize(
        tmp_path, monkeypatch):
    backend = DesktopBackend(
        port=0, api_key="k", state_path=tmp_path / "s.json")
    api = _patch_gateway(monkeypatch, backend, _FakeDevBoxApi())
    coords = backend._gateway_coords(
        api, "devin-vnc-test", {"gateway_token": "gw-token"})
    assert coords is not None
    novnc = "https://p-1340.example.dev/__devbox/novnc"
    wake = "resume_lower_s=900&resume_upper_s=18000"
    expected_url = (
        f"{novnc}/vnc.html?network_token=cap-x&{wake}&path="
        "websockify%3Fnetwork_token%3Dcap-x%26resume_lower_s%3D900"
        "%26resume_upper_s%3D18000")
    assert coords["vnc_url"] == expected_url
    assert coords["fork_vnc_base_url"] == novnc

    monkeypatch.setattr(backend, "_ensure_box", lambda _ctx: coords)
    router = backend.router()
    for method in ("EnsureSandBox", "EnsureSandBoxWindow"):
        status, content_type, body = router.dispatch(
            "aiserver.v1.GrokBotService", method,
            "application/json", b"{}")
        assert status == 200
        assert content_type == "application/json"
        response = json.loads(body)
        assert response["vnc_url"] == expected_url
        assert response["fork_vnc_base_url"] == novnc


def test_ensure_sandbox_creates_session(tmp_path, monkeypatch):
    backend = DesktopBackend(port=0, api_key="k",
                             state_path=tmp_path / "s.json")
    api = _patch_gateway(monkeypatch, backend, _FakeDevBoxApi())
    resp = backend._ensure_box({"authorization": "Bearer x"})
    assert api.created, "no session created"
    kw = api.created[0][2]
    assert kw["tags"] == ["grok-bot-box"]
    keys = [s["key"] for s in kw["session_secrets"]]
    assert "GROKBOT_GATEWAY_TOKEN" in keys
    assert "GROKBOT_SHARE_CREDENTIAL" in keys
    assert next(item["value"] for item in kw["session_secrets"]
                if item["key"] == "GROKBOT_SHARE_CREDENTIAL") == \
        "share-credential-test"
    assert "share-credential-test" not in (
        tmp_path / "s.json").read_text()
    assert resp["gateway_url"].startswith("https://")
    assert resp["network_token"] == "cap-x"
    assert resp["gateway_token"]
    assert resp["pod_id"] == "devin-fake-0"
    assert api.keep_alive_calls == [(
        "POST", "/api/sessions/devin-fake-0/keep-alive",
        {"keep_alive": True, "duration_minutes": 1440})]
    saved = json.loads((tmp_path / "s.json").read_text())
    box = next(iter(saved["box"].values()))
    assert abs(
        box["keep_alive_until_ms"] - int(time.time() * 1000)
        - 24 * 60 * 60 * 1000) < 1000


def test_share_credential_must_match_box_identity(tmp_path):
    backend = DesktopBackend(port=0, api_key="k",
                             state_path=tmp_path / "s.json")
    api = _FakeDevBoxApi()
    api.call = lambda *_args, **_kwargs: {
        "credential": "share-credential-test",
        "authId": "different-user",
        "expiresAtMs": int(time.time() * 1000) + 60_000,
    }
    with pytest.raises(ConnectError, match="invalid sharing credential"):
        backend._mint_share_credential(api, "u")


def test_ensure_sandbox_reuses_live_session(tmp_path, monkeypatch):
    backend = DesktopBackend(port=0, api_key="k",
                             state_path=tmp_path / "s.json")
    api = _FakeDevBoxApi(sessions={"devin-old": {"status": "running"}})
    _patch_gateway(monkeypatch, backend, api)
    backend._save_box_state({"user_id": "u", "org_id": "org-devbox"}, {
        "session_id": "devin-old", "gateway_token": "gt",
        "share_credential_expires_at_ms": int(time.time() * 1000)
        + 365 * 24 * 60 * 60 * 1000})
    resp = backend._ensure_box({"authorization": "Bearer x"})
    assert not api.created, "should reuse existing session"
    assert resp["pod_id"] == "devin-old"
    assert len(api.keep_alive_calls) == 1
    assert not api.messages


def test_ensure_sandbox_same_identity_rotated_bearer(tmp_path, monkeypatch):
    """Two different bearer tokens (e.g. post-refresh) for the same
    identity must reuse the box — no second create_session."""
    backend = DesktopBackend(port=0, api_key="k",
                             state_path=tmp_path / "s.json")
    api = _patch_gateway(monkeypatch, backend, _FakeDevBoxApi())
    resp1 = backend._ensure_box({"authorization": "Bearer tok-1"})
    assert api.created, "first call should create the box"
    resp2 = backend._ensure_box({"authorization": "Bearer tok-2"})
    assert len(api.created) == 1, "rotated bearer must not re-create"
    assert resp2["pod_id"] == resp1["pod_id"]
    assert len(api.keep_alive_calls) == 1


def test_ensure_renews_keep_alive_inside_12_hour_window(
        tmp_path, monkeypatch):
    backend = DesktopBackend(port=0, api_key="k",
                             state_path=tmp_path / "s.json")
    api = _FakeDevBoxApi(sessions={"devin-old": {"status": "running"}})
    _patch_gateway(monkeypatch, backend, api)
    old_expiry = int(time.time() * 1000) + 11 * 60 * 60 * 1000
    ident = {"user_id": "u", "org_id": "org-devbox"}
    backend._save_box_state(ident, {
        "session_id": "devin-old", "gateway_token": "gt",
        "share_credential_expires_at_ms": int(time.time() * 1000)
        + 365 * 24 * 60 * 60 * 1000,
        "keep_alive_until_ms": old_expiry,
    })

    backend._ensure_box({"authorization": "Bearer x"})

    assert len(api.keep_alive_calls) == 1
    saved = json.loads((tmp_path / "s.json").read_text())
    box = next(iter(saved["box"].values()))
    assert box["keep_alive_until_ms"] > old_expiry


def test_ensure_keep_alive_failure_does_not_fail_ensure(
        tmp_path, monkeypatch, caplog):
    backend = DesktopBackend(port=0, api_key="k",
                             state_path=tmp_path / "s.json")
    api = _FakeDevBoxApi()
    api.keep_alive_error = ApiError(503, "response body must not be logged")
    _patch_gateway(monkeypatch, backend, api)

    response = backend._ensure_box({"authorization": "Bearer x"})

    assert response["pod_id"] == "devin-fake-0"
    assert len(api.keep_alive_calls) == 1
    assert "status=503" in caplog.text
    assert "response body must not be logged" not in caplog.text


@pytest.mark.parametrize("status", ["suspended", "stopped"])
def test_ensure_wakes_suspended_or_stopped_box_before_gateway_wait(
        tmp_path, monkeypatch, status):
    backend = DesktopBackend(port=0, api_key="k",
                             state_path=tmp_path / "s.json")
    api = _FakeDevBoxApi(sessions={"devin-old": {"status": status}})
    _patch_gateway(monkeypatch, backend, api)
    ident = {"user_id": "u", "org_id": "org-devbox"}
    backend._save_box_state(ident, {
        "session_id": "devin-old", "gateway_token": "gt",
        "share_credential_expires_at_ms": int(time.time() * 1000)
        + 365 * 24 * 60 * 60 * 1000,
        "keep_alive_until_ms": int(time.time() * 1000)
        + 24 * 60 * 60 * 1000,
    })
    awaited = []
    monkeypatch.setattr(backend, "_gateway_coords", lambda *_: None)
    monkeypatch.setattr(backend, "_provision_listener", lambda *_: "provision")

    def await_gateway(_api, session_id, _box):
        awaited.append(session_id)
        return {"pod_id": session_id, "gateway_url": "http://gateway"}

    monkeypatch.setattr(backend, "_await_gateway", await_gateway)

    response = backend._ensure_box({"authorization": "Bearer x"})

    assert api.messages == [("org-devbox", "devin-old", "Continue.")]
    assert awaited == ["devin-old"]
    assert response["pod_id"] == "devin-old"


def test_ensure_sandbox_unavailable(tmp_path, monkeypatch):
    backend = DesktopBackend(port=0, api_key="k",
                             state_path=tmp_path / "s.json")
    _patch_gateway(monkeypatch, backend,
                   _FakeDevBoxApi(ready_after=10 ** 9))
    monkeypatch.setattr(
        "devbox_bot.desktop.BOX_DEADLINE_S", 0.01)
    import time as _t
    monkeypatch.setattr(_t, "sleep", lambda s: None)
    with pytest.raises(ConnectError) as ei:
        backend._ensure_box({"authorization": "Bearer x"})
    assert ei.value.code == "unavailable"


def test_provision_relay_mints_port_7813(tmp_path, monkeypatch):
    backend = DesktopBackend(port=0, api_key="k",
                             state_path=tmp_path / "s.json")
    api = _FakeDevBoxApi()
    monkeypatch.setattr(urllib.request, "urlopen",
                        lambda *a, **k: _FakeHTTPResp())

    status, body = backend._provision_relay(api, "devin-test", "/health")

    assert status == 200
    assert body == {"state": "awaiting"}
    assert "local_port=7813" in api.calls[0][1]


def test_recreate_deletes_and_reallocates(tmp_path, monkeypatch):
    backend = DesktopBackend(port=0, api_key="k",
                             state_path=tmp_path / "s.json")
    api = _FakeDevBoxApi(sessions={"devin-old": {"status": "running"}})
    _patch_gateway(monkeypatch, backend, api)
    backend._save_box_state({"user_id": "u", "org_id": "org-devbox"}, {
        "session_id": "devin-old", "gateway_token": "gt"})
    resp = backend._recreate_box({"authorization": "Bearer x"})
    assert "devin-old" in api.deleted
    assert api.created
    assert resp["started"] is True


# ── box.py credential endpoint ──────────────────────────────────────

def test_box_credential(tmp_path, monkeypatch):
    monkeypatch.setenv("GROKBOT_INFERENCE_CREDENTIAL", "cred-1")
    backend = BoxBackend()
    handler = make_handler(backend.router(), backend.extra_routes())
    server, port = _serve(handler)
    try:
        status, body, _ = _req(
            port, "POST", "/sand-box/inference-credential",
            json.dumps({"credential": "cred-1"}).encode(),
            {"content-type": "application/json"})
        assert status == 200
        assert "accessToken" in json.loads(body)
        status, _, _ = _req(
            port, "POST", "/sand-box/inference-credential",
            json.dumps({"credential": "wrong"}).encode(),
            {"content-type": "application/json"})
        assert status == 401
    finally:
        server.shutdown()


def test_box_sharing_routes_forward_with_share_credential(monkeypatch):
    monkeypatch.setenv("GROKBOT_SHARE_CREDENTIAL", "share-token")
    monkeypatch.setenv("DEVBOX_WEBAPP_ORIGIN", "https://webapp.example")
    backend = BoxBackend()
    captured = {}

    class _Response:
        status = 200

        def __init__(self):
            self.headers = {}

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self, _limit=-1):
            payload = json.dumps({
                "rooms": [], "pendingJoinRequests": []}).encode()
            return payload if _limit < 0 else payload[:_limit]

    def _urlopen(request, timeout):
        captured["url"] = request.full_url
        captured["authorization"] = request.get_header("Authorization")
        captured["body"] = json.loads(request.data)
        captured["timeout"] = timeout
        return _Response()

    monkeypatch.setattr(urllib.request, "urlopen", _urlopen)
    server, port = _serve(make_handler(
        backend.router(), backend.extra_routes()))

    def post_bearer(token):
        connection = http.client.HTTPConnection(
            "127.0.0.1", port, timeout=10)
        connection.request(
            "POST", "/sand/share-state",
            body=json.dumps({"cursor": "state"}).encode(),
            headers={
                "content-type": "application/json",
                "authorization": f"Bearer {token}",
            },
        )
        response = connection.getresponse()
        result = response.status, response.read()
        connection.close()
        return result

    try:
        access_token = backend._issue_access_token()["accessToken"]
        status, body = post_bearer(access_token)
        assert status == 200
        assert json.loads(body) == {
            "rooms": [], "pendingJoinRequests": []}
        assert captured["url"] == (
            "https://webapp.example/api/grokbot/sand/share-state")
        assert captured["authorization"] == "Bearer share-token"
        assert captured["body"] == {"cursor": "state"}
        assert captured["timeout"] == 15

        status, _ = post_bearer("invalid")
        assert status == 401
    finally:
        server.shutdown()


def test_box_sharing_relay_logs_only_path_and_status(monkeypatch, caplog):
    monkeypatch.setenv("GROKBOT_SHARE_CREDENTIAL", "share-token-sentinel")
    monkeypatch.setenv("DEVBOX_WEBAPP_ORIGIN", "https://webapp.example")
    backend = BoxBackend()

    class _Response:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self, _limit=-1):
            payload = json.dumps({
                "room": {"roomId": "response-body-sentinel"},
                "shareUrl": "response-url-sentinel",
            }).encode()
            return payload if _limit < 0 else payload[:_limit]

    monkeypatch.setattr(
        urllib.request, "urlopen", lambda *_args, **_kwargs: _Response())
    server, port = _serve(make_handler(
        backend.router(), backend.extra_routes()))
    access_token = backend._issue_access_token()["accessToken"]
    request_body = json.dumps({
        "agentId": "request-body-sentinel",
    }).encode()
    caplog.set_level(logging.INFO, logger="devbox_bot.box")

    try:
        connection = http.client.HTTPConnection(
            "127.0.0.1", port, timeout=10)
        connection.request(
            "POST",
            "/sand/share-rooms/from-agent?query-secret-sentinel=value",
            body=request_body,
            headers={
                "content-type": "application/json",
                "authorization": f"Bearer {access_token}",
            },
        )
        response = connection.getresponse()
        response_status = response.status
        response_body = response.read()
        connection.close()

        assert response_status == 200
        assert b"response-body-sentinel" in response_body
        log_lines = [
            record.getMessage() for record in caplog.records
            if record.name == "devbox_bot.box"
        ]
        assert log_lines == [
            "grokbot.sharing.relay path=/sand/share-rooms/from-agent status=200"
        ]
        logged = "\n".join(log_lines)
        for sensitive in (
                "authorization", "Bearer ", access_token,
                "share-token-sentinel", "request-body-sentinel",
                "response-body-sentinel", "response-url-sentinel",
                "query-secret-sentinel", "value"):
            assert sensitive not in logged
    finally:
        server.shutdown()


# ── shared dashboard handlers (desktop.py + box.py) ────────────────

def _box_server(monkeypatch, **env):
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    backend = BoxBackend()
    handler = make_handler(backend.router(), backend.extra_routes())
    return _serve(handler)


def test_box_dashboard_handlers(monkeypatch):
    monkeypatch.setenv("GROKBOT_INFERENCE_CREDENTIAL", "cred-1")
    monkeypatch.setenv("GROKBOT_USER_JSON", json.dumps({
        "user_id": "u-1", "org_id": "org-1", "name": "Ada Lovelace"}))
    server, port = _box_server(monkeypatch)
    try:
        status, body, _ = _req(
            port, "POST", "/sand-box/inference-credential",
            json.dumps({"credential": "cred-1"}).encode(),
            {"content-type": "application/json"})
        token = json.loads(body)["accessToken"]
        status, body, _ = _req(
            port, "POST", "/aiserver.v1.DashboardService/GetMe",
            b"", {"content-type": "application/proto",
                  "authorization": f"Bearer {token}"})
        assert status == 200
        # decode via codec to confirm authId propagation
        msg = codec_mod.codec().decode(
            "aiserver.v1.GetMeResponse", body)
        assert msg.auth_id == "u-1"
        for svc, meth in (
                ("DashboardService", "GetUserPrivacyMode"),
                ("DashboardService", "GetTeams"),
                ("DashboardService", "GetManagedSkills"),
                ("DashboardService", "GetEffectiveUserPlugins"),
                ("DashboardService", "GetAvailableMcpServers"),
                ("GrokBotService", "GetSandBoxRunState"),
                ("AnalyticsService", "BootstrapStatsig"),
                ("AiService", "AvailableModels")):
            status, _, _ = _req(
                port, "POST", f"/aiserver.v1.{svc}/{meth}", b"",
                {"content-type": "application/proto",
                 "authorization": f"Bearer {token}"})
            assert status == 200, (svc, meth, status)
    finally:
        server.shutdown()


def test_box_dashboard_requires_bearer(monkeypatch):
    monkeypatch.setenv("GROKBOT_INFERENCE_CREDENTIAL", "cred-1")
    server, port = _box_server(monkeypatch)
    try:
        status, _, _ = _req(
            port, "POST", "/aiserver.v1.DashboardService/GetMe", b"",
            {"content-type": "application/proto"})
        assert status == 401
    finally:
        server.shutdown()


def _free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _start_provision_listener(tmp_path, *, idle="900"):
    port = _free_port()
    home = tmp_path / "grok-bot-box"
    env = dict(os.environ)
    env.update({
        "GROKBOT_HOME": str(home),
        "GROKBOT_PROVISION_PORT": str(port),
        "GROKBOT_PROVISION_IDLE_S": idle,
    })
    script = (Path(__file__).resolve().parent.parent
              / "box" / "provision.py")
    proc = subprocess.Popen(
        [sys.executable, str(script)], env=env,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return proc, port, home


def _load_provision_module():
    path = (Path(__file__).resolve().parent.parent
            / "box" / "provision.py")
    spec = importlib.util.spec_from_file_location(
        "grok_bot_box_provision_test", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _prepare_provision_module(module, tmp_path):
    home = tmp_path / "grok-bot-box"
    home.mkdir()
    module.GB_HOME = home
    module.CREDS = home / "creds.env"
    module.CREDS.write_text("GROKBOT_GATEWAY_TOKEN='test-token'\n")
    module.LOG_DIR = home / "logs"
    module.START_LOG = module.LOG_DIR / "start-box.log"
    module.PIDFILE = home / "provision.pid"
    return home


class _FakeStartBoxProcess:
    def __init__(self, pid, exit_code):
        self.pid = pid
        self.exit_code = exit_code
        self.wait_calls = 0

    def poll(self):
        return self.exit_code

    def wait(self):
        self.wait_calls += 1
        return self.exit_code


def _get_json(port, path):
    with urllib.request.urlopen(
            f"http://127.0.0.1:{port}{path}", timeout=5) as response:
        return response.status, json.loads(response.read())


def _post_json(port, path, body):
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}{path}",
        data=json.dumps(body).encode(),
        headers={"content-type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=5) as response:
            return response.status, json.loads(response.read())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read())


def _wait_listener(port, proc, timeout=10):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline and proc.poll() is None:
        try:
            status, body = _get_json(port, "/health")
            if status == 200:
                return body
        except OSError:
            time.sleep(0.1)
    raise AssertionError("provision listener did not become ready")


def test_start_box_uses_packaged_node_runtime():
    script = (Path(__file__).resolve().parent.parent / "box" /
              "start-box.sh").read_text()
    assert 'NODE_BIN="$PACK_DIR/node"' in script
    assert ('exec setsid "$NODE_BIN" --disable-warning=ExperimentalWarning '
            '"$HOST_MAIN"') in script
    assert 'exec setsid node "$HOST_MAIN"' not in script


def test_start_box_retries_runtime_downloads_over_http1():
    script = (Path(__file__).resolve().parent.parent / "box" /
              "start-box.sh").read_text()
    assert script.count("--http1.1") == 5
    assert script.count("--retry-all-errors") == 4


def test_start_box_bounds_runtime_downloads_and_retries_size_head():
    script = (Path(__file__).resolve().parent.parent / "box" /
              "start-box.sh").read_text()
    lines = script.splitlines()
    curl_commands = []
    i = 0
    while i < len(lines):
        line = lines[i]
        if "curl" in line and not line.lstrip().startswith("#"):
            command = line[line.index("curl"):].strip()
            while lines[i].rstrip().endswith("\\"):
                command = (command.rstrip()[:-1].rstrip() + " "
                           + lines[i + 1].strip())
                i += 1
            curl_commands.append(command)
        i += 1

    downloads = [
        command for command in curl_commands
        if "RUNTIME_URL" in command
        and re.search(r"(?:^|\s)-o(?:\s|$)", command)
        and "-o /dev/null" not in command
    ]
    assert len(downloads) == 3
    assert all("--speed-limit" in command and "--speed-time" in command
               for command in downloads)

    head = next(command for command in curl_commands
                if "-fsSIL" in command and "RUNTIME_URL" in command)
    assert "--max-time 20" in head
    assert "--retry 3" in head
    assert "--retry-all-errors" in head
    assert "--retry-delay 2" in head
    assert "|| true" in head
    assert """trap 'echo "start-box: failed rc=$? line=$LINENO" >&2' ERR""" \
        in script

    fallback = next(command for command in downloads
                    if '"$PACK"' in command)
    assert "--speed-limit 5120" in fallback
    assert "--speed-time 30" in fallback
    assert "--max-time 600" not in fallback
    assert "--retry 5" in fallback
    assert "--retry-all-errors" in fallback
    assert "--retry-delay 2" in fallback
    assert "-C -" in fallback
    assert script.index('rm -f "$PACK"') > script.index("-C -")

    checksum = next(command for command in downloads
                    if 'RUNTIME_URL.sha256' in command)
    assert "--speed-limit 1" in checksum
    assert "--speed-time 20" in checksum


def test_start_box_size_probe_is_nonfatal_and_err_trap_is_installed():
    script = (Path(__file__).resolve().parent.parent / "box" /
              "start-box.sh").read_text()
    assert """trap 'echo "start-box: failed rc=$? line=$LINENO" >&2' ERR""" \
        in script
    assert "--range 0-0" in script
    assert 'tolower($1) == "content-range:"' in script
    assert 'if [ -z "$SIZE" ]; then' in script
    assert "-fsSIL" in script
    assert '|| true)"' in script


def test_start_box_resumes_only_chunks_for_the_verified_pack():
    script = (Path(__file__).resolve().parent.parent / "box" /
              "start-box.sh").read_text()
    assert 'PART_HASH_FILE="$GB_HOME/runtime-download.sha256"' in script
    assert '"$RUNTIME_URL.sha256"' in script
    assert 'if [ ! -f "$PART_HASH_FILE" ]' in script
    assert '"$EXPECTED_PACK_SHA"' in script
    assert 'rm -f "$GB_HOME"/.part-*' not in script
    assert 'cat "$GB_HOME"/.part-0 "$GB_HOME"/.part-1' in script


def test_start_box_does_not_pass_its_lock_to_long_lived_services():
    script = (Path(__file__).resolve().parent.parent / "box" /
              "start-box.sh").read_text()
    assert script.count("exec 9>&-") == 5
    assert ('export PYTHONPATH="$PACK_DIR"\n    exec 9>&-\n'
            '    exec setsid "$PY" -m devbox_bot.box --port 7812') in script
    assert ('export SAND_BOX_TERMINALS_DIRECTORY="$SAND_DATA_ROOT/box-terminals"\n'
            '    exec 9>&-\n    exec setsid "$NODE_BIN"') in script
    assert ('export SAND_TREE_SITTER_NODE_DEPS="$PACK_DIR/node_modules"\n'
            '    exec 9>&-\n    exec setsid "$NODE_BIN"') in script
    assert ('exec 9>&-\n    exec setsid "$NODE_BIN" "$WINDOW_ROUTER_SCRIPT" '
            '1339 1337 14000') in script
    assert ('exec 9>&-\n    export GROKBOT_PROVISION_IDLE_S='
            in script)


def test_start_box_stages_verified_runtime_before_replacing_live_pack():
    script = (Path(__file__).resolve().parent.parent / "box" /
              "start-box.sh").read_text()
    assert 'AVAILABLE_BYTES="$(df -PB1 "$GB_HOME"' in script
    assert "REQUIRED_BYTES=$((SIZE * 6 + 67108864))" in script
    checksum = script.index("sha256sum -c \"$PACK_NAME.sha256\"")
    extraction = script.index('tar -xzf "$PACK" -C "$RUNTIME_STAGE"')
    daemon_check = script.index(
        'if [ ! -f "$RUNTIME_STAGE/opt-sand/box-exec-daemon/main.cjs" ]')
    shim_check = script.index(
        'BOX_EXEC_DAEMON_SHIM_STAGE="$RUNTIME_STAGE/opt-sand/exec-daemon/exec-daemon"')
    shim_syntax = script.index('bash -n "$BOX_EXEC_DAEMON_SHIM_STAGE"')
    script_check = script.index(
        "for required_script in start-window stop-window box-x11vnc")
    assert script.index("sand-window-router.mjs", script_check) < \
        script.index("do\n", script_check)
    router_start = script.index('exec setsid "$NODE_BIN" '
                               '"$WINDOW_ROUTER_SCRIPT" 1339 1337 14000')
    packaged_script_check = script.index(
        'PACKAGED_START_BOX_STAGE="$RUNTIME_STAGE/opt-sand/grok-bot-box/start-box.sh"')
    packaged_script_syntax = script.index(
        'bash -n "$PACKAGED_START_BOX_STAGE"')
    marker = script.index('printf \'%s\\n\' "$PACK_SHA" > "$RUNTIME_STAGE/.installed"')
    old_runtime = script.index('mv "$PACK_DIR" "$OLD_RUNTIME_DIR"')
    activation = script.index('mv "$RUNTIME_STAGE" "$PACK_DIR"')
    cleanup = script.index('rm -rf -- "$OLD_RUNTIME_DIR"')
    update = script.index('mv -f -- "$START_BOX_STAGE" "$START_BOX_FILE"')
    assert (checksum < extraction < daemon_check < shim_check < shim_syntax
            < script_check
            < packaged_script_check < packaged_script_syntax < marker)
    assert marker < old_runtime < activation < cleanup
    assert cleanup < update
    assert router_start > script.index("if ! box_exec_daemon_listening; then")
    assert "failed to restore previous runtime" in script
    assert 'START_BOX_STAGE="$GB_HOME/.start-box-stage.$BASHPID"' in script
    assert 'for box_script in "$BOX_SCRIPTS_DIR"/*' in script
    assert 'ln -sfn -- "$box_script" "$script_destination"' in script
    assert ('if [ ! -x /usr/local/bin/box-xvfb ] && '
            '[ -x /usr/bin/Xvfb ]; then') in script
    assert 'ln -sfn -- /usr/bin/Xvfb /usr/local/bin/box-xvfb' in script
    assert 'BOX_WORKSPACE_ROOT="$SAND_DATA_ROOT/box-workspace"' in script
    assert 'ln -sfn "$BOX_WORKSPACE_ROOT" /workspace' in script
    assert 'preserving real /exec-daemon/exec-daemon' in script
    assert 'mkdir -m 0755 -- "$EXEC_DAEMON_DIRECTORY"' in script
    assert ('sudo -n install -d -m 0755 -- "$EXEC_DAEMON_DIRECTORY"'
            in script)
    assert '[ ! -x "$EXEC_DAEMON_DIRECTORY" ]' in script
    assert 'sudo -n chmod 0755 -- "$EXEC_DAEMON_DIRECTORY"' in script
    assert 'start-box: /exec-daemon is not accessible' in script
    assert 'sudo -n install -d -m 0755 /home/box' in script
    # per-window box-bounded-log resolves node via /exec-daemon/node
    assert 'NODE_BINARY="$PACK_DIR/node"' in script
    assert 'preserving real /exec-daemon/node' in script
    assert 'ln -sfn -- "$NODE_BINARY" "$EXEC_DAEMON_DIRECTORY/node"' in script
    assert 'sudo -n ln -sfn -- "$NODE_BINARY" "$EXEC_DAEMON_DIRECTORY/node"' \
        in script
    # start-desktop.sh truncates /usr/local/bin/box-chrome via `cat >` as
    # this user, so a writable placeholder must exist beforehand
    assert '[ ! -w /usr/local/bin/box-chrome ]' in script
    assert ('sudo -n install -m 0755 -o "$(id -u)" -g "$(id -g)" /dev/null \\'
            '\n      /usr/local/bin/box-chrome' in script)
    # per-window WM/compositor/root painter, best-effort install
    assert 'command -v xfwm4 >/dev/null 2>&1' in script
    assert 'command -v picom >/dev/null 2>&1' in script
    assert 'command -v hsetroot >/dev/null 2>&1' in script
    assert ('sudo -n apt-get install -y --no-install-recommends \\'
            '\n    xfwm4 picom hsetroot' in script)
    assert 'start-box: desktop packages unavailable' in script


def test_start_box_exec_daemon_directory_explicit_mode(tmp_path):
    """Execute the extracted /exec-daemon fragment under umask 077 and
    assert both creation and repair paths end at mode 0755."""
    package_dir = Path(__file__).resolve().parent.parent
    script = (package_dir / "box" / "start-box.sh").read_text()
    fragment_start = script.index('EXEC_DAEMON_DIRECTORY="/exec-daemon"')
    fragment_end = script.index(
        'if [ -e "$EXEC_DAEMON_DIRECTORY/exec-daemon" ]')
    fragment = script[fragment_start:fragment_end]

    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    (fake_bin / "sudo").write_text("#!/bin/sh\nshift\nexec \"$@\"\n")
    (fake_bin / "sudo").chmod(0o755)

    def run_fragment(directory):
        body = fragment.replace(
            'EXEC_DAEMON_DIRECTORY="/exec-daemon"',
            f'EXEC_DAEMON_DIRECTORY="{directory}"')
        return subprocess.run(
            ["/usr/bin/env", "-i", f"PATH={fake_bin}:/usr/bin:/bin",
             "bash", "-c", f"umask 077\n{body}"],
            capture_output=True, text=True)

    created = tmp_path / "exec-daemon-fresh"
    result = run_fragment(created)
    assert result.returncode == 0, result.stderr
    assert stat.S_IMODE(created.stat().st_mode) == 0o755

    stale = tmp_path / "exec-daemon-stale"
    stale.mkdir()
    os.chmod(stale, 0o600)
    result = run_fragment(stale)
    assert result.returncode == 0, result.stderr
    assert stat.S_IMODE(stale.stat().st_mode) == 0o755


def _fake_sudo_bin(tmp_path):
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir(exist_ok=True)
    (fake_bin / "sudo").write_text("#!/bin/sh\nshift\nexec \"$@\"\n")
    (fake_bin / "sudo").chmod(0o755)
    return fake_bin


def test_start_box_links_exec_daemon_node_and_precreates_box_chrome(
        tmp_path):
    """Execute the /exec-daemon node link and box-chrome placeholder
    fragments with a fake sudo and assert the results."""
    package_dir = Path(__file__).resolve().parent.parent
    script = (package_dir / "box" / "start-box.sh").read_text()
    fake_bin = _fake_sudo_bin(tmp_path)

    exec_dir = tmp_path / "exec-daemon"
    exec_dir.mkdir()
    node_target = tmp_path / "runtime" / "node"
    node_target.parent.mkdir()
    node_target.write_text("#!/bin/sh\n")
    node_target.chmod(0o700)
    fragment = script[
        script.index('NODE_BINARY="$PACK_DIR/node"'):
        script.index("# ── python + protobuf for box.py")]
    fragment = fragment.replace(
        'NODE_BINARY="$PACK_DIR/node"',
        f'EXEC_DAEMON_DIRECTORY="{exec_dir}"\nNODE_BINARY="{node_target}"')
    result = subprocess.run(
        ["/usr/bin/env", "-i", f"PATH={fake_bin}:/usr/bin:/bin",
         "bash", "-c", fragment],
        capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert (exec_dir / "node").is_symlink()
    assert os.readlink(exec_dir / "node") == str(node_target)

    fake_usr_local_bin = tmp_path / "usr-local-bin"
    fake_usr_local_bin.mkdir()
    fragment = script[
        script.index("if [ -d /usr/local/bin ] && "
                     "[ ! -w /usr/local/bin/box-chrome ]; then"):
        script.index("# Per-window desktops need a window manager")]
    fragment = fragment.replace("/usr/local/bin", str(fake_usr_local_bin))
    result = subprocess.run(
        ["/usr/bin/env", "-i", f"PATH={fake_bin}:/usr/bin:/bin",
         "bash", "-c", fragment],
        capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    placeholder = fake_usr_local_bin / "box-chrome"
    assert placeholder.is_file()
    assert stat.S_IMODE(placeholder.stat().st_mode) == 0o755
    # start-desktop.sh's `cat >` must succeed by truncating the file
    placeholder.write_text("#!/usr/bin/env bash\n")


def test_start_box_stops_owned_services_before_starting_replacements():
    script = (Path(__file__).resolve().parent.parent / "box" /
              "start-box.sh").read_text()
    force_restart = script.index(
        'if [ "${GROKBOT_FORCE_RESTART_RUNTIME:-0}" = "1" ]; then')
    stop_box = script.index(
        'stop_owned_runtime_process "$GB_HOME/box.pid"')
    start_box = script.index('echo "start-box: phase=box"')
    start_daemon = script.index(
        "if ! box_exec_daemon_listening; then")
    assert force_restart < stop_box < start_box < start_daemon


def test_start_box_restarts_and_waits_for_provision_listener():
    script = (Path(__file__).resolve().parent.parent / "box" /
              "start-box.sh").read_text()
    assert "|| ! provision_listener_healthy" in script
    assert "NEW_PROVISION_PID" not in script
    assert 'if [ "$PROVISION_READY" -ne 1 ]; then' in script
    assert script.index('echo "start-box: done"') > \
        script.index('echo "start-box: refreshed provisioning listener"')


def test_provision_supervision_retries_nonzero_exits_three_times(
        tmp_path, monkeypatch):
    module = _load_provision_module()
    home = _prepare_provision_module(module, tmp_path)
    clock = [0.0]
    processes = [
        _FakeStartBoxProcess(1101, 7),
        _FakeStartBoxProcess(1102, 8),
        _FakeStartBoxProcess(1103, 9),
    ]
    launched = []

    def launch(**_kwargs):
        proc = processes[len(launched)]
        launched.append(proc)
        return proc

    monkeypatch.setattr(module, "_launch_start_box", launch)
    monkeypatch.setattr(module, "_services_running", lambda: False)
    monkeypatch.setattr(module.time, "monotonic", lambda: clock[0])
    server = module.ProvisionServer(("127.0.0.1", 0), module.Handler)
    try:
        server.launch_start_box()
        server.supervise_start_box()
        assert launched == processes[:1]
        assert processes[0].wait_calls == 1
        assert not (home / "start-box.pid").exists()
        status = module._listener_status(server)
        assert status["start_box_exit_code"] == 7
        assert status["start_box_attempts"] == 1
        assert status["state"] == "provisioning"

        clock[0] = 9
        server.supervise_start_box()
        assert len(launched) == 1
        clock[0] = 10
        server.supervise_start_box()
        assert len(launched) == 2
        server.supervise_start_box()
        assert processes[1].wait_calls == 1

        clock[0] = 19
        server.supervise_start_box()
        assert len(launched) == 2
        clock[0] = 20
        server.supervise_start_box()
        assert len(launched) == 3
        server.supervise_start_box()
        assert processes[2].wait_calls == 1

        status = module._listener_status(server)
        assert status["start_box_exit_code"] == 9
        assert status["start_box_attempts"] == 3
        assert status["phase"] == "start-box-failed"
        assert status["state"] == "provisioning"
        assert len(launched) == 3
    finally:
        server.server_close()


def test_provision_supervision_does_not_retry_exit_zero(
        tmp_path, monkeypatch):
    module = _load_provision_module()
    _prepare_provision_module(module, tmp_path)
    process = _FakeStartBoxProcess(1201, 0)
    launches = []
    monkeypatch.setattr(
        module, "_launch_start_box",
        lambda **_kwargs: launches.append(process) or process)
    monkeypatch.setattr(module, "_services_running", lambda: False)
    server = module.ProvisionServer(("127.0.0.1", 0), module.Handler)
    try:
        server.launch_start_box()
        server.supervise_start_box()
        server.supervise_start_box()
        status = module._listener_status(server)
        assert launches == [process]
        assert process.wait_calls == 1
        assert status["start_box_exit_code"] == 0
        assert status["start_box_attempts"] == 1
        assert status["phase"] != "start-box-failed"
    finally:
        server.server_close()


def test_provision_supervision_does_not_retry_when_services_are_running(
        tmp_path, monkeypatch):
    module = _load_provision_module()
    _prepare_provision_module(module, tmp_path)
    process = _FakeStartBoxProcess(1301, 4)
    launches = []
    monkeypatch.setattr(
        module, "_launch_start_box",
        lambda **_kwargs: launches.append(process) or process)
    monkeypatch.setattr(module, "_services_running", lambda: True)
    server = module.ProvisionServer(("127.0.0.1", 0), module.Handler)
    try:
        server.launch_start_box()
        server.supervise_start_box()
        status = module._listener_status(server)
        assert launches == [process]
        assert process.wait_calls == 1
        assert status["start_box_exit_code"] == 4
        assert status["start_box_attempts"] == 1
        assert status["phase"] != "start-box-failed"
        server.supervise_start_box()
        assert launches == [process]
    finally:
        server.server_close()


def test_provision_listener_flow_and_second_post_409(tmp_path):
    proc, port, home = _start_provision_listener(tmp_path)
    try:
        assert _wait_listener(port, proc)["state"] == "awaiting"
        unknown_status, unknown_body = _post_json(
            port, "/provision",
            {"GROKBOT_GATEWAY_TOKEN": "test-secret", "BAD_KEY": "x"})
        assert unknown_status == 400
        assert unknown_body["keys"] == ["BAD_KEY"]
        non_string_status, _ = _post_json(
            port, "/provision", {"GROKBOT_GATEWAY_TOKEN": 5})
        assert non_string_status == 400

        if os.name == "posix":
            log_dir = home / "logs"
            log_dir.mkdir(parents=True, exist_ok=True)
            (log_dir / "start-box.log").write_text(
                "GROKBOT_GATEWAY_TOKEN=test-secret test-secret\n")
        status, body = _post_json(
            port, "/provision", {"GROKBOT_GATEWAY_TOKEN": "test-secret"})
        assert status == 202
        assert body["state"] == "provisioning"
        assert proc.poll() is None
        creds = home / "creds.env"
        assert creds.exists()
        if os.name == "posix":
            assert creds.stat().st_mode & 0o777 == 0o600

        status, details = _get_json(port, "/status")
        assert status == 200
        assert details["state"] == "provisioning"
        assert details["phase"] in {
            "download", "extract", "box", "host-main"}
        assert {"listener", "start_box", "box", "host_main"} <= set(
            details["pids"])
        assert "test-secret" not in json.dumps(details)

        second_status, _ = _post_json(
            port, "/provision", {"GROKBOT_GATEWAY_TOKEN": "test-secret"})
        assert proc.poll() is None
        assert second_status == 409

        duplicate = subprocess.run(
            [sys.executable,
             str(Path(__file__).resolve().parent.parent
                 / "box" / "provision.py")],
            env=dict(os.environ, GROKBOT_HOME=str(home),
                     GROKBOT_PROVISION_PORT=str(port)),
            timeout=5, check=False)
        assert duplicate.returncode == 0
        assert proc.poll() is None
    finally:
        if proc.poll() is None:
            proc.terminate()
            proc.wait(timeout=10)


def test_provision_update_restarts_existing_identity_and_rejects_mismatch(
        tmp_path):
    proc, port, home = _start_provision_listener(tmp_path)
    home.mkdir(parents=True, exist_ok=True)
    start_box = home / "start-box.sh"
    start_box.write_text("#!/bin/sh\nexit 0\n")
    start_box.chmod(0o700)
    try:
        _wait_listener(port, proc)
        user_json = json.dumps({"user_id": "share-user-1", "org_id": "org-1"})
        status, _ = _post_json(port, "/provision", {
            "GROKBOT_GATEWAY_TOKEN": "gateway-old",
            "GROKBOT_INFERENCE_CREDENTIAL": "inference-old",
            "GROKBOT_USER_JSON": user_json,
        })
        assert status == 202
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if not _get_json(port, "/status")[1]["pids"]["start_box"]["alive"]:
                break
            time.sleep(0.05)

        update = {
            "GROKBOT_GATEWAY_TOKEN": "gateway-new",
            "GROKBOT_INFERENCE_CREDENTIAL": "inference-new",
            "GROKBOT_SHARE_CREDENTIAL": "share-new",
            "GROKBOT_USER_JSON": user_json,
        }
        status, body = _post_json(port, "/provision/update", update)
        assert status == 202, body
        assert body["state"] == "updating"
        assert "GROKBOT_SHARE_CREDENTIAL=share-new" in (
            home / "creds.env").read_text()
        if os.name == "posix":
            assert (home / "creds.env").stat().st_mode & 0o777 == 0o600
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            state = _get_json(port, "/status")[1]
            if not state["pids"]["start_box"]["alive"]:
                break
            time.sleep(0.05)
        assert not state["pids"]["start_box"]["alive"]

        mismatch = {**update, "GROKBOT_USER_JSON": json.dumps({
            "user_id": "different-user", "org_id": "org-1"})}
        status, body = _post_json(port, "/provision/update", mismatch)
        assert status == 403
        assert body["error"] == "identity_mismatch"
        assert "share-new" in (home / "creds.env").read_text()
        assert "different-user" not in (home / "creds.env").read_text()
    finally:
        if proc.poll() is None:
            proc.terminate()
            proc.wait(timeout=10)


def test_provision_listener_idle_exit(tmp_path):
    proc, port, _ = _start_provision_listener(tmp_path, idle="1")
    try:
        _wait_listener(port, proc)
        assert proc.wait(timeout=5) == 0
    finally:
        if proc.poll() is None:
            proc.terminate()
            proc.wait(timeout=10)


def test_provision_listener_restart_resumes_services(tmp_path):
    home = tmp_path / "grok-bot-box"
    home.mkdir()
    (home / "creds.env").write_text(
        "GROKBOT_GATEWAY_TOKEN='test-token'\n")
    (home / "start-box.sh").write_text(
        'printf resumed > "$GROKBOT_HOME/resume.log"\n')
    proc, port, _ = _start_provision_listener(tmp_path)
    try:
        assert _wait_listener(port, proc)["state"] == "provisioning"
        deadline = time.monotonic() + 5
        while (time.monotonic() < deadline
               and not (home / "resume.log").exists()):
            time.sleep(0.05)
        assert (home / "resume.log").read_text() == "resumed"
        assert proc.poll() is None
    finally:
        if proc.poll() is None:
            proc.terminate()
            proc.wait(timeout=10)


def test_ensure_sandbox_uses_provision_listener(tmp_path, monkeypatch):
    backend = DesktopBackend(port=0, api_key="k",
                             state_path=tmp_path / "s.json")
    monkeypatch.delenv("GROKBOT_ASSET_TOKEN", raising=False)
    monkeypatch.delenv("GROKBOT_ASSET_ORIGIN", raising=False)
    api = _patch_gateway(monkeypatch, backend, _FakeDevBoxApi())
    relayed = []

    def provision_relay(_api, _sid, path, payload=None):
        relayed.append((path, payload))
        if path == "/health":
            return 200, {"state": "awaiting"}
        return 202, {"state": "provisioning"}

    monkeypatch.setattr(backend, "_provision_relay", provision_relay)
    response = backend._ensure_box({"authorization": "Bearer x"})
    assert [path for path, _ in relayed] == ["/health", "/provision"]
    payload = relayed[-1][1]
    assert payload["GROKBOT_GATEWAY_TOKEN"]
    assert payload["GROKBOT_INFERENCE_CREDENTIAL"]
    assert payload["GROKBOT_SHARE_CREDENTIAL"] == "share-credential-test"
    assert payload["DEVBOX_HARNESS_LLM_TOKEN"] == "session-asset-token"
    assert payload["DEVBOX_HARNESS_LLM_OPENAI_BASE_URL"] == (
        "https://app.devinai.net/internal/harness-llm/v1")
    assert api.asset_token_json_body == {"session_id": "devin-fake-0"}
    assert json.loads(payload["GROKBOT_USER_JSON"])["user_id"] == "u"
    assert response["provision_path"] == "provision"
    assert backend._box_state({
        "user_id": "u", "org_id": "org-devbox"
    })["provision_path"] == "provision"
    kwargs = api.created[0][2]
    assert kwargs["image_family"] == "Debian_12"
    assert BOX_PROMPT == (
        "This session hosts the Grok Bot box runtime. Do not run commands "
        "or stage/start services unless a follow-up message explicitly asks "
        "you to do so.")


def test_asset_token_request_normalizes_session_id(tmp_path):
    backend = DesktopBackend(port=0, api_key="k",
                             state_path=tmp_path / "s.json")
    api = _FakeDevBoxApi()
    payload = {}

    backend._set_session_asset_token(
        api, "4ce430018b80434fb578585b75297d9e", payload)

    assert api.asset_token_json_body == {
        "session_id": "devin-4ce430018b80434fb578585b75297d9e"}
    assert payload["DEVBOX_HARNESS_LLM_TOKEN"] == "session-asset-token"
    assert payload["DEVBOX_HARNESS_LLM_OPENAI_BASE_URL"] == (
        "https://app.devinai.net/internal/harness-llm/v1")


def test_asset_token_failure_does_not_block_listener_provisioning(
        tmp_path, monkeypatch, caplog):
    backend = DesktopBackend(port=0, api_key="k",
                             state_path=tmp_path / "s.json")
    monkeypatch.delenv("GROKBOT_ASSET_TOKEN", raising=False)
    monkeypatch.delenv("GROKBOT_ASSET_ORIGIN", raising=False)
    api = _FakeDevBoxApi()
    api.asset_token_error = ApiError(503, "private error body")
    _patch_gateway(monkeypatch, backend, api)
    relayed = []

    def provision_relay(_api, _sid, path, payload=None):
        relayed.append((path, payload))
        if path == "/health":
            return 200, {"state": "awaiting"}
        return 202, {"state": "provisioning"}

    monkeypatch.setattr(backend, "_provision_relay", provision_relay)
    with caplog.at_level(logging.WARNING, logger="devbox_bot.desktop"):
        response = backend._ensure_box({"authorization": "Bearer x"})

    assert response["provision_path"] == "provision"
    assert [path for path, _ in relayed] == ["/health", "/provision"]
    payload = relayed[-1][1]
    assert "DEVBOX_HARNESS_LLM_TOKEN" not in payload
    assert "DEVBOX_HARNESS_LLM_OPENAI_BASE_URL" not in payload
    assert api.asset_token_json_body == {"session_id": "devin-fake-0"}
    assert "status=503" in caplog.text
    assert "private error body" not in caplog.text
    assert "session-asset-token" not in caplog.text


def test_existing_asset_token_override_has_priority(
        tmp_path, monkeypatch):
    backend = DesktopBackend(port=0, api_key="k",
                             state_path=tmp_path / "s.json")
    monkeypatch.setenv("GROKBOT_ASSET_TOKEN", "override-asset-token")
    monkeypatch.setenv(
        "GROKBOT_ASSET_ORIGIN",
        "https://override.example/internal/harness-llm/v1")
    api = _patch_gateway(monkeypatch, backend, _FakeDevBoxApi())
    relayed = []

    def provision_relay(_api, _sid, path, payload=None):
        relayed.append((path, payload))
        if path == "/health":
            return 200, {"state": "awaiting"}
        return 202, {"state": "provisioning"}

    monkeypatch.setattr(backend, "_provision_relay", provision_relay)
    backend._ensure_box({"authorization": "Bearer x"})

    payload = relayed[-1][1]
    assert payload["DEVBOX_HARNESS_LLM_TOKEN"] == "override-asset-token"
    assert payload["DEVBOX_HARNESS_LLM_OPENAI_BASE_URL"] == (
        "https://override.example/internal/harness-llm/v1")
    assert api.asset_token_json_body is None


def test_existing_box_refreshes_share_credential_without_replacing_session(
        tmp_path, monkeypatch):
    backend = DesktopBackend(port=0, api_key="k",
                             state_path=tmp_path / "s.json")
    api = _FakeDevBoxApi(sessions={"devin-existing": {"status": "running"}})
    _patch_gateway(monkeypatch, backend, api)
    ident = {"user_id": "u", "org_id": "org-devbox"}
    backend._save_box_state(ident, {
        "session_id": "devin-existing",
        "gateway_token": "gateway-existing",
        "inference_credential": "inference-existing",
        "share_credential_expires_at_ms": 0,
    })
    relayed = []

    def provision_relay(_api, session_id, path, payload=None):
        relayed.append((session_id, path, payload))
        if path == "/provision/update":
            return 202, {"startBoxAttempts": 2}
        return 200, {
            "state": "provisioned",
            "start_box_exit_code": 0,
            "start_box_attempts": 2,
            "pids": {"start_box": {"alive": False}},
        }

    monkeypatch.setattr(backend, "_provision_relay", provision_relay)
    response = backend._ensure_box({"authorization": "Bearer x"})
    assert response["pod_id"] == "devin-existing"
    assert not api.created
    assert not api.deleted
    assert [item[1] for item in relayed] == [
        "/provision/update", "/status",
    ]
    payload = relayed[0][2]
    assert payload["GROKBOT_SHARE_CREDENTIAL"] == "share-credential-test"
    assert json.loads(payload["GROKBOT_USER_JSON"])["user_id"] == "u"
    assert "share-credential-test" not in (tmp_path / "s.json").read_text()


def test_ensure_sandbox_agent_fallback_waits_for_timeout(
        tmp_path, monkeypatch):
    backend = DesktopBackend(port=0, api_key="k",
                             state_path=tmp_path / "s.json")
    api = _patch_gateway(monkeypatch, backend, _FakeDevBoxApi())
    elapsed = [0.0]
    monkeypatch.setattr(
        "devbox_bot.desktop.PROVISION_WAIT_S", 0.02)
    monkeypatch.setattr(
        "devbox_bot.desktop.time.monotonic", lambda: elapsed[0])
    monkeypatch.setattr(
        "devbox_bot.desktop.time.sleep",
        lambda seconds: elapsed.__setitem__(0, elapsed[0] + seconds))
    response = backend._ensure_box({"authorization": "Bearer x"})
    assert elapsed[0] >= 0.02
    assert response["provision_path"] == "agent"
    assert len(api.messages) == 1
    assert "120 seconds" in api.messages[0][2]
    state = backend._box_state({
        "user_id": "u", "org_id": "org-devbox"
    })
    assert state["provision_path"] == "agent"


def test_ensure_sandbox_reprovisions_when_listener_awaiting(
        tmp_path, monkeypatch):
    backend = DesktopBackend(port=0, api_key="k",
                             state_path=tmp_path / "s.json")
    api = _FakeDevBoxApi(sessions={"devin-old": {"status": "running"}})
    _patch_gateway(monkeypatch, backend, api)
    backend._save_box_state({"user_id": "u", "org_id": "org-devbox"}, {
        "session_id": "devin-old", "gateway_token": "gt",
        "inference_credential": "ic",
        "share_credential_expires_at_ms": int(time.time() * 1000)
        + 365 * 24 * 60 * 60 * 1000,
    })
    calls = []
    gateway_calls = []

    def provision_relay(_api, _sid, path, payload=None):
        calls.append((path, payload))
        if path == "/health":
            return 200, {"state": "awaiting"}
        return 202, {"state": "provisioning"}

    def gateway_coords(_api, sid, box):
        gateway_calls.append(sid)
        if len(gateway_calls) == 1:
            return None
        return {"pod_id": sid, "provision_path": box.get("provision_path")}

    monkeypatch.setattr(backend, "_provision_relay", provision_relay)
    monkeypatch.setattr(backend, "_gateway_coords", gateway_coords)
    response = backend._ensure_box({"authorization": "Bearer x"})
    assert [path for path, _ in calls] == ["/health", "/provision"]
    assert response["pod_id"] == "devin-old"
    assert response["provision_path"] == "provision"
    assert not api.created
