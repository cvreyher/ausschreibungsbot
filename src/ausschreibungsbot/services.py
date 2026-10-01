import os
import sys
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

from .browser import Browser
from .config import Settings
from .db import DB
from .mcp_session import McpSession


@dataclass
class Services:
    """Gemeinsame Abhängigkeiten für Agents, Pipeline und Bot."""

    settings: Settings
    db: DB
    browser: Browser
    # OrderCity-MCP (Produkte bepreisen) – None, wenn kein ORDERCITY_API_KEY gesetzt ist
    ordercity: McpSession | None = None
    # Wird vom Telegram-Bot gesetzt: meldet relevante neue Ausschreibungen als Karte.
    notify_tenders: Callable[[list[dict]], Awaitable[None]] | None = field(default=None)


def ordercity_session(settings: Settings) -> McpSession | None:
    """Startet den eigenen OrderCity-MCP-Server (stdio) mit demselben Python wie der Bot."""
    if not settings.ordercity_api_key:
        return None
    env = {
        **os.environ,
        "ORDERCITY_API_KEY": settings.ordercity_api_key,
        "ORDERCITY_BASE_URL": settings.ordercity_base_url,
        "ANGEBOT_AUFSCHLAG_PROZENT": str(settings.angebot_aufschlag_prozent),
        "ANGEBOT_MONTAGE_PRO_STUECK_EUR": str(settings.angebot_montage_pro_stueck_eur),
    }
    return McpSession(
        "ordercity",
        {"transport": "stdio", "command": sys.executable, "args": ["-m", "ausschreibungsbot.ordercity"], "env": env},
    )
