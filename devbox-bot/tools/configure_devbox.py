#!/usr/bin/env python3
"""Idempotent DevBox configuration for the Grok Bot box.

Creates (or updates) the org secrets the in-box inference endpoint reads
and an org-target blueprint that stages and starts the provisioning
listener. The listener is inert until box credentials arrive over its
relayed guest port.

Usage:
    python plugin/bot/tools/configure_devbox.py \
        --org org-devbox \
        [--origin https://app.devinai.net]

Credentials are read from DEVBOX_API_KEY and GROKBOT_LLM_* environment
variables.

Only existing DevBox APIs are used; nothing is written to app/.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path

START_BOX = (Path(__file__).resolve().parent.parent
             / "box" / "start-box.sh").read_text()
PROVISION = (Path(__file__).resolve().parent.parent
             / "box" / "provision.py").read_text()


def _request(method: str, url: str, key: str,
             body: dict | None = None) -> tuple[int, object]:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers={
        "authorization": f"Bearer {key}",
        "content-type": "application/json",
        "user-agent": "devbox-bot-configure/0.1",
        "accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.status, json.loads(
                resp.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as exc:
        try:
            payload = json.loads(
                exc.read().decode("utf-8", "replace"))
        except ValueError:
            payload = exc.read()[:300]
        return exc.code, payload


def _secret_ids(api_key: str, origin: str, org_id: str,
                wanted: dict[str, str]) -> list[str]:
    status, body = _request(
        "GET", f"{origin}/api/{org_id}/secrets", api_key)
    if status != 200:
        raise RuntimeError(f"secret list failed ({status})")
    secrets_list = (
        body if isinstance(body, list)
        else body.get("secrets") if isinstance(body, dict)
        else None)
    if not isinstance(secrets_list, list):
        raise TypeError("unexpected secret list response")
    existing = {s.get("key") or s.get("name"): s for s in secrets_list
                if isinstance(s, dict)}
    ids = []
    for name, value in wanted.items():
        if not value:
            continue
        if name in existing:
            secret = existing[name]
            secret_id = secret.get("secret_id") or secret.get("id")
            if not secret_id:
                raise RuntimeError(f"secret {name} has no identifier")
            status, _ = _request(
                "PUT", f"{origin}/api/{org_id}/secrets/{secret_id}",
                api_key, {"key": name, "value": value,
                          "type": (secret.get("type")
                                   or secret.get("secret_type")
                                   or "key-value"),
                          "note": secret.get("note") or "",
                          "sensitive": secret.get("sensitive", True)})
            if status not in (200, 201):
                raise RuntimeError(
                    f"secret {name} update failed ({status})")
            ids.append(secret_id)
            print(f"secret {name}: updated ({secret_id})")
            continue
        status, body = _request(
            "POST", f"{origin}/api/{org_id}/secrets", api_key,
            {"key": name, "value": value})
        if status in (200, 201):
            sid = ((body.get("secret_id") or body.get("id") or "")
                   if isinstance(body, dict) else "")
            if not sid:
                raise RuntimeError(
                    f"secret {name} create returned no identifier")
            print(f"secret {name}: created ({sid})")
            ids.append(sid)
        else:
            raise RuntimeError(f"secret {name} create failed ({status})")
    return [i for i in ids if i]


BLUEPRINT_NAME = "grok-bot-box"


START_BOX_PATH = "$HOME/.grok-bot-box/start-box.sh"
PROVISION_PATH = "$HOME/.grok-bot-box/provision.py"

_STAGE_COMMAND = (
    "true || false; mkdir -p \"$HOME/.grok-bot-box\" && cat > "
    + START_BOX_PATH + " <<'__START_BOX_EOF__'\n"
    + "{script}\n__START_BOX_EOF__\n"
    + "cat > " + PROVISION_PATH + " <<'__PROVISION_EOF__'\n"
    + "{provision}\n__PROVISION_EOF__\n"
    + "chmod 700 " + START_BOX_PATH + " " + PROVISION_PATH
    + " && mkdir -p \"$HOME/.grok-bot-box/logs\""
    + " && (GROKBOT_PROVISION_IDLE_S={idle_s} nohup setsid python3 "
    + PROVISION_PATH
    + " >>\"$HOME/.grok-bot-box/logs/provision.log\" 2>&1 "
    + "</dev/null &)"
)


def _stage_command(idle_s: int = 900) -> str:
    return _STAGE_COMMAND.format(
        script=START_BOX, provision=PROVISION, idle_s=idle_s)


def _blueprint(api_key: str, origin: str, org_id: str,
               provision_idle_s: int = 900) -> str:
    """Create/update an org blueprint that starts the guest listener."""
    prefix = (f"{origin}/v3beta1/organizations/{org_id}"
              f"/snapshot-setup/blueprints")
    startup_commands = [{"command": _stage_command(provision_idle_s),
                         "startup_command_type": "dep"}]
    status, body = _request("GET", prefix, api_key)
    blueprints = (body.get("blueprints") if isinstance(body, dict)
                  else body) or []
    existing = next(
        (b for b in blueprints
         if b.get("type") == "org" and b.get("name") == BLUEPRINT_NAME),
        None)
    if existing is not None:
        blueprint_id = existing["blueprint_id"]
        status, body = _request(
            "PUT", f"{prefix}/{blueprint_id}", api_key,
            {"startup_commands": startup_commands})
        if status in (200, 201):
            print(f"blueprint {blueprint_id}: updated startup command")
            return blueprint_id
        print(f"blueprint update failed ({status}): "
              f"{str(body)[:160]}", file=sys.stderr)
        return ""
    status, body = _request(
        "POST", prefix, api_key,
        {"name": BLUEPRINT_NAME,
         "startup_commands": startup_commands})
    if status in (200, 201) and isinstance(body, dict):
        blueprint_id = body.get("blueprint_id") or ""
        print(f"blueprint {blueprint_id}: created (org target)")
        return blueprint_id
    print(f"blueprint create failed ({status}): {str(body)[:160]}",
          file=sys.stderr)
    return ""


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="configure_devbox")
    parser.add_argument("--org", required=True)
    parser.add_argument("--origin",
                        default="https://app.devinai.net")
    parser.add_argument("--provision-idle-s", type=int, default=900)
    args = parser.parse_args(argv)
    api_key = os.environ.get("DEVBOX_API_KEY", "")
    if not api_key:
        parser.error("DEVBOX_API_KEY environment variable is required")
    if args.provision_idle_s < 1:
        parser.error("--provision-idle-s must be positive")

    origin = args.origin.rstrip("/")
    print(f"org={args.org} origin={origin}")
    ids = _secret_ids(api_key, origin, args.org, {
        "GROKBOT_LLM_BASE_URL": os.environ.get(
            "GROKBOT_LLM_BASE_URL", ""),
        "GROKBOT_LLM_API_KEY": os.environ.get(
            "GROKBOT_LLM_API_KEY", ""),
        "GROKBOT_LLM_MODEL": os.environ.get(
            "GROKBOT_LLM_MODEL", "")})
    blueprint_id = _blueprint(
        api_key, origin, args.org, args.provision_idle_s)
    print(json.dumps({
        "org": args.org, "secret_ids": ids,
        "blueprint_id": blueprint_id}))
    return 0 if blueprint_id else 1


if __name__ == "__main__":
    sys.exit(main())
