"""Live-Protokoll im Telegram-Chat: zeigt, wie die Agents untereinander kommunizieren.

Ein LangChain-Callback-Handler hängt am Delegate-Lauf. Callbacks wandern automatisch in alle
verschachtelten Sub-Agents mit; `lc_agent_name` verrät, welcher Agent gerade handelt.
Die Zeilen landen in EINER Telegram-Nachricht, die laufend ergänzt wird (gedrosselt).
"""

import asyncio
import json
import logging
import re
import time
from urllib.parse import urlparse

from aiogram import Bot
from langchain_core.callbacks import AsyncCallbackHandler
from langchain_core.callbacks.manager import adispatch_custom_event

log = logging.getLogger(__name__)

MODES = ("aus", "kurz", "voll")
TG_LIMIT = 3800
FLUSH_EVERY_S = 2.0

AGENTS = {
    "decocity_delegate": "🤖 Delegate",
    "recherche": "🔎 Recherche",
    "abgabe": "📤 Abgabe",
    "kalkulation": "🧮 Kalkulation",
    "mengen": "📏 Mengen",
    "konfigurator": "🛠 Konfigurator",
}

# Tools, hinter denen ein anderer Agent steckt → werden als Übergabe "A → B" angezeigt
HANDOFFS = {
    "ausschreibungen_suchen": "🛰 Scout",
    "analyst_beauftragen": "📊 Analyst",
    "vollbewertung_beauftragen": "📊 Analyst",
    "recherche_beauftragen": "🔎 Recherche",
    "unterlagen_sichern": "📂 Unterlagen",
    "kalkulation_beauftragen": "🧮 Kalkulation",
    "angebot_beauftragen": "✍️ Angebot",
    "abgabe_durchfuehren": "📤 Abgabe",
    "positionen_ermitteln": "📏 Mengen",
    "position_bepreisen": "🛠 Konfigurator",
    "kalkulation_abschliessen": "🧾 Rechenkern",
}
# erscheinen ohnehin als eigene Nachricht im Chat
SILENT = {"frage_an_nutzer", "freigabe_anfordern"}

TOOL_ICONS = {
    "seite_lesen": "🌐", "datei_herunterladen": "📥", "dokument_lesen": "📄", "zip_entpacken": "🗜",
    "downloads_auflisten": "📁", "hersteller_auflisten": "🏭", "produkte_suchen": "🏭", "produkt_konfigurator": "🏭",
    "konfiguration_berechnen": "🏭", "stoffe_suchen": "🏭", "kategorien": "🏭", "profil_merken": "🧠",
}


async def melde(text: str) -> None:
    """Fortschrittsmeldung aus beliebigem Code (z.B. Downloads, OCR) ins Live-Protokoll."""
    try:
        await adispatch_custom_event("fortschritt", {"text": text})
    except Exception:
        pass  # außerhalb eines Agent-Laufs (z.B. geplanter Scout) gibt es kein Protokoll


def _short(value, n: int = 90) -> str:
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
    text = " ".join(text.split())
    return text if len(text) <= n else text[: n - 1] + "…"


def _args(inputs: dict | None, input_str: str) -> str:
    if not inputs:
        try:
            inputs = json.loads(input_str.replace("'", '"'))
        except Exception:
            return _short(input_str, 70)
    if not isinstance(inputs, dict):
        return _short(inputs, 70)
    parts = []
    for k, v in inputs.items():
        if v in (None, "", [], {}):
            continue
        if isinstance(v, str) and v.startswith("http"):
            u = urlparse(v)
            v = u.netloc + (u.path if len(u.path) < 40 else "…" + u.path[-35:])
        parts.append(f"{k}={_short(v, 60)}")
    return ", ".join(parts)[:160]


def _handoff_args(tool: str, inputs: dict | None) -> str | None:
    """Kompakte Darstellung für häufige Übergaben."""
    if not inputs:
        return None
    if tool == "position_bepreisen":
        masse = f" {inputs.get('breite_mm')}×{inputs.get('hoehe_mm')} mm" if inputs.get("breite_mm") else ""
        return f"Pos. {inputs.get('nr')} {inputs.get('bezeichnung', '')} – {inputs.get('menge')}×{masse}"
    return None


def _ergebnis(text: str) -> str:
    """Rückmeldung eines Agents kürzen: Speicherpfade weg, bei Kalkulationen nur die Summen."""
    text = re.sub(r"\(Gespeichert unter [^)]*\)", "", text).strip()
    summen = re.findall(r"Summe (?:netto|brutto): \**([\d.,]+ €)", text)
    if len(summen) == 2 and "| Pos." in text:
        return f"Summe netto {summen[0]}, brutto {summen[1]}"
    return _short(text, 160)


