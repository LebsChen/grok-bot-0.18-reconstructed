#!/usr/bin/env python3
"""Idempotent DevBox configuration for the Grok Bot box.

Creates (or updates) the org secrets the in-box inference endpoint reads
and an org-target blueprint whose startup command runs
``plugin/bot/box/start-box.sh``.  The org blueprint is the documented
fallback when attaching/cloning the Grok Bot repo is not viable; the
script is a no-op unless ``GROKBOT_GATEWAY_TOKEN`` is set (only box
sessions carry that session secret), so it is safe on every org session.

Usage:
    python plugin/bot/tools/configure_devbox.py \
        --org org-devbox \
        --api-key <dv_/cog_ key> \
        [--origin https://app.devinai.net] \
        [--llm-base-url ... --llm-api-key ... --llm-model ...]

Only existing DevBox APIs are used; nothing is written to app/.
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.request
from pathlib import Path

START_BOX = (Path(__file__).resolve().parent.parent
             / "box" / "start-box.sh").read_text()


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
    existing = {s.get("key") or s.get("name"): s.get("secret_id")
                or s.get("id") for s in (
                    body if isinstance(body, list)
                    else body.get("secrets", []))}
    ids = []
    for name, value in wanted.items():
        if not value:
            continue
        if name in existing:
            ids.append(existing[name])
            print(f"secret {name}: already present "
                  f"({existing[name]})")
            continue
        status, body = _request(
            "POST", f"{origin}/api/{org_id}/secrets", api_key,
            {"key": name, "value": value})
        if status in (200, 201):
            sid = body.get("secret_id") or body.get("id") or ""
            print(f"secret {name}: created ({sid})")
            ids.append(sid)
        else:
            print(f"secret {name}: create failed "
                  f"({status}): {str(body)[:120]}",
                  file=sys.stderr)
    return [i for i in ids if i]


BLUEPRINT_NAME = "grok-bot-box"


START_BOX_PATH = "$HOME/.grok-bot-box/start-box.sh"

# DevBox session secrets are injected into the guest only as environment
# of *agent* exec tool calls — blueprint `dep` startup commands run
# through bare sandbox.exec and cannot see them.  So the startup command
# only stages the script; the box session's prompt asks the agent to run
# it (agent execs carry GROKBOT_* secrets in env), which keeps the
# GROKBOT_GATEWAY_TOKEN no-op gate intact for non-box sessions.
_STAGE_COMMAND = (
    # DevBox prepends `cd <repos_dir> &&` to org startup commands; the
    # repos dir does not exist on repo-less box sessions, which would
    # kill the whole && chain.  `true || false;` neutralizes that chain
    # whether the cd succeeds or fails.
    "true || false; mkdir -p \"$HOME/.grok-bot-box\" && cat > "
    + START_BOX_PATH.replace("$HOME", "$HOME")
    + " <<'__START_BOX_EOF__'\n"
    + "{script}\n__START_BOX_EOF__\n"
    + "chmod +x " + START_BOX_PATH
)


def _stage_command() -> str:
    return _STAGE_COMMAND.format(script=START_BOX)


def _blueprint(api_key: str, origin: str, org_id: str) -> str:
    """Create/update an org blueprint with a `dep` startup command that
    stages start-box.sh into the guest."""
    prefix = (f"{origin}/v3beta1/organizations/{org_id}"
              f"/snapshot-setup/blueprints")
    startup_commands = [{"command": _stage_command(),
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
    parser.add_argument("--api-key", required=True)
    parser.add_argument("--origin",
                        default="https://app.devinai.net")
    parser.add_argument("--llm-base-url", default="")
    parser.add_argument("--llm-api-key", default="")
    parser.add_argument("--llm-model", default="")
    args = parser.parse_args(argv)

    origin = args.origin.rstrip("/")
    print(f"org={args.org} origin={origin}")
    ids = _secret_ids(args.api_key, origin, args.org, {
        "GROKBOT_LLM_BASE_URL": args.llm_base_url,
        "GROKBOT_LLM_API_KEY": args.llm_api_key,
        "GROKBOT_LLM_MODEL": args.llm_model})
    blueprint_id = _blueprint(args.api_key, origin, args.org)
    print(json.dumps({
        "org": args.org, "secret_ids": ids,
        "blueprint_id": blueprint_id}))
    return 0 if blueprint_id else 1


if __name__ == "__main__":
    sys.exit(main())
