"""devbox_bot — standalone Grok Bot ↔ DevBox backend bridge.

``devbox_bot.desktop`` serves the Grok Bot desktop's "core domain"
(SAND_BACKEND_URL / CURSOR_API_BASE_URL / CURSOR_WEBSITE_URL) on
127.0.0.1 and translates every client RPC into existing DevBox HTTP APIs
— OIDC login at auth.devinai.net, v3 sessions, preview-link capabilities
— without any DevBox code change.

``devbox_bot.box`` runs inside the box VM as the in-box inference
endpoint the host-main process calls for credential renewal and
InferenceService.Stream.
"""

__all__ = ["box", "codec", "connect", "desktop", "devbox_api", "inference"]