class LiveLog:
    """Eine Telegram-Nachricht, die laufend ergänzt wird."""

    def __init__(self, bot: Bot, chat_id: int, title: str):
        self.bot, self.chat_id, self.title = bot, chat_id, title
        self.start = time.monotonic()
        self.lines: list[str] = []
        self.message_id: int | None = None
        self.sent_text = ""
        self.offset = 0  # ab dieser Zeile gehört alles zur aktuellen Nachricht
        self._dirty = asyncio.Event()
        self._task = asyncio.create_task(self._loop())

    def add(self, line: str) -> None:
        t = int(time.monotonic() - self.start)
        self.lines.append(f"{t // 60}:{t % 60:02d} {line}")
        self._dirty.set()

    def _text(self) -> str:
        return f"📜 {self.title}\n" + "\n".join(self.lines[self.offset :])

    async def _flush(self) -> None:
        text = self._text()
        if len(text) > TG_LIMIT and self.message_id:
            # aktuelle Nachricht ist voll → neue beginnen
            self.offset = len(self.lines) - 1
            self.message_id, self.sent_text = None, ""
            text = self._text()
        text = text[:TG_LIMIT]
        if text == self.sent_text:
            return
        try:
            if self.message_id is None:
                msg = await self.bot.send_message(self.chat_id, text, disable_notification=True)
                self.message_id = msg.message_id
            else:
                await self.bot.edit_message_text(text, chat_id=self.chat_id, message_id=self.message_id)
            self.sent_text = text
        except Exception as e:  # z.B. Flood-Control – beim nächsten Mal erneut
            log.debug("Live-Protokoll: %s", e)

    async def _loop(self) -> None:
        while True:
            await self._dirty.wait()
            self._dirty.clear()
            await self._flush()
            await asyncio.sleep(FLUSH_EVERY_S)

    async def close(self, footer: str | None = None) -> None:
        if footer and self.lines:
            self.add(footer)
        self._task.cancel()
        if self.lines:
            await self._flush()


class TraceHandler(AsyncCallbackHandler):
    """Übersetzt Tool-Aufrufe der Agents in Protokollzeilen."""

    def __init__(self, live: LiveLog, mode: str = "kurz"):
        self.live = live
        self.voll = mode == "voll"
        self._runs: dict = {}  # run_id → (Aufrufer, Tool)

    def _caller(self, metadata: dict | None) -> str:
        name = (metadata or {}).get("lc_agent_name") or "?"
        return AGENTS.get(name, f"🤖 {name}")

    async def on_tool_start(self, serialized, input_str, *, run_id, parent_run_id=None, tags=None, metadata=None, inputs=None, **kw):
        tool = (serialized or {}).get("name") or kw.get("name") or "?"
        if tool in SILENT:
            return
        caller = self._caller(metadata)
        self._runs[run_id] = (caller, tool)
        if tool in HANDOFFS:
            args = _handoff_args(tool, inputs) or _args(inputs, input_str)
            self.live.add(f"{caller} → {HANDOFFS[tool]}" + (f": {args}" if args else ""))
        elif self.voll:
            icon = TOOL_ICONS.get(tool, "🌐" if tool.startswith("browser_") else "🔧")
            self.live.add(f"   {caller} · {icon} {tool.removeprefix('browser_')}({_args(inputs, input_str)})")

    async def on_tool_end(self, output, *, run_id, **kw):
        caller, tool = self._runs.pop(run_id, (None, None))
        if tool in HANDOFFS:
            text = getattr(output, "content", output)
            if isinstance(text, list):
                text = " ".join(str(b.get("text", b)) if isinstance(b, dict) else str(b) for b in text)
            self.live.add(f"{HANDOFFS[tool]} → {caller}: {_ergebnis(str(text))}")

    async def on_tool_error(self, error, *, run_id, **kw):
        caller, tool = self._runs.pop(run_id, (None, None))
        if tool and (tool in HANDOFFS or self.voll):
            self.live.add(f"⚠️ {tool} fehlgeschlagen: {_short(str(error), 120)}")

    async def on_custom_event(self, name, data, *, run_id, tags=None, metadata=None, **kw):
        if name == "fortschritt":
            self.live.add(f"   {data.get('text', '')}")
