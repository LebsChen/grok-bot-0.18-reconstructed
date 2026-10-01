"""devbox_bot.handlers — account/dashboard Connect handlers shared by
desktop.py and box.py.

host-main inside the box calls the same ``aiserver.v1.DashboardService``
methods the desktop shell does; without them it only sees empty defaults.
``get_identity(ctx)`` must return ``{"user_id", "org_id", "name",
"email"}`` (email may be empty).
"""

from __future__ import annotations

import os
import time


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
    def _statsig(_req, _ctx):
        return {"config": "", "generated_at_ms": int(time.time() * 1000)}

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
