"""Tests for devbox_bot (plugin/bot). Pure stdlib + protobuf; no DevBox
imports — the bot only uses DevBox's *existing* HTTP APIs."""

from __future__ import annotations

import base64
import hashlib
import http.server
import importlib.util
import io
import json
import os
import re
import socket
import subprocess
import sys
import tarfile
import threading
import time
import urllib.request
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from devbox_bot import codec as codec_mod
from devbox_bot.box import BoxBackend
from devbox_bot.codec import ConnectError
from devbox_bot.connect import (
    make_handler,
    reset_unhandled,
    unhandled_methods,
)
from devbox_bot.desktop import BOX_PROMPT, DesktopBackend, _pkce_ok
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
    with tarfile.open(base, "w:gz") as archive:
        for name, payload in (
                ("MANIFEST.json", b"{}"),
                ("devbox_bot/stale.py", b"stale")):
            info = tarfile.TarInfo(name)
            info.size = len(payload)
            archive.addfile(info, io.BytesIO(payload))

    script = package_dir.parent / "tools" / "repack_runtime_pack.py"
    subprocess.run([
        sys.executable, str(script), str(base), str(output),
        "--source", str(package_dir),
    ], check=True, capture_output=True, text=True)
    subprocess.run([
        sys.executable, str(script), str(base), str(second_output),
        "--source", str(package_dir),
    ], check=True, capture_output=True, text=True)
    assert hashlib.sha256(output.read_bytes()).digest() == \
        hashlib.sha256(second_output.read_bytes()).digest()

    source_files = {
        f"devbox_bot/{path.relative_to(package_dir).as_posix()}":
            hashlib.sha256(path.read_bytes()).hexdigest()
        for path in package_dir.rglob("*")
        if path.is_file() and "__pycache__" not in path.parts
        and path.suffix != ".pyc"
    }
    with tarfile.open(output, "r:gz") as archive:
        packed = {
            member.name.removeprefix("./"):
                hashlib.sha256(archive.extractfile(member).read()).hexdigest()
            for member in archive.getmembers()
            if member.isfile() and
            member.name.removeprefix("./").startswith("devbox_bot/")
        }
        assert "devbox_bot/stale.py" not in packed
        assert packed == source_files


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
    assert resp["gateway_url"].startswith("https://")
    assert resp["network_token"] == "cap-x"
    assert resp["gateway_token"]
    assert resp["pod_id"] == "devin-fake-0"


def test_ensure_sandbox_reuses_live_session(tmp_path, monkeypatch):
    backend = DesktopBackend(port=0, api_key="k",
                             state_path=tmp_path / "s.json")
    api = _FakeDevBoxApi(sessions={"devin-old": {"status": "running"}})
    _patch_gateway(monkeypatch, backend, api)
    backend._save_box_state({"user_id": "u", "org_id": "org-devbox"}, {
        "session_id": "devin-old", "gateway_token": "gt"})
    resp = backend._ensure_box({"authorization": "Bearer x"})
    assert not api.created, "should reuse existing session"
    assert resp["pod_id"] == "devin-old"


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
    assert script.count("--http1.1") == 4
    assert script.count("--retry-all-errors") == 3


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
    assert "--max-time 600" in fallback
    assert "--retry 5" in fallback
    assert "--retry-all-errors" in fallback
    assert "--retry-delay 2" in fallback
    assert "-C -" in fallback

    checksum = next(command for command in downloads
                    if 'RUNTIME_URL.sha256' in command)
    assert "--speed-limit 1" in checksum
    assert "--speed-time 20" in checksum


def test_start_box_size_probe_is_nonfatal_and_err_trap_is_installed():
    script = (Path(__file__).resolve().parent.parent / "box" /
              "start-box.sh").read_text()
    assert """trap 'echo "start-box: failed rc=$? line=$LINENO" >&2' ERR""" \
        in script
    assert "-fsSIL" in script
    assert '|| true)"' in script


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

    def launch():
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
        lambda: launches.append(process) or process)
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
        lambda: launches.append(process) or process)
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
