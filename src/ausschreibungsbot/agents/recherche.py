"""Sub-Agent "Recherche": steuert per Playwright MCP den Browser auf der Vergabeplattform,
liest Bekanntmachung und Vergabeunterlagen und erstellt einen strukturierten Bericht."""

import re
import time
import zipfile
from datetime import datetime
from pathlib import Path
from urllib.parse import unquote, urljoin, urlparse

import httpx
from bs4 import BeautifulSoup

from langchain.agents import create_agent
from langchain.agents.middleware import ClearToolUsesEdit, ContextEditingMiddleware, ModelCallLimitMiddleware
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage
from langchain_core.tools import tool
from pypdf import PdfReader

from ..llm import chat_model
from ..profile import load_profile
from ..services import Services
from ..scout import HEADERS
from .common import COMPANY, SECURITY_RULES, tender_brief, tool_errors

MAX_CHARS = 30_000
DOWNLOAD_WAIT_S = 60
MAX_MODEL_CALLS = 45
MAX_DOWNLOAD_BYTES = 300 * 1024 * 1024

RECHERCHE_PROMPT = f"""Du bist der Recherche-Agent von {COMPANY}. Du bedienst einen Browser über Playwright-Tools.

Deine Aufgabe: Öffne die Bekanntmachung auf der Vergabeplattform, lies sie vollständig und lade die
Vergabeunterlagen herunter, sofern das ohne Anmeldung möglich ist. Lies die Dokumente (PDF/ZIP) mit
den Datei-Tools. Arbeite zielgerichtet: Du hast ein Budget von etwa 30 Aktionen.

Vorgehen (in dieser Reihenfolge, das spart Zeit und Geld):
1. seite_lesen(Bekanntmachungs-URL) – liefert Text und Links der Seite.
2. Link zu den Vergabeunterlagen (oft Reiter "Vergabeunterlagen"/"Dokumente") ebenfalls mit seite_lesen öffnen.
3. Gibt es "Alle Dokumente als ZIP" oder einzelne PDF-Links: datei_herunterladen(url).
4. zip_entpacken, dann gezielt lesen: Aufforderung zur Angebotsabgabe, Bewerbungsbedingungen,
   Leistungsverzeichnis/Leistungsbeschreibung (Sonnenschutz-Positionen!), Eignungsnachweise.
5. Nur wenn seite_lesen keinen sinnvollen Inhalt liefert (JavaScript-Seite) oder Klicks nötig sind:
   Browser-Tools verwenden.
Wiederhole eine fehlgeschlagene Aktion höchstens zweimal – dann weiter mit dem Rest.
Wenn du genug weißt, schreibe den Bericht. Lieber ein Bericht mit Lücken als gar keiner.

Liefere am Ende einen Bericht auf Deutsch mit genau diesen Abschnitten:
1. Leistungsgegenstand (was genau, Mengen, Lose – welches Los passt zu Decocity?)
2. Fristen (Angebotsfrist, Bieterfragen, Ortsbesichtigung, Ausführungszeitraum)
3. Eignungskriterien und geforderte Nachweise/Formulare (als Liste)
4. Zuschlagskriterien
5. Abgabeweg (elektronisch? Plattform? Registrierung/Bietertool/Signatur nötig?)
6. Heruntergeladene Dateien (Pfade)
7. Offene Fragen an Decocity (Infos, die nur Decocity beantworten kann, z.B. Preise, Referenzen, Kapazität)

Bedienung des Browsers (nur falls nötig):
- Nach browser_navigate oder browser_click IMMER browser_snapshot aufrufen, um den Seiteninhalt zu sehen.
- Auf Vergabeplattformen gibt es meist Reiter wie "Verfahrensangaben" und "Vergabeunterlagen".
- Downloads laufen im Hintergrund weiter. Prüfe danach mit downloads_auflisten, unter welchem
  Namen die Datei gespeichert wurde (er kann vom angezeigten Namen abweichen), und nutze genau diesen.

Regeln:
- Melde dich NIRGENDS an, registriere dich nicht, gib keine Firmendaten in Formulare ein und
  sende nichts ab. Wenn etwas nur mit Login geht, schreibe das in den Bericht.
- Bestätige Cookie-Banner nur mit "nur notwendige" bzw. "ablehnen", falls möglich.
{SECURITY_RULES}"""

