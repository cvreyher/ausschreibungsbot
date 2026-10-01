"""Gemeinsamer Playwright-MCP-Browser.

Die Session bleibt für die gesamte Laufzeit offen, damit Logins und Tabs zwischen
Tool-Aufrufen erhalten bleiben. Ein Lock sorgt dafür, dass immer nur ein Agent den
Browser steuert.
"""

import asyncio

from .config import Settings
from .mcp_session import McpSession


class Browser(McpSession):
    def __init__(self, settings: Settings):
        args = [
            "-y",
            "@playwright/mcp@latest",
            "--user-data-dir",
            str((settings.data_dir / "browser-profile").resolve()),
            "--output-dir",
            str(settings.downloads_dir.resolve()),
            "--viewport-size",
            "1280,900",
            "--image-responses",
            "omit",
        ]
        if settings.playwright_headless:
            args.append("--headless")
        super().__init__("playwright", {"transport": "stdio", "command": "npx", "args": args})
        self.lock = asyncio.Lock()
