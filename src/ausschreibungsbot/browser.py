"""Gemeinsamer Playwright-MCP-Browser.

Die Session bleibt für die gesamte Laufzeit offen, damit Logins und Tabs zwischen
Tool-Aufrufen erhalten bleiben. Ein Lock sorgt dafür, dass immer nur ein Agent den
Browser steuert.
"""

import asyncio
import logging
from contextlib import AsyncExitStack

from langchain_core.tools import BaseTool
from langchain_mcp_adapters.client import MultiServerMCPClient
from langchain_mcp_adapters.tools import load_mcp_tools

from .config import Settings

log = logging.getLogger(__name__)


class Browser:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.lock = asyncio.Lock()
        self._stack: AsyncExitStack | None = None
        self._tools: list[BaseTool] | None = None

    async def tools(self) -> list[BaseTool]:
        if self._tools is None:
            await self._start()
        return self._tools

    async def _start(self) -> None:
        s = self.settings
        args = [
            "-y",
            "@playwright/mcp@latest",
            "--user-data-dir",
            str((s.data_dir / "browser-profile").resolve()),
            "--output-dir",
            str(s.downloads_dir.resolve()),
            "--viewport-size",
            "1280,900",
        ]
        if s.playwright_headless:
            args.append("--headless")
        client = MultiServerMCPClient(
            {"playwright": {"transport": "stdio", "command": "npx", "args": args}}
        )
        self._stack = AsyncExitStack()
        session = await self._stack.enter_async_context(client.session("playwright"))
        self._tools = await load_mcp_tools(session)
        log.info("Playwright MCP gestartet (%d Tools)", len(self._tools))

    async def close(self) -> None:
        if self._stack:
            await self._stack.aclose()
            self._stack = None
            self._tools = None
