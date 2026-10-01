"""Thin stdlib client for existing DevBox HTTP APIs.

Only endpoints that already exist on https://app.devinai.net are used;
nothing here assumes DevBox code changes.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request

DEFAULT_ORIGIN = "https://app.devinai.net"
DEFAULT_AUTH_ORIGIN = "https://auth.devinai.net"


class ApiError(Exception):
    def __init__(self, status: int, body: str = "") -> None:
        super().__init__(f"HTTP {status}")
        self.status = status
        self.body = body


def _request(method: str, url: str, token: str | None = None,
             json_body: dict | list | None = None,
             form: dict | None = None, timeout: float = 30.0,
             headers: dict | None = None) -> tuple[int, dict | list | str]:
    data = None
    hdrs = {"accept": "application/json",
            "user-agent": "devbox-bot/0.1 (+https://devinai.net)",
            **(headers or {})}
    if json_body is not None:
        data = json.dumps(json_body).encode()
        hdrs["content-type"] = "application/json"
    elif form is not None:
        data = urllib.parse.urlencode(form).encode()
        hdrs["content-type"] = "application/x-www-form-urlencoded"
    if token:
        hdrs["authorization"] = f"Bearer {token}"
    req = urllib.request.Request(url, data=data, headers=hdrs,
                                 method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            text = resp.read().decode("utf-8", "replace")
            status = resp.status
    except urllib.error.HTTPError as exc:
        text = exc.read().decode("utf-8", "replace")
        status = exc.code
    try:
        body = json.loads(text)
    except ValueError:
        body = text
    return status, body


class DevBoxApi:
    def __init__(self, origin: str = DEFAULT_ORIGIN,
                 token: str | None = None) -> None:
        self.origin = origin.rstrip("/")
        self.token = token

    def call(self, method: str, path: str, **kw):
        status, body = _request(
            method, f"{self.origin}{path}", token=self.token, **kw)
        if status >= 400:
            raise ApiError(status, body if isinstance(body, str)
                           else json.dumps(body)[:300])
        return body

    # ── identity ──────────────────────────────────────────────────
    def current_membership(self) -> dict:
        return self.call("GET", "/api/users/current-membership")

    # ── sessions (v3) ─────────────────────────────────────────────
    def create_session(self, org_id: str, prompt: str, **kw) -> dict:
        body = {"prompt": prompt}
        body.update(kw)
        return self.call("POST", f"/v3/organizations/{org_id}/sessions",
                         json_body=body)

    def get_session(self, org_id: str, session_id: str) -> dict:
        return self.call("GET", f"/v3/organizations/{org_id}/sessions/{session_id}")

    def delete_session(self, org_id: str, session_id: str) -> None:
        self.call("DELETE", f"/v3/organizations/{org_id}/sessions/{session_id}")

    def send_message(self, org_id: str, session_id: str,
                     message: str) -> dict:
        return self.call(
            "POST", f"/v3/organizations/{org_id}/sessions/{session_id}/messages",
            json_body={"message": message})

    def list_sessions(self, org_id: str) -> dict:
        return self.call("GET", f"/v3/organizations/{org_id}/sessions")

    # ── preview link / gateway capability ─────────────────────────
    def mint_preview_link(self, session_id: str,
                          local_port: int) -> dict:
        return self.call(
            "PUT", f"/api/preview-link/{session_id}",
            json_body=None,
            timeout=30.0,
            headers=None)

    # ── org secrets + blueprints (admin) ──────────────────────────
    def list_secrets(self, org_id: str) -> list:
        body = self.call("GET", f"/api/{org_id}/secrets")
        return body if isinstance(body, list) else body.get(
            "secrets", body)

    def create_secret(self, org_id: str, key: str, value: str) -> dict:
        return self.call("POST", f"/api/{org_id}/secrets",
                         json_body={"key": key, "value": value})

    def create_blueprint(self, org_id: str, repo_name: str,
                         contents: str) -> dict:
        return self.call("POST", f"/api/{org_id}/blueprints",
                         json_body={"repo_name": repo_name,
                                    "contents": contents})

    def update_blueprint(self, org_id: str, blueprint_id: str,
                         contents: str) -> dict:
        return self.call(
            "PUT",
            f"/v3/organizations/{org_id}/snapshot-setup/blueprints/{blueprint_id}",
            json_body={"contents": contents})

    # ── OIDC token endpoint (auth.devinai.net) ────────────────────
    @staticmethod
    def oidc_token(auth_origin: str, form: dict,
                   timeout: float = 30.0) -> tuple[int, dict | str]:
        return _request(
            "POST",
            f"{auth_origin.rstrip('/')}/oauth/token",
            form=form, timeout=timeout)

    @staticmethod
    def authorize_url(auth_origin: str, params: dict) -> str:
        return (f"{auth_origin.rstrip('/')}/authorize?"
                + urllib.parse.urlencode(params))
