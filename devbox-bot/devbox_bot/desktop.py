"""devbox_bot.desktop — the local backend the Grok Bot desktop talks to.

Run:  python -m devbox_bot.desktop [--port 7811]

The packaged app is pointed at this process with
``SAND_BACKEND_URL=CURSOR_API_BASE_URL=CURSOR_WEBSITE_URL=http://127.0.0.1:7811``.

Everything here is implemented on top of *existing* DevBox interfaces:
OIDC authorize/token on auth.devinai.net, v3 sessions, the preview-link
capability route and org secrets/blueprints. No DevBox code changes.

Two login modes:

* interactive — real DevBox OIDC authorization-code + PKCE flow; the
  desktop's browser finishes on ``/callback`` (loopback redirect URIs
  are allowed by the DevBox OIDC server).
* headless — only when ``DEVBOX_API_KEY`` is set in the environment; any
  well-formed poll completes immediately with a locally minted HS256
  token mapped to the API key in memory. This mode is refused when the
  env var is absent.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import hmac
import json
import logging
import os
import secrets
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from .codec import ConnectError
from .connect import ConnectRouter, serve
from .devbox_api import DEFAULT_AUTH_ORIGIN, DEFAULT_ORIGIN, DevBoxApi
from .handlers import register
from .inference import stream_inference

log = logging.getLogger("devbox_bot.desktop")

BOX_TAG = "grok-bot-box"
BOX_PROMPT = (
    "This session hosts the Grok Bot box runtime. Do not run commands or "
    "stage/start services unless a follow-up message explicitly asks you "
    "to do so.")
BOX_STAGE_PROMPT = (
    "The automatic provisioning listener was unreachable for 120 seconds. "
    "Run exactly one one-shot exec without shell_id: install -d -m 700 "
    "\"$HOME/.grok-bot-box\"; umask 077; env | grep -E "
    "'^(GROKBOT_|DEVBOX_HARNESS_LLM_)' > "
    "\"$HOME/.grok-bot-box/creds.env\"; chmod 600 "
    "\"$HOME/.grok-bot-box/creds.env\"; bash "
    "\"$HOME/.grok-bot-box/start-box.sh\". Reply 'ready' after it exits.")
OIDC_CLIENT_ID = "grok-bot-desktop"
OIDC_SCOPE = "openid profile email offline_access"
PENDING_TTL = 600
BOX_DEADLINE_S = int(os.environ.get("GROKBOT_BOX_DEADLINE_S", "300"))
PROVISION_WAIT_S = int(os.environ.get("GROKBOT_PROVISION_WAIT_S", "120"))
STATE_DIR_NAME = "devbox-bot"
PROD_CLIENT_ID = "KbZUR41cY7W6zRSdpSUJ7I7mLYBKOCmB"
DEV_CLIENT_ID = "OzaBXLClY5CAGxNzUhQ2vlknpi07tGuE"
CLIENT_IDS = {PROD_CLIENT_ID, DEV_CLIENT_ID}


def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _pkce_ok(challenge: str, verifier: str) -> bool:
    digest = hashlib.sha256(verifier.encode()).digest()
    return hmac.compare_digest(_b64url(digest), challenge)


def _hs256_jwt(secret: bytes, claims: dict) -> str:
    header = _b64url(json.dumps(
        {"alg": "HS256", "typ": "JWT"}).encode())
    payload = _b64url(json.dumps(claims).encode())
    sig = _b64url(hmac.new(
        secret, f"{header}.{payload}".encode(),
        hashlib.sha256).digest())
    return f"{header}.{payload}.{sig}"


def _default_state_path() -> Path:
    base = os.environ.get("XDG_STATE_HOME") or os.environ.get(
        "LOCALAPPDATA") or str(Path.home() / ".local" / "state")
    path = Path(base) / STATE_DIR_NAME
    path.mkdir(parents=True, exist_ok=True)
    return path / "state.json"


class _State:
    """Durable, 0600 state file: issued-token → DevBox credential map and
    the current box session coordinates."""

    def __init__(self, path: Path | None = None) -> None:
        self.path = path or _default_state_path()
        self._lock = threading.Lock()
        self.data: dict = {"tokens": {}, "refresh": {}, "box": {}}
        if self.path.exists():
            try:
                self.data.update(json.loads(
                    self.path.read_text()))
            except (ValueError, OSError):
                pass
        try:
            os.chmod(self.path, 0o600)
        except OSError:
            pass

    def save(self) -> None:
        with self._lock:
            fd, tmp = tempfile.mkstemp(
                dir=str(self.path.parent), prefix="state-")
            try:
                os.write(fd, json.dumps(self.data).encode())
                try:
                    os.fchmod(fd, 0o600)
                except AttributeError:
                    pass  # Windows: chmod the path below instead
            finally:
                os.close(fd)
            os.replace(tmp, self.path)
            try:
                os.chmod(self.path, 0o600)
            except OSError:
                pass

    def get(self, *keys, default=None):
        node = self.data
        for key in keys:
            if not isinstance(node, dict) or key not in node:
                return default
            node = node[key]
        return node


class DesktopBackend:
    def __init__(self, port: int = 7811,
                 devbox_origin: str = DEFAULT_ORIGIN,
                 auth_origin: str = DEFAULT_AUTH_ORIGIN,
                 state_path: Path | None = None,
                 api_key: str | None = None) -> None:
        self.port = port
        self.devbox_origin = devbox_origin.rstrip("/")
        self.auth_origin = auth_origin.rstrip("/")
        self.state = _State(state_path)
        self.api_key = api_key or os.environ.get("DEVBOX_API_KEY") or None
        self.local_secret = secrets.token_bytes(32)
        self.pending: dict[str, dict] = {}
        self._pending_lock = threading.Lock()
        # model catalog override: env JSON {"client-id": "provider-model"}
        self.model_map = json.loads(
            os.environ.get("GROKBOT_LLM_MODEL_MAP", "{}") or "{}")
        self.llm_base_url = os.environ.get("GROKBOT_LLM_BASE_URL", "")
        self.llm_api_key = os.environ.get("GROKBOT_LLM_API_KEY", "")
        self.llm_model = os.environ.get("GROKBOT_LLM_MODEL", "")
        self._identity_cache: dict[str, dict] = {}

    # ── bearer → DevBox credential resolution ─────────────────────
    def devbox_token_for(self, authorization: str) -> str | None:
        token = authorization.removeprefix("Bearer ").strip()
        if not token:
            return None
        # an OIDC access token passes straight through to DevBox
        if token.count(".") == 2:
            parts = token.split(".")
            try:
                claims = json.loads(base64.urlsafe_b64decode(
                    parts[1] + "=="))
            except (ValueError, json.JSONDecodeError):
                claims = {}
            # DevBox-issued tokens (OIDC) work directly
            if (claims.get("iss", "").startswith(self.auth_origin)
                    or "grok-bot" not in claims.get("aud", "")) \
                    and claims.get("sub"):
                return token
            if claims.get("iss", "").startswith(
                    "devbox-bot-local"):
                return self.state.get("tokens", token)
            # locally minted headless token → mapped API key
            mapped = self.state.get("tokens", token)
            if mapped:
                return mapped
            return token if claims else self.state.get("tokens", token)
        return self.state.get("tokens", token) or (
            token if token.startswith(("dv_", "cog_", "apk_"))
            else None)

    def api_for(self, ctx: dict) -> DevBoxApi:
        token = self.devbox_token_for(ctx.get("authorization", ""))
        if not token:
            raise ConnectError("unauthenticated", "sign-in required")
        return DevBoxApi(self.devbox_origin, token)

    def _identity(self, api: DevBoxApi) -> dict:
        if api.token in self._identity_cache:
            return self._identity_cache[api.token]
        try:
            body = api.current_membership()
        except Exception:  # noqa: BLE001 — identity lookup is best-effort
            body = {}
        org = body.get("org") if isinstance(body.get("org"), dict) else {}
        user = body.get("user") if isinstance(
            body.get("user"), dict) else body
        ident = {
            "user_id": (user.get("id") or user.get("user_id")
                        or body.get("user_id") or ""),
            "org_id": (org.get("org_id") or body.get("organization_id")
                       or body.get("org_id") or ""),
            "email": user.get("email") or body.get("email") or "",
            "name": (user.get("display_name") or user.get("name")
                     or body.get("name") or ""),
        }
        self._identity_cache[api.token] = ident
        return ident

    # ── Connect handlers ──────────────────────────────────────────
    def router(self) -> ConnectRouter:
        r = ConnectRouter()
        # shared account handlers for methods we don't override below
        # (privacy mode, managed skills, plugins, MCP, Statsig, …)
        register(
            r,
            lambda ctx: self._identity(self.api_for(ctx)),
            self._catalog)

        @r.unary("aiserver.v1.DashboardService", "GetMe")
        def _get_me(_req, ctx):
            api = self.api_for(ctx)
            ident = self._identity(api)
            first, _, last = (ident.get("name") or "").partition(" ")
            return {
                "auth_id": ident.get("user_id") or "devbox",
                "email": ident.get("email") or "",
                "first_name": first or ident.get("name") or "",
                "last_name": last,
            }

        @r.unary("aiserver.v1.DashboardService", "GetTeams")
        def _get_teams(_req, ctx):
            api = self.api_for(ctx)
            ident = self._identity(api)
            return {"teams": [{
                "name": ident.get("org_id") or "devbox",
                "id": 1, "privacy_mode_forced": True,
                "subscription_status": "active"}]}

        @r.streaming("aiserver.v1.InferenceService", "Stream")
        def _stream(req, ctx):
            self.api_for(ctx)  # still require sign-in
            if not self.llm_base_url:
                yield {"error": {
                    "message": "GROKBOT_LLM_BASE_URL not configured",
                    "code": "UNKNOWN", "error_type": 1}}
                return
            model_map = dict(self.model_map)
            if self.llm_model:
                model_map.setdefault("", self.llm_model)
            yield from stream_inference(
                req, self.llm_base_url, self.llm_api_key, model_map)

        @r.unary("aiserver.v1.InferenceService",
                 "RecordAgentFollowupClassification")
        def _noop1(_req, _ctx):
            return {}

        @r.unary("aiserver.v1.InferenceService",
                 "RecordAgentPostTurnLabeling")
        def _noop2(_req, _ctx):
            return {}

        @r.unary("aiserver.v1.AiService", "AvailableModels")
        def _models(_req, ctx):
            self.api_for(ctx)
            models = self._catalog()
            return {"models": [{
                "name": m, "default_on": i == 0,
                "supports_agent": True, "supports_thinking": True,
                "supports_images": True, "supports_max_mode": True,
                "supports_non_max_mode": True,
                "supports_cmd_k": True,
                "client_display_name": m, "server_model_name": m,
                "context_token_limit": 200000,
                "context_token_limit_for_max_mode": 200000,
            } for i, m in enumerate(models)]}

        @r.unary("aiserver.v1.AiService", "GetDefaultModel")
        def _default_model(_req, ctx):
            self.api_for(ctx)
            models = self._catalog()
            return {"model": models[0] if models else "",
                    "default_model": models[0] if models else ""}

        # ── GrokBotService box lifecycle ──────────────────────────
        @r.unary("aiserver.v1.GrokBotService", "EnsureSandBox")
        def _ensure(_req, ctx):
            return self._ensure_box(ctx)

        @r.unary("aiserver.v1.GrokBotService", "EnsureSandBoxWindow")
        def _ensure_window(_req, ctx):
            return self._ensure_box(ctx)

        @r.unary("aiserver.v1.GrokBotService", "RecreateSandBox")
        def _recreate(_req, ctx):
            return self._recreate_box(ctx)

        @r.unary("aiserver.v1.GrokBotService", "ForceRecreateSandBox")
        def _force_recreate(_req, ctx):
            return self._recreate_box(ctx)

        @r.unary("aiserver.v1.GrokBotService", "GetSandBoxRunState")
        def _run_state(_req, ctx):
            return self._box_run_state(ctx)

        @r.unary("aiserver.v1.GrokBotService", "ListSandBoxes")
        def _list_boxes(_req, ctx):
            api = self.api_for(ctx)
            ident = self._identity(api)
            box = self._box_state(ident)
            running = bool(box.get("session_id"))
            return {"boxes": [{"running": running}]}

        return r

    def _catalog(self) -> list[str]:
        env = os.environ.get("GROKBOT_MODELS") or ""
        models = [m.strip() for m in env.split(",") if m.strip()]
        if not models and self.llm_model:
            models = [self.llm_model]
        return models or ["devbox-default"]

    # ── box allocation over existing v3 session APIs ──────────────
    def _box_key(self, ident: dict) -> str:
        # keyed by stable identity, NOT the bearer — OIDC refresh
        # rotates access tokens and would orphan the box otherwise
        return f"{ident.get('org_id') or ''}:{ident.get('user_id') or 'anon'}"

    def _box_state(self, ident: dict) -> dict:
        box = self.state.get("box") or {}
        key = self._box_key(ident)
        if key in box:
            return dict(box[key])
        # migrate the legacy token-keyed entry (pre-identity keys):
        # only when exactly one box exists do we attribute it to this
        # caller — otherwise we can't tell which token maps here
        legacy = {k: v for k, v in box.items()
                  if ":" not in k or k.count(":") > 1}
        if len(legacy) == 1 and ident.get("user_id"):
            entry = next(iter(legacy.values()))
            box[self._box_key(ident)] = entry
            self.state.data["box"] = box
            self.state.save()
            return dict(entry)
        return {}

    def _save_box_state(self, ident: dict, box: dict) -> None:
        boxes = dict(self.state.data.get("box") or {})
        boxes[self._box_key(ident)] = box
        self.state.data["box"] = boxes
        self.state.save()

    def _provision_payload(self, ident: dict, org_id: str,
                           box: dict) -> dict[str, str]:
        payload = {
            "GROKBOT_GATEWAY_TOKEN": box["gateway_token"],
            "GROKBOT_INFERENCE_CREDENTIAL": box["inference_credential"],
            "GROKBOT_RUNTIME_URL": os.environ.get(
                "GROKBOT_RUNTIME_URL", ""),
            "GROKBOT_USER_JSON": json.dumps({
                "user_id": ident.get("user_id") or "",
                "org_id": org_id or "",
                "name": ident.get("name") or "",
            }),
        }
        for key, env_name in (
                ("GROKBOT_LLM_BASE_URL", "GROKBOT_LLM_BASE_URL"),
                ("GROKBOT_LLM_API_KEY", "GROKBOT_LLM_API_KEY"),
                ("GROKBOT_LLM_MODEL", "GROKBOT_LLM_MODEL")):
            value = os.environ.get(env_name, "")
            if value:
                payload[key] = value
        asset_token = os.environ.get("GROKBOT_ASSET_TOKEN", "")
        if asset_token:
            payload["DEVBOX_HARNESS_LLM_TOKEN"] = asset_token
            payload["DEVBOX_HARNESS_LLM_OPENAI_BASE_URL"] = (
                os.environ.get(
                    "GROKBOT_ASSET_ORIGIN",
                    "https://app.devinai.net/internal/harness-llm/v1"))
        return payload

    def _provision_relay(self, api: DevBoxApi, session_id: str,
                         path: str,
                         payload: dict | None = None) -> tuple[int, dict]:
        cap = self._mint_capability(api, session_id, 7813)
        if cap is None:
            return 0, {}
        base = str(cap.get("url") or "").rstrip("/")
        token = str(cap.get("token") or "")
        if not base or not token:
            return 0, {}
        headers = {
            "x-anyrun-network-token": token,
            "user-agent": "devbox-bot/0.1",
            "accept": "application/json",
        }
        data = None
        if payload is not None:
            data = json.dumps(payload).encode()
            headers["content-type"] = "application/json"
        request = urllib.request.Request(
            f"{base}{path}", data=data,
            method="POST" if payload is not None else "GET",
            headers=headers)
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                raw = response.read().decode("utf-8", "replace")
                status = response.status
        except urllib.error.HTTPError as exc:
            raw = exc.read().decode("utf-8", "replace")
            status = exc.code
        except (OSError, TimeoutError):
            return 0, {}
        try:
            body = json.loads(raw)
        except ValueError:
            body = {}
        return status, body if isinstance(body, dict) else {}

    def _send_stage_message(self, api: DevBoxApi, org_id: str,
                            session_id: str) -> None:
        api.send_message(org_id, session_id, BOX_STAGE_PROMPT)

    def _provision_listener(self, api: DevBoxApi, org_id: str,
                            session_id: str, box: dict,
                            ident: dict) -> str:
        payload = self._provision_payload(ident, org_id, box)
        deadline = time.monotonic() + PROVISION_WAIT_S
        reachable = False
        while time.monotonic() < deadline:
            status, body = self._provision_relay(
                api, session_id, "/health")
            if status == 200:
                reachable = True
                state = body.get("state")
                if state == "awaiting":
                    post_status, _post_body = self._provision_relay(
                        api, session_id, "/provision", payload)
                    if post_status in (202, 409):
                        log.info("box %s provisioned via listener",
                                 session_id)
                        return "provision"
                    if post_status not in (0, 502, 503, 504):
                        raise ConnectError(
                            "unavailable",
                            f"box provision request failed: HTTP "
                            f"{post_status}")
                elif state in {"provisioning", "provisioned"}:
                    return "provision"
                else:
                    raise ConnectError(
                        "unavailable",
                        "box listener returned an invalid health state")
            elif status not in (0, 502, 503, 504):
                reachable = True
                raise ConnectError(
                    "unavailable",
                    f"box listener health failed: HTTP {status}")
            time.sleep(5)
        if reachable:
            raise ConnectError(
                "unavailable",
                "box listener remained reachable but did not become ready")
        self._send_stage_message(api, org_id, session_id)
        log.warning("box %s provisioning fallback path=agent", session_id)
        return "agent"

    def _ensure_box(self, ctx: dict) -> dict:
        api = self.api_for(ctx)
        ident = self._identity(api)
        org_id = ident.get("org_id") or self._default_org(api)
        box = self._box_state(ident)
        session_id = box.get("session_id", "")
        if session_id:
            try:
                sess = api.get_session(org_id, session_id)
                status = str(sess.get("status", "")).lower()
                if status not in {"finished", "error", "expired",
                                  "deleted", "archived"}:
                    coords = self._gateway_coords(api, session_id, box)
                    if coords:
                        coords["provision_path"] = box.get(
                            "provision_path", "provision")
                        return coords
                    if not box.get("inference_credential"):
                        box["inference_credential"] = (
                            secrets.token_urlsafe(32))
                    box["provision_path"] = self._provision_listener(
                        api, org_id, session_id, box, ident)
                    self._save_box_state(ident, box)
                    coords = self._await_gateway(api, session_id, box)
                    coords["provision_path"] = box["provision_path"]
                    log.info("box %s ready via %s", session_id,
                             box["provision_path"])
                    return coords
            except Exception as exc:  # noqa: BLE001 — stale box state is fine
                log.debug("existing box check failed: %s", exc)
        # create a fresh box session
        gateway_token = secrets.token_urlsafe(32)
        inference_credential = secrets.token_urlsafe(32)
        secret_ids = self._llm_secret_ids(api, org_id)
        box = {
            "session_id": "",
            "gateway_token": gateway_token,
            "inference_credential": inference_credential,
            "created_at": time.time(),
        }
        payload = self._provision_payload(ident, org_id, box)
        session_secrets = [{
            "key": key,
            "value": value,
            "is_sensitive": any(
                marker in key for marker in ("TOKEN", "CREDENTIAL", "API_KEY")),
        } for key, value in payload.items()]
        session = api.create_session(
            org_id, BOX_PROMPT,
            title="Grok Bot box",
            tags=[BOX_TAG],
            image_family="Debian_12",
            session_secrets=session_secrets,
            secret_ids=secret_ids or None,
        )
        session_id = session.get("session_id") or session.get(
            "devin_id") or ""
        if not session_id:
            raise ConnectError("internal",
                               "session create returned no id")
        box["session_id"] = session_id
        self._save_box_state(ident, box)
        box["provision_path"] = self._provision_listener(
            api, org_id, session_id, box, ident)
        self._save_box_state(ident, box)
        coords = self._await_gateway(api, session_id, box)
        coords["provision_path"] = box["provision_path"]
        log.info("box %s ready via %s", session_id, box["provision_path"])
        return coords

    def _default_org(self, api: DevBoxApi) -> str:
        try:
            body = api.call("GET", "/v3/organizations")
        except Exception:  # noqa: BLE001 — org listing is best-effort
            body = {}
        orgs = body.get("organizations") if isinstance(
            body, dict) else body
        if orgs:
            return orgs[0].get("org_id") or orgs[0].get("id") or ""
        return ""

    def _llm_secret_ids(self, api: DevBoxApi, org_id: str) -> list[str]:
        try:
            secrets_list = api.list_secrets(org_id)
        except Exception:  # noqa: BLE001 — secrets are optional
            return []
        wanted = {"GROKBOT_LLM_BASE_URL", "GROKBOT_LLM_API_KEY",
                  "GROKBOT_LLM_MODEL"}
        ids = []
        for s in secrets_list if isinstance(secrets_list, list) else []:
            if s.get("key") in wanted or s.get("name") in wanted:
                ids.append(s.get("secret_id") or s.get("id") or "")
        return [i for i in ids if i]

    def _mint_capability(self, api: DevBoxApi, session_id: str,
                         port: int = 1340) -> dict | None:
        try:
            body = api.call(
                "PUT", f"/api/preview-link/{session_id}"
                       f"?local_port={port}")
        except Exception:  # noqa: BLE001 — capability mint may 404/503
            return None
        if isinstance(body, dict) and body.get("url"):
            return body
        return None

    def _gateway_coords(self, api: DevBoxApi, session_id: str,
                        box: dict) -> dict | None:
        cap = self._mint_capability(api, session_id)
        if cap is None:
            return None
        url = str(cap.get("url") or "")
        token = str(cap.get("token") or "")
        if not url or not token:
            return None
        parsed = urllib.parse.urlparse(url)
        base = f"{parsed.scheme}://{parsed.netloc}"
        # health-check through the relay with the capability
        try:
            req = urllib.request.Request(
                f"{base}/health",
                headers={"x-anyrun-network-token": token,
                         "user-agent": "devbox-bot/0.1"})
            with urllib.request.urlopen(req, timeout=15) as resp:
                if resp.status != 200:
                    return None
        except Exception:  # noqa: BLE001 — health probe may fail early
            return None
        ident = self._identity(api)
        return {
            "cluster": "devbox",
            "tenant_id": ident.get("org_id") or "",
            "pod_id": session_id,
            "network_token": token,
            "gateway_url": base,
            "gateway_token": box.get("gateway_token", ""),
            "provision_path": box.get("provision_path", ""),
        }

    def _await_gateway(self, api: DevBoxApi, session_id: str,
                       box: dict) -> dict:
        deadline = time.monotonic() + BOX_DEADLINE_S
        last_err = ""
        while time.monotonic() < deadline:
            coords = self._gateway_coords(api, session_id, box)
            if coords:
                return coords
            last_err = "gateway not ready"
            time.sleep(5)
        raise ConnectError("unavailable",
                           f"box did not become ready: {last_err}")

    def _recreate_box(self, ctx: dict) -> dict:
        api = self.api_for(ctx)
        ident = self._identity(api)
        org_id = ident.get("org_id") or self._default_org(api)
        box = self._box_state(ident)
        session_id = box.get("session_id", "")
        if session_id:
            try:
                api.delete_session(org_id, session_id)
            except Exception as exc:  # noqa: BLE001 — recreate is best-effort
                log.debug("box session delete failed: %s", exc)
            self._save_box_state(ident, {})
        self._ensure_box(ctx)
        return {"started": True, "reason": "recreated"}

    def _box_run_state(self, ctx: dict) -> dict:
        api = self.api_for(ctx)
        ident = self._identity(api)
        org_id = ident.get("org_id") or self._default_org(api)
        box = self._box_state(ident)
        session_id = box.get("session_id", "")
        state = 1  # ABSENT
        if session_id:
            try:
                sess = api.get_session(org_id, session_id)
                status = str(sess.get("status", "")).lower()
                state = 3 if status not in {
                    "finished", "error", "expired", "deleted",
                    "archived"} else 1
            except Exception:  # noqa: BLE001 — missing session = ABSENT
                state = 1
        return {"state": state}

    # ── plain HTTP routes (login, oauth, health) ──────────────────
    def extra_routes(self) -> dict:
        def _json(handler, status, obj):
            body = json.dumps(obj).encode()
            handler.send_response(status)
            handler.send_header("content-type", "application/json")
            handler.send_header("content-length", str(len(body)))
            handler.end_headers()
            handler.wfile.write(body)

        def _redirect(handler, location):
            handler.send_response(302)
            handler.send_header("location", location)
            handler.send_header("content-length", "0")
            handler.end_headers()

        def health(h):
            _json(h, 200, {"ok": True})

        def login_deep_control(h):
            query = urllib.parse.parse_qs(
                urllib.parse.urlparse(h.path).query)
            uuid = query.get("uuid", [""])[0]
            challenge = query.get("challenge", [""])[0]
            if h.command == "POST":
                length = int(h.headers.get("content-length") or 0)
                raw = h.rfile.read(length)
                ctype = h.headers.get("content-type", "")
                if "application/json" in ctype:
                    try:
                        body = json.loads(raw or b"{}")
                    except ValueError:
                        body = {}
                else:
                    body = {k: v[0] for k, v in
                            urllib.parse.parse_qs(
                                raw.decode("utf-8", "replace")).items()}
                uuid = uuid or str(body.get("uuid", ""))
                challenge = challenge or str(body.get("challenge", ""))
            if not uuid or not challenge:
                _json(h, 400, {"error": "uuid and challenge required"})
                return
            # headless mode: DEVBOX_API_KEY → complete instantly
            if self.api_key:
                with self._pending_lock:
                    self.pending[uuid] = {
                        "challenge": challenge,
                        "tokens": self._headless_tokens(),
                        "expires": time.time() + PENDING_TTL,
                    }
                _json(h, 200, {"ok": True, "headless": True})
                return
            verifier = _b64url(secrets.token_bytes(32))
            with self._pending_lock:
                self.pending[uuid] = {
                    "challenge": challenge,
                    "verifier": verifier,
                    "tokens": None,
                    "expires": time.time() + PENDING_TTL,
                }
            _redirect(h, DevBoxApi.authorize_url(
                self.auth_origin, {
                    "response_type": "code",
                    "client_id": OIDC_CLIENT_ID,
                    "redirect_uri":
                        f"http://127.0.0.1:{self.port}/callback",
                    "scope": OIDC_SCOPE,
                    "state": uuid,
                    "audience": self.devbox_origin,
                    "code_challenge": _b64url(hashlib.sha256(
                        verifier.encode()).digest()),
                    "code_challenge_method": "S256",
                }))

        def callback(h):
            query = urllib.parse.parse_qs(
                urllib.parse.urlparse(h.path).query)
            uuid = query.get("state", [""])[0]
            code = query.get("code", [""])[0]
            with self._pending_lock:
                pending = self.pending.get(uuid)
            if pending is None or not code:
                _json(h, 400, {"error": "unknown state or missing code"})
                return
            status, body = DevBoxApi.oidc_token(self.auth_origin, {
                "grant_type": "authorization_code",
                "client_id": OIDC_CLIENT_ID,
                "code": code,
                "redirect_uri":
                    f"http://127.0.0.1:{self.port}/callback",
                "code_verifier": pending["verifier"],
            })
            if status != 200 or not isinstance(body, dict):
                _json(h, 502, {"error": "token exchange failed",
                               "status": status})
                return
            pending["tokens"] = {
                "accessToken": body.get("access_token", ""),
                "refreshToken": body.get("refresh_token", ""),
                "authId": "",
            }
            h.send_response(200)
            h.send_header("content-type", "text/html")
            html = (b"<html><body><h2>Signed in to Grok Bot.</h2>"
                    b"<p>You can close this window.</p></body></html>")
            h.send_header("content-length", str(len(html)))
            h.end_headers()
            h.wfile.write(html)

        def poll(h):
            query = urllib.parse.parse_qs(
                urllib.parse.urlparse(h.path).query)
            uuid = query.get("uuid", [""])[0]
            verifier = query.get("verifier", [""])[0]
            if not uuid:
                _json(h, 400, {"error": "uuid required"})
                return
            with self._pending_lock:
                pending = self.pending.get(uuid)
            if pending is None or pending.get("consumed"):
                _json(h, 404, {"error": "not_found"})
                return
            if time.time() > pending.get("expires", 0):
                _json(h, 404, {"error": "expired"})
                return
            if not _pkce_ok(pending["challenge"], verifier):
                _json(h, 403, {"error": "invalid_grant"})
                return
            tokens = pending.get("tokens")
            if not tokens:
                _json(h, 404, {"error": "pending"})
                return
            pending["consumed"] = True
            _json(h, 200, tokens)

        def oauth_token(h):
            length = int(h.headers.get("content-length") or 0)
            raw = h.rfile.read(length)
            ctype = h.headers.get("content-type", "")
            if "application/json" in ctype:
                try:
                    form = json.loads(raw or b"{}")
                except ValueError:
                    _json(h, 400, {"error": "malformed"})
                    return
            else:
                form = {k: v[0] for k, v in urllib.parse.parse_qs(
                    raw.decode("utf-8", "replace")).items()}
            if str(form.get("client_id", "")) not in CLIENT_IDS \
                    and str(form.get("client_id", "")) != OIDC_CLIENT_ID:
                _json(h, 404, {"error": "not_found"})
                return
            if form.get("grant_type") == "refresh_token":
                refresh = str(form.get("refresh_token", ""))
                if self.api_key and refresh.startswith("gbr_local_"):
                    mapped = self.state.get("refresh", refresh)
                    if mapped is None:
                        _json(h, 200, {"shouldLogout": True})
                        return
                    tokens = self._headless_tokens()
                    _json(h, 200, {
                        "access_token": tokens.get("accessToken", ""),
                        "refresh_token": tokens.get("refreshToken", refresh),
                        "token_type": "Bearer",
                        "expires_in": 3600,
                    })
                    return
                status, body = DevBoxApi.oidc_token(
                    self.auth_origin, {
                        "grant_type": "refresh_token",
                        "client_id": str(form.get(
                            "client_id", OIDC_CLIENT_ID)),
                        "refresh_token": refresh,
                    })
                if status != 200 or not isinstance(body, dict):
                    _json(h, 200, {"shouldLogout": True})
                    return
                _json(h, 200, {
                    "access_token": body.get("access_token", ""),
                    "refresh_token": body.get("refresh_token",
                                              refresh)})
                return
            _json(h, 400, {"error": "unsupported_grant_type"})

        def exchange_api_key(h):
            auth = h.headers.get("authorization", "")
            key = auth.removeprefix("Bearer ").strip()
            if not key:
                _json(h, 401, {"error": "unauthenticated"})
                return
            _json(h, 200, self._headless_tokens(api_key=key))

        def unhandled_list(h):
            from .connect import unhandled_methods
            _json(h, 200, {"unhandled": unhandled_methods()})

        return {
            ("GET", "/healthz"): health,
            ("GET", "/loginDeepControl"): login_deep_control,
            ("POST", "/loginDeepControl/approve"): login_deep_control,
            ("GET", "/callback"): callback,
            ("GET", "/auth/poll"): poll,
            ("POST", "/oauth/token"): oauth_token,
            ("POST", "/auth/exchange_user_api_key"): exchange_api_key,
            ("GET", "/debug/unhandled"): unhandled_list,
        }

    def _headless_tokens(self, api_key: str | None = None) -> dict:
        key = api_key or self.api_key
        if not key:
            raise ConnectError("unauthenticated",
                               "DEVBOX_API_KEY not configured")
        now = int(time.time())
        api = DevBoxApi(self.devbox_origin, key)
        ident = self._identity(api)
        access = _hs256_jwt(self.local_secret, {
            "iss": f"devbox-bot-local/{self.port}",
            "sub": ident.get("user_id") or "devbox-local",
            "aud": "grok-bot",
            "email": ident.get("email") or "",
            "org_id": ident.get("org_id") or "",
            "iat": now, "exp": now + 3600,
        })
        refresh = f"gbr_local_{secrets.token_urlsafe(24)}"
        self.state.data.setdefault("tokens", {})[access] = key
        self.state.data.setdefault("tokens", {})[refresh] = key
        self.state.data.setdefault("refresh", {})[refresh] = access
        self.state.save()
        return {"accessToken": access, "refreshToken": refresh,
                "authId": ident.get("user_id") or ""}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="devbox_bot.desktop")
    parser.add_argument("--port", type=int, default=7811)
    parser.add_argument("--devbox-origin", default=DEFAULT_ORIGIN)
    parser.add_argument("--auth-origin", default=DEFAULT_AUTH_ORIGIN)
    parser.add_argument("--state-file", default=None)
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=(logging.DEBUG if os.environ.get("GROKBOT_DEBUG")
               else logging.INFO),
        format="%(asctime)s %(name)s %(levelname)s %(message)s")
    backend = DesktopBackend(
        port=args.port, devbox_origin=args.devbox_origin,
        auth_origin=args.auth_origin,
        state_path=Path(args.state_file) if args.state_file else None)
    server = serve(backend.router(), "127.0.0.1", args.port,
                   extra_routes=backend.extra_routes())
    log.info("devbox-bot desktop backend on http://127.0.0.1:%d "
             "(headless=%s)", args.port, bool(backend.api_key))
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
