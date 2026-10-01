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
import secrets
import sys
import time
import urllib.parse

from .codec import ConnectError
from .connect import ConnectRouter, serve
from .inference import stream_inference

log = logging.getLogger("devbox_bot.box")

CREDENTIAL_TTL_S = 3600


def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


class BoxBackend:
    def __init__(self, port: int = 7812) -> None:
        self.port = port
        self.gateway_token = os.environ.get("GROKBOT_GATEWAY_TOKEN", "")
        self.credential = os.environ.get(
            "GROKBOT_INFERENCE_CREDENTIAL", "")
        self.llm_base_url = os.environ.get("GROKBOT_LLM_BASE_URL", "")
        self.llm_api_key = os.environ.get("GROKBOT_LLM_API_KEY", "")
        self.llm_model = os.environ.get("GROKBOT_LLM_MODEL", "")
        self.model_map = json.loads(
            os.environ.get("GROKBOT_LLM_MODEL_MAP", "{}") or "{}")
        self.signing_secret = secrets.token_bytes(32)
        self.session_id = os.environ.get("DEVIN_SESSION_ID", "box")

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

        return r

    def extra_routes(self) -> dict:
        def _json(handler, status, obj):
            body = json.dumps(obj).encode()
            handler.send_response(status)
            handler.send_header("content-type", "application/json")
            handler.send_header("content-length", str(len(body)))
            handler.end_headers()
            handler.wfile.write(body)

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

        return {
            ("GET", "/healthz"): health,
            ("POST", "/sand-box/inference-credential"):
                _credential_exchange,
            ("POST", "/sand-box/local-exec-daemon-credential"):
                _credential_exchange,
        }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="devbox_bot.box")
    parser.add_argument("--port", type=int, default=7812)
    args = parser.parse_args(argv)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s")
    backend = BoxBackend(port=args.port)
    server = serve(backend.router(), "127.0.0.1", args.port,
                   extra_routes=backend.extra_routes())
    log.info("devbox-bot box backend on http://127.0.0.1:%d",
             args.port)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
