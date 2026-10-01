"""Tests for devbox_bot (plugin/bot). Pure stdlib + protobuf; no DevBox
imports — the bot only uses DevBox's *existing* HTTP APIs."""

from __future__ import annotations

import base64
import hashlib
import http.server
import io
import json
import sys
import threading
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
from devbox_bot.desktop import DesktopBackend, _pkce_ok
from devbox_bot.inference import stream_inference


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


import urllib.error

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


# ── EnsureSandBox against a fake DevBox API ─────────────────────────

class _FakeDevBoxApi:
    """Stands in for DevBoxApi inside _ensure_box; records calls."""

    def __init__(self, ready_after=1, sessions=None):
        self.token = "tok"
        self.sessions = sessions or {}
        self.created = []
        self.deleted = []
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

    def list_secrets(self, org_id):
        return []

    def call(self, method, path, **kw):
        if path.startswith("/api/preview-link/"):
            self._ensure_calls += 1
            if self._ensure_calls < self.ready_after:
                raise RuntimeError("not ready")
            return {"url": "https://p-1340.example.dev/",
                    "token": "cap-x"}
        raise RuntimeError(path)


class _FakeHTTPResp:
    status = 200

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
