"""Dauerhafte MCP-Session (stdio) für die gesamte Laufzeit des Bots.

Die Session lebt in einem eigenen Hintergrund-Task: anyio verlangt, dass sie im selben Task
geöffnet und geschlossen wird – Agent-Läufe und Shutdown laufen aber in anderen Tasks.
Stirbt der Server, wird er beim nächsten tools()-Aufruf neu gestartet.
"""

import asyncio
import logging
from typing import Any

from langchain_core.tools import BaseTool
from langchain_mcp_adapters.client import MultiServerMCPClient
from langchain_mcp_adapters.tools import load_mcp_tools

log = logging.getLogger(__name__)


class McpSession:
    def __init__(self, name: str, connection: dict[str, Any]):
        self.name = name
        self.connection = connection
        self._start_lock = asyncio.Lock()
        self._tools: list[BaseTool] | None = None
        self._task: asyncio.Task | None = None
        self._stop: asyncio.Event | None = None

    async def tools(self) -> list[BaseTool]:
        async with self._start_lock:
            if self._tools is None:
                await self._start()
        return self._tools

    async def _start(self) -> None:
        ready = asyncio.Event()
        self._stop = asyncio.Event()
        error: list[BaseException] = []

        async def owner() -> None:
            try:
                client = MultiServerMCPClient({self.name: self.connection})
                async with client.session(self.name) as session:
                    self._tools = await load_mcp_tools(session)
                    log.info("MCP %s gestartet (%d Tools)", self.name, len(self._tools))
                    ready.set()
                    await self._stop.wait()
            except Exception as e:
                error.append(e)
                log.exception("MCP %s beendet", self.name)
            finally:
                self._tools = None
                ready.set()

        self._task = asyncio.create_task(owner(), name=f"mcp-{self.name}")
        await ready.wait()
        if self._tools is None:
            raise RuntimeError(f"MCP {self.name} konnte nicht starten: {error[0] if error else 'unbekannt'}")

    async def close(self) -> None:
        if self._task and not self._task.done():
            self._stop.set()
            try:
                await asyncio.wait_for(self._task, timeout=15)
            except (TimeoutError, asyncio.CancelledError):
                self._task.cancel()
        self._task = None
