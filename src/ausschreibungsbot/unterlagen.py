"""Unterlagen einer Ausschreibung sichern: Bekanntmachungs-PDFs und Vergabeunterlagen herunterladen,
ZIPs entpacken, in der Datenbank registrieren und den Text extrahieren (inkl. OCR).

Läuft deterministisch (ohne LLM) vor und nach dem Recherche-Agent – damit die Dokumente garantiert
gesichert werden, egal was der Agent tut.
"""

import hashlib
import logging
import re
import shutil
import uuid
import zipfile
from pathlib import Path
from urllib.parse import unquote, urljoin, urlparse

import httpx
from bs4 import BeautifulSoup

from . import ocr
from .scout import HEADERS
from .trace import melde
from .services import Services

log = logging.getLogger(__name__)

MAX_DOWNLOAD_BYTES = 300 * 1024 * 1024
MAX_FILES = 40
MAX_PAGES = 6
MAX_TEXT_CHARS = 400_000  # Text pro Dokument in DB/Extrakt
EXTRAKT = ".extrakt.txt"  # Textdatei neben jedem Dokument (für dokument_lesen)

FILE_URL = re.compile(r"\.(pdf|zip|docx?|xlsx?|x8\d|d8\d|p8\d)(?:$|[?;#])", re.I)
FILE_TEXT = re.compile(r"\b(pdf|zip|herunterladen|download)\b", re.I)
NAV_TEXT = re.compile(r"vergabeunterlagen|unterlagen|dokumente|documents|bekanntmachung", re.I)
ALL_ZIP = re.compile(r"alle\b.*\bzip|zip.*\balle\b|gesamt.*zip|komplett.*zip", re.I)
BEKANNTMACHUNG = re.compile(r"bekanntmachung|notice", re.I)
VERFAHREN = re.compile(r"verfahrensangaben|bekanntmachung|ausschreibungsdetails|details", re.I)
SKIP = re.compile(r"^(mailto:|javascript:|tel:|#)", re.I)


def tender_dir(services: Services, tender_id: int) -> Path:
    d = services.settings.bids_dir / str(tender_id) / "unterlagen"
    d.mkdir(parents=True, exist_ok=True)
    return d


def classify(name: str) -> str:
    n = name.lower()
    if n.endswith(".zip"):
        return "archiv"
    if re.search(r"bekanntmachung|notice|auftragsbekanntmachung|veröffentlichung", n):
        return "bekanntmachung"
    if re.search(r"(^|[^a-z])lv([^a-z]|$)|leistungsverzeichnis|leistungsbeschreibung|x8\d$|d8\d$", n):
        return "lv"
    return "unterlage"


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


async def download(client: httpx.AsyncClient, url: str, target_dir: Path) -> Path:
    """Lädt eine Datei herunter. Dateiname aus Content-Disposition oder URL."""
    async with client.stream("GET", url) as r:
        r.raise_for_status()
        name = None
        if cd := r.headers.get("content-disposition"):
            m = re.search(r"filename\*?=(?:UTF-8'')?\"?([^\";]+)", cd)
            name = unquote(m.group(1)) if m else None
        name = name or unquote(Path(urlparse(str(r.url)).path).name) or "download"
        if "." not in name and "pdf" in r.headers.get("content-type", ""):
            name += ".pdf"
        name = re.sub(r"[^\w.\- ]", "_", name).strip() or "download"
        target = target_dir / name
        size = 0
        with target.open("wb") as f:
            async for chunk in r.aiter_bytes():
                size += len(chunk)
                if size > MAX_DOWNLOAD_BYTES:
                    f.close()
                    target.unlink(missing_ok=True)
                    raise ValueError("Datei größer als 300 MB – abgebrochen")
                f.write(chunk)
    return target


async def _page_links(client: httpx.AsyncClient, url: str) -> tuple[str, list[tuple[str, str]]]:
    r = await client.get(url)
    r.raise_for_status()
    soup = BeautifulSoup(r.text, "html.parser")
    links = []
    for a in soup.select("a[href]"):
        href = a["href"].strip()
        if SKIP.match(href):
            continue
        label = " ".join(filter(None, [a.get_text(" ", strip=True), a.get("title") or ""]))
        links.append((label, urljoin(str(r.url), href)))
    return str(r.url), links


