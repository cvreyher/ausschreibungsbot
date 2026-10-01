from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

from .browser import Browser
from .config import Settings
from .db import DB


@dataclass
class Services:
    """Gemeinsame Abhängigkeiten für Agents, Pipeline und Bot."""

    settings: Settings
    db: DB
    browser: Browser
    # Wird vom Telegram-Bot gesetzt: meldet relevante neue Ausschreibungen als Karte.
    notify_tenders: Callable[[list[dict]], Awaitable[None]] | None = field(default=None)
