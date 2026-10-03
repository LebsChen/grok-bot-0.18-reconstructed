"""devbox_bot.handlers — account/dashboard Connect handlers shared by
desktop.py and box.py.

host-main inside the box calls the same ``aiserver.v1.DashboardService``
methods the desktop shell does; without them it only sees empty defaults.
``get_identity(ctx)`` must return ``{"user_id", "org_id", "name",
"email"}`` (email may be empty).
"""

from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path

log = logging.getLogger(__name__)

_FEATURE_GATE_MANIFEST = json.loads(
    Path(__file__).with_name("feature_gates.json").read_text(
        encoding="utf-8"))
_FEATURE_GATE_DEFAULTS = _FEATURE_GATE_MANIFEST.get("gates")
if (not isinstance(_FEATURE_GATE_DEFAULTS, dict)
        or len(_FEATURE_GATE_DEFAULTS) != 608
        or any(not isinstance(value, bool)
               for value in _FEATURE_GATE_DEFAULTS.values())):
    raise ValueError("feature_gates.json must contain 608 boolean gates")

DEVBOX_GATE_OVERRIDES = frozenset({
    "sand_agent_network",
    "sand_multiplayer",
    "sand_teach_by_demonstration",
    "sand_memory_dreaming",
    "sand_browser_use_subagent",
    "sand_usage_page",
    "sand_action_audit_logs",
    "sand_get_grok_bot_ios",
    "sand_special_settings",
    "sand_auto_disk_saver",
    "sand_computer_use_unicode_typing",
})


def _statsig_feature_gates() -> dict[str, bool]:
    gates = dict(_FEATURE_GATE_DEFAULTS)
    gates.update({name: True for name in DEVBOX_GATE_OVERRIDES})
    for entry in os.environ.get("GROKBOT_FEATURE_GATES", "").split(","):
        entry = entry.strip()
        if not entry:
            continue
        enabled = not entry.startswith("!")
        name = entry[1:].strip() if not enabled else entry
        if name not in gates:
            log.warning("ignoring unknown GROKBOT_FEATURE_GATES name %r",
                        name)
            continue
        gates[name] = enabled
    return gates


def register(router, get_identity, catalog) -> None:
    """Register DashboardService / AiService account handlers.

    ``catalog`` returns the list of model names for AvailableModels.
    """
    @router.unary("aiserver.v1.DashboardService", "GetMe")
    def _get_me(_req, ctx):
        ident = get_identity(ctx)
        first, _, last = (ident.get("name") or "").partition(" ")
        return {
            "auth_id": ident.get("user_id") or "devbox",
            "email": ident.get("email") or "",
            "first_name": first or ident.get("name") or "",
            "last_name": last,
        }

    @router.unary("aiserver.v1.DashboardService", "GetTeams")
    def _get_teams(_req, ctx):
        ident = get_identity(ctx)
        return {"teams": [{
            "name": ident.get("org_id") or "devbox",
            "id": 1, "privacy_mode_forced": True,
            "subscription_status": "active"}]}

    @router.unary("aiserver.v1.DashboardService", "GetUserPrivacyMode")
    def _privacy(_req, _ctx):
        return {"privacy_mode": 1, "is_enforced_by_team": True}

    @router.unary("aiserver.v1.DashboardService",
                  "GetTeamAdminSettingsOrEmptyIfNotInTeam")
    def _team_admin(_req, _ctx):
        return {}

    @router.unary("aiserver.v1.DashboardService", "GetManagedSkills")
    def _managed_skills(_req, _ctx):
        return {"skills": []}

    @router.unary("aiserver.v1.DashboardService", "GetEffectiveUserPlugins")
    def _plugins(_req, _ctx):
        return {"plugins": [], "marketplaces": []}

    @router.unary("aiserver.v1.DashboardService",
                  "GetAvailableMcpServers")
    def _mcp(_req, _ctx):
        return {"servers": []}

    @router.unary("aiserver.v1.GrokBotService", "GetSandBoxRunState")
    def _run_state(_req, _ctx):
        return {"state": 3}  # RUNNING

    @router.unary("aiserver.v1.AnalyticsService", "BootstrapStatsig")
    def _statsig(_req, ctx):
        generated_at_ms = int(time.time() * 1000)
        user_id = (get_identity(ctx) or {}).get("user_id")
        if not isinstance(user_id, str) or not user_id.strip():
            raise ValueError("BootstrapStatsig requires an identity user_id")
        feature_gates = {
            name: {
                "name": name,
                "value": value,
                "rule_id": "devbox",
                "id_type": "userID",
                "secondary_exposures": [],
            }
            for name, value in sorted(_statsig_feature_gates().items())
        }
        config = {
            "feature_gates": feature_gates,
            "dynamic_configs": {},
            "layer_configs": {},
            "param_stores": {},
            "has_updates": True,
            "hash_used": "none",
            "time": generated_at_ms,
            "user": {"userID": user_id},
            "response_format": "init-v1",
            "generator": "devbox",
        }
        return {
            "config": json.dumps(config, separators=(",", ":")),
            "generated_at_ms": generated_at_ms,
        }

    @router.unary("aiserver.v1.AiService", "AvailableModels")
    def _models(_req, _ctx):
        models = catalog()
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

    @router.unary("aiserver.v1.AiService", "GetDefaultModel")
    def _default_model(_req, _ctx):
        models = catalog()
        return {"model": models[0] if models else "",
                "default_model": models[0] if models else ""}


def models_from_env(env_name: str = "GROKBOT_MODELS",
                    llm_model: str = "") -> list[str]:
    env = os.environ.get(env_name) or ""
    models = [m.strip() for m in env.split(",") if m.strip()]
    if not models and llm_model:
        models = [llm_model]
    return models or ["devbox-default"]