async def sichern(services: Services, tender: dict) -> str:
    """Crawlt Bekanntmachung (+ Unterseiten zu Unterlagen) und lädt alle Dateien herunter."""
    target = tender_dir(services, tender["id"])
    start = [u for u in (tender.get("notice_url"), tender.get("url")) if u]
    seen_pages: set[str] = set()
    seen_files: set[str] = set()
    quellen: dict[str, str] = {}
    druckseiten: list[str] = []  # Seiten, die als Bekanntmachungs-PDF gedruckt werden, falls es kein PDF gibt
    fehler: list[str] = []
    async with httpx.AsyncClient(headers=HEADERS, timeout=60, follow_redirects=True) as client:
        queue = [(u, 0) for u in start]
        while queue and len(seen_pages) < MAX_PAGES:
            url, depth = queue.pop(0)
            if url in seen_pages:
                continue
            seen_pages.add(url)
            try:
                final_url, links = await _page_links(client, url)
            except Exception as e:
                fehler.append(f"Seite {url}: {e}")
                continue
            host = urlparse(final_url).netloc
            if depth == 0 and url == tender.get("notice_url"):
                druckseiten.append(final_url)
                druckseiten += [h for l, h in links if VERFAHREN.search(l) and urlparse(h).netloc == host][:2]
            files = [
                (label, href) for label, href in links
                if FILE_URL.search(href) or (FILE_TEXT.search(label) and urlparse(href).netloc == host)
            ]
            # Gibt es ein Gesamt-ZIP, reicht das (plus Bekanntmachungs-PDFs) – sonst lädt man alles doppelt.
            gesamt = [f for f in files if ALL_ZIP.search(f[0])]
            if gesamt:
                files = gesamt + [f for f in files if BEKANNTMACHUNG.search(f"{f[0]} {f[1]}")]
            for label, href in files:
                if href in seen_files or len(seen_files) >= MAX_FILES:
                    continue
                seen_files.add(href)
                try:
                    p = await download(client, href, target)
                    quellen[p.name] = href
                    log.info("Unterlage gesichert: %s", p.name)
                    await melde(f"📥 {p.name} ({p.stat().st_size // 1024} KB)")
                except Exception as e:
                    fehler.append(f"Download {href}: {e}")
            if depth == 0:
                for label, href in links:
                    if NAV_TEXT.search(label) and urlparse(href).netloc == host and (label, href) not in files:
                        queue.append((href, 1))
    hat_pdf = any(classify(n) == "bekanntmachung" and n.lower().endswith(".pdf") for n in quellen) or any(
        p.name.lower().startswith("bekanntmachung") for p in target.glob("*.pdf")
    )
    if not hat_pdf and druckseiten:
        try:
            await bekanntmachung_drucken(services, list(dict.fromkeys(druckseiten)), target)
        except Exception as e:
            fehler.append(f"Bekanntmachung als PDF drucken: {e}")
    neu = await registrieren(services, tender, quellen)
    msg = f"{len(seen_files)} Datei-Links gefunden, {neu} neue Unterlagen registriert."
    if fehler:
        msg += " Probleme: " + "; ".join(fehler[:5])
    return msg


async def bekanntmachung_drucken(services: Services, seiten: list[str], target: Path) -> None:
    """Viele Plattformen zeigen die Bekanntmachung nur als HTML. Dann druckt der Browser sie als PDF,
    damit immer eine Bekanntmachung in der Akte liegt."""
    outdir = services.settings.downloads_dir.resolve()  # Playwright MCP darf nur hierhin schreiben
    async with services.browser.lock:
        tools = {t.name: t for t in await services.browser.tools()}
        if "browser_pdf_save" not in tools:
            raise RuntimeError("Playwright MCP ohne PDF-Funktion (--caps pdf)")
        for i, url in enumerate(seiten, 1):
            await tools["browser_navigate"].ainvoke({"url": url})
            name = "Bekanntmachung.pdf" if i == 1 else f"Bekanntmachung_{i}.pdf"
            tmp = outdir / f"druck-{uuid.uuid4().hex}.pdf"
            result = str(await tools["browser_pdf_save"].ainvoke({"filename": str(tmp)}))
            if "### Error" in result or not tmp.exists():
                raise RuntimeError(result[:300])
            shutil.move(tmp, target / name)
            log.info("Bekanntmachung als PDF gedruckt: %s ← %s", name, url)
            await melde(f"🖨 {name} aus der Webseite erzeugt (kein PDF angeboten)")