ABGABE_PROMPT = f"""Du bist der Abgabe-Agent von {COMPANY}. Du bedienst einen Browser über Playwright-Tools.
Der Nutzer hat die Abgabe des Angebots ausdrücklich freigegeben. Reiche das Angebot auf der
Vergabeplattform mit den bereitgestellten Dateien ein. Wenn eine Anmeldung, ein Bietertool oder eine
elektronische Signatur nötig ist, die du nicht hast, brich ab und beschreibe genau, was fehlt.
Schreibe am Ende in die letzte Zeile entweder "ABGABE_ERFOLGREICH" oder "ABGABE_NICHT_MOEGLICH".
{SECURITY_RULES}"""


def doc_tools(downloads: Path):
    root = downloads.resolve()

    def _norm(name: str) -> str:
        return re.sub(r"[^a-z0-9]", "", name.lower())

    def _files() -> list[Path]:
        files = [p for p in root.rglob("*") if p.is_file() and p.suffix != ".yml"]  # .yml = Browser-Snapshots
        return sorted(files, key=lambda p: p.stat().st_mtime, reverse=True)

    def _listing() -> str:
        return "\n".join(f"{p.relative_to(root)} ({p.stat().st_size // 1024} KB)" for p in _files()[:100]) or "(leer)"

    def _find(name: str) -> Path:
        """Findet eine Datei auch bei leicht abweichendem Namen (Playwright ersetzt z.B. '_' durch '-')
        und wartet kurz, falls der Download noch läuft."""
        deadline = time.monotonic() + DOWNLOAD_WAIT_S
        while True:
            p = (root / name).resolve()
            if not p.is_relative_to(root):
                raise ValueError("Pfad außerhalb des Download-Ordners")
            if p.is_file():
                return p
            target = _norm(Path(name).name)
            for f in _files():
                if _norm(f.name) == target:
                    return f
            if time.monotonic() > deadline:
                raise FileNotFoundError(f"'{name}' nicht gefunden. Vorhandene Dateien:\n{_listing()}")
            time.sleep(2)

    @tool
    def downloads_auflisten() -> str:
        """Listet alle heruntergeladenen Dateien (neueste zuerst)."""
        return _listing()

    @tool
    def zip_entpacken(dateiname: str) -> str:
        """Entpackt eine ZIP-Datei aus dem Download-Ordner und listet den Inhalt."""
        src = _find(dateiname)
        target = src.with_suffix("")
        with zipfile.ZipFile(src) as z:
            for member in z.namelist():
                if not (target / member).resolve().is_relative_to(target.resolve()):
                    return "Abgebrochen: ZIP enthält unsichere Pfade."
            z.extractall(target)
        return "\n".join(str(p.relative_to(root)) for p in target.rglob("*") if p.is_file())

    @tool
    def dokument_lesen(dateiname: str, seite_von: int = 1, seite_bis: int = 30) -> str:
        """Liest Text aus einer PDF- oder Textdatei im Download-Ordner (Seitenbereich bei PDFs)."""
        p = _find(dateiname)
        if p.suffix.lower() == ".pdf":
            reader = PdfReader(p)
            pages = reader.pages[max(seite_von - 1, 0) : seite_bis]
            text = "\n".join(f"--- Seite {i} ---\n{pg.extract_text() or ''}" for i, pg in enumerate(pages, seite_von))
            text = f"[{len(reader.pages)} Seiten insgesamt]\n{text}"
        else:
            text = p.read_text(encoding="utf-8", errors="replace")
        return text[:MAX_CHARS] + ("\n[... gekürzt]" if len(text) > MAX_CHARS else "")

    return [downloads_auflisten, zip_entpacken, dokument_lesen]


def web_tools(downloads: Path):
    """Leichte Alternativen zum Browser: Seite als Text lesen und Dateien direkt herunterladen."""
    root = downloads.resolve()

    @tool
    async def seite_lesen(url: str) -> str:
        """Lädt eine Webseite und gibt ihren Text und ihre Links zurück (viel günstiger als der Browser)."""
        async with httpx.AsyncClient(headers=HEADERS, timeout=45, follow_redirects=True) as client:
            r = await client.get(url)
        r.raise_for_status()
        soup = BeautifulSoup(r.text, "html.parser")
        for tag in soup(["script", "style", "noscript", "svg"]):
            tag.decompose()
        text = re.sub(r"\n\s*\n+", "\n", soup.get_text("\n", strip=True))
        links = []
        for a in soup.select("a[href]"):
            label = a.get_text(" ", strip=True) or a.get("title") or ""
            href = urljoin(str(r.url), a["href"])
            if label and href.startswith("http") and (label, href) not in links:
                links.append((label[:80], href))
        out = f"URL: {r.url}\n\n{text[:MAX_CHARS // 2]}\n\nLinks:\n" + "\n".join(f"- {l} → {h}" for l, h in links[:120])
        return out

    @tool
    async def datei_herunterladen(url: str) -> str:
        """Lädt eine Datei (PDF, ZIP, …) direkt in den Download-Ordner und gibt den gespeicherten Namen zurück."""
        async with httpx.AsyncClient(headers=HEADERS, timeout=180, follow_redirects=True) as client:
            async with client.stream("GET", url) as r:
                r.raise_for_status()
                name = None
                if cd := r.headers.get("content-disposition"):
                    m = re.search(r"filename\*?=(?:UTF-8'')?\"?([^\";]+)", cd)
                    name = unquote(m.group(1)) if m else None
                name = name or unquote(Path(urlparse(str(r.url)).path).name) or "download"
                name = re.sub(r"[^\w.\- ]", "_", name).strip() or "download"
                target = root / name
                size = 0
                with target.open("wb") as f:
                    async for chunk in r.aiter_bytes():
                        size += len(chunk)
                        if size > MAX_DOWNLOAD_BYTES:
                            raise ValueError("Datei größer als 300 MB – abgebrochen")
                        f.write(chunk)
        return f"Gespeichert als '{name}' ({size // 1024} KB)"

    return [seite_lesen, datei_herunterladen]