def uebernehmen(services: Services, tender: dict, seit: float) -> int:
    """Verschiebt Dateien, die der Browser seit 'seit' heruntergeladen hat, in den Ordner der Ausschreibung."""
    target = tender_dir(services, tender["id"])
    moved = 0
    for p in services.settings.downloads_dir.iterdir():
        if p.is_file() and p.suffix != ".yml" and p.stat().st_mtime >= seit:
            shutil.move(p, target / p.name)
            moved += 1
    return moved


async def registrieren(services: Services, tender: dict, quellen: dict[str, str] | None = None) -> int:
    """Entpackt ZIPs, registriert alle Dateien in der DB und extrahiert Texte (inkl. OCR)."""
    db, s = services.db, services.settings
    root = tender_dir(services, tender["id"])

    for z in list(root.rglob("*.zip")):
        dest = z.with_suffix("")
        if dest.exists():
            continue
        try:
            with zipfile.ZipFile(z) as zf:
                if all((dest / m).resolve().is_relative_to(dest.resolve()) for m in zf.namelist()):
                    zf.extractall(dest)
        except zipfile.BadZipFile as e:
            log.warning("ZIP %s defekt: %s", z.name, e)

    neu = 0
    for p in sorted(root.rglob("*")):
        if not p.is_file() or p.name.endswith(EXTRAKT):
            continue
        doc_id = await db.add_document(
            {
                "tender_id": tender["id"],
                "filename": p.name,
                "path": str(p.resolve()),
                "source_url": (quellen or {}).get(p.name),
                "kind": classify(p.name),
                "sha256": _sha256(p),
                "size": p.stat().st_size,
            }
        )
        if doc_id:
            neu += 1
    if neu:
        await melde(f"🗂 {neu} Unterlagen in der Datenbank registriert – lese Texte …")

    budget = ocr.OcrBudget(s.ocr_max_seiten)
    for d in await db.list_documents(tender["id"]):
        if d["status"] != "neu" or d["kind"] == "archiv":
            continue
        path = Path(d["path"])
        try:
            if path.suffix.lower() == ".pdf":
                text, pages, ocr_pages, method = await ocr.pdf_text(s, path, budget)
            else:
                text, pages, ocr_pages = ocr.other_text(path), None, 0
                method = "datei" if text is not None else "keiner"
            if ocr_pages:
                await melde(f"🔤 OCR: {ocr_pages} gescannte Seite(n) aus {path.name} gelesen")
            if text and len(text) > MAX_TEXT_CHARS:
                # z.B. CAD-Pläne mit Tausenden Mini-Beschriftungen – für Recherche und Kalkulation wertlos
                text = text[:MAX_TEXT_CHARS] + f"\n[... gekürzt, Originaltext {len(text):,} Zeichen]"
            if text:
                path.with_name(path.name + EXTRAKT).write_text(text, encoding="utf-8")
            await db.update_document(
                d["id"], text=text, pages=pages, ocr_pages=ocr_pages, text_method=method,
                status="text" if text else "fehler", error=None if text else "kein Text extrahierbar",
            )
        except Exception as e:
            log.warning("Text aus %s fehlgeschlagen: %s", path.name, e)
            await db.update_document(d["id"], status="fehler", error=str(e)[:500])
    return neu


def uebersicht(docs: list[dict], root: Path) -> str:
    if not docs:
        return "(keine Unterlagen gesichert)"
    lines = []
    for d in docs:
        rel = Path(d["path"]).relative_to(root.resolve()) if Path(d["path"]).is_relative_to(root.resolve()) else d["filename"]
        info = f"{d['pages']} S." if d.get("pages") else ""
        if d.get("ocr_pages"):
            info += f", {d['ocr_pages']} per OCR"
        lines.append(f"- [{d['kind']}] {rel} ({info or d.get('text_method') or d['status']})")
    return "\n".join(lines)