async def _run(services: Services, system_prompt: str, task: str, uploads: bool = False) -> str:
    s = services.settings
    blocked = {"browser_run_code_unsafe"} | (set() if uploads else {"browser_file_upload"})
    async with services.browser.lock:
        browser_tools = [t for t in await services.browser.tools() if t.name not in blocked]
        agent = create_agent(
            chat_model(s, s.model_subagent),
            tools=[*browser_tools, *doc_tools(s.downloads_dir), *web_tools(s.downloads_dir)],
            system_prompt=system_prompt,
            middleware=[
                tool_errors(),
                # Browser-Snapshots sind groß: alte Tool-Ergebnisse aus dem Kontext räumen
                ContextEditingMiddleware(edits=[ClearToolUsesEdit(trigger=60_000, keep=4)]),
                ModelCallLimitMiddleware(run_limit=MAX_MODEL_CALLS, exit_behavior="end"),
            ],
        )
        result = await agent.ainvoke({"messages": [HumanMessage(task)]}, {"recursion_limit": 500})
    return await _final_report(services, system_prompt, result["messages"])


async def _final_report(services: Services, system_prompt: str, messages: list) -> str:
    """Endet der Agent ohne Bericht (Budget erschöpft), wird aus dem Bisherigen ein Bericht erzwungen."""
    last = messages[-1]
    if isinstance(last, AIMessage) and not last.tool_calls and last.text.strip():
        return last.text
    if isinstance(last, AIMessage) and last.tool_calls:
        messages = messages[:-1]  # Tool-Aufruf ohne Ergebnis würde die API ablehnen
    s = services.settings
    msg = await chat_model(s, s.model_subagent).ainvoke(
        [
            SystemMessage(system_prompt),
            *messages,
            HumanMessage(
                "Dein Aktionsbudget ist aufgebraucht. Schreibe JETZT den Bericht mit allem, was du bisher "
                "herausgefunden hast. Markiere fehlende Punkte deutlich als 'nicht ermittelt'."
            ),
        ]
    )
    return "⚠️ Aktionsbudget erschöpft – Bericht mit dem bisherigen Stand.\n\n" + msg.text


def _save(services: Services, tender_id: int, filename: str, title: str, text: str) -> Path:
    folder = services.settings.bids_dir / str(tender_id)
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / filename
    with path.open("a", encoding="utf-8") as f:
        f.write(f"\n\n## {title} – {datetime.now():%d.%m.%Y %H:%M}\n\n{text}\n")
    return path


async def recherchiere(services: Services, tender: dict, auftrag: str) -> str:
    task = f"{tender_brief(tender)}\n\nAuftrag vom Delegate-Agent:\n{auftrag}"
    report = await _run(services, RECHERCHE_PROMPT, task)
    path = _save(services, tender["id"], "recherche.md", "Recherche", report)
    return f"{report}\n\n(Gespeichert unter {path})"


async def gib_ab(services: Services, tender: dict, angebot: str) -> str:
    folder = services.settings.bids_dir / str(tender["id"])
    files = "\n".join(str(p.resolve()) for p in folder.glob("*") if p.is_file())
    task = (
        f"{tender_brief(tender)}\n\nFirmenprofil:\n{load_profile(services.settings)}\n\n"
        f"Freigegebenes Angebot:\n{angebot}\n\nVerfügbare Dateien:\n{files}"
    )
    report = await _run(services, ABGABE_PROMPT, task, uploads=True)
    _save(services, tender["id"], "abgabe.md", "Abgabeversuch", report)
    return report
