"""Textextraktion aus Unterlagen – mit OCR für gescannte PDF-Seiten.

PDF-Seiten mit eingebettetem Text werden direkt (kostenlos) gelesen. Seiten ohne Text (Scans)
werden gerendert und von einem Vision-Modell über OpenRouter transkribiert.
"""

import asyncio
import base64
import io
import logging
import re
import zipfile
from pathlib import Path

import pypdfium2 as pdfium
from bs4 import BeautifulSoup
from langchain_core.messages import HumanMessage
from pypdf import PdfReader

from .config import Settings
from .llm import chat_model

log = logging.getLogger(__name__)

OCR_PROMPT = (
    "Transkribiere den gesamten Text dieser Dokumentseite exakt und vollständig (meist Deutsch). "
    "Tabellen als Markdown-Tabellen. Positionsnummern, Mengen, Maße, Einheiten und Beträge unverändert übernehmen. "
    "Keine Erklärungen, keine Zusammenfassung – nur der Text der Seite. Ist die Seite leer, antworte mit [leer]."
)
OCR_PARALLEL = 4


class OcrBudget:
    """Begrenzt die Zahl der OCR-Seiten pro Lauf (Kostenschutz)."""

    def __init__(self, seiten: int):
        self.rest = seiten

    def nimm(self) -> bool:
        if self.rest <= 0:
            return False
        self.rest -= 1
        return True


def _render_jpeg(pdf_path: Path, index: int, scale: float = 2.0) -> bytes:
    pdf = pdfium.PdfDocument(pdf_path)
    try:
        image = pdf[index].render(scale=scale).to_pil().convert("RGB")
        image.thumbnail((2000, 2000))  # Obergrenze, damit die Bilder nicht zu groß werden
        buf = io.BytesIO()
        image.save(buf, format="JPEG", quality=85)
        return buf.getvalue()
    finally:
        pdf.close()


async def _ocr_page(settings: Settings, pdf_path: Path, index: int) -> str:
    jpeg = await asyncio.to_thread(_render_jpeg, pdf_path, index)
    llm = chat_model(settings, settings.model_ocr, temperature=0)
    msg = await llm.ainvoke(
        [
            HumanMessage(
                content=[
                    {"type": "text", "text": OCR_PROMPT},
                    {"type": "image", "base64": base64.b64encode(jpeg).decode(), "mime_type": "image/jpeg"},
                ]
            )
        ]
    )
    return msg.text.strip()


async def pdf_text(settings: Settings, pdf_path: Path, budget: OcrBudget) -> tuple[str, int, int, str]:
    """Gibt (Text mit Seitenmarkern, Seitenzahl, OCR-Seiten, Methode) zurück."""
    reader = PdfReader(pdf_path)
    texts: list[str] = []
    for page in reader.pages:
        try:
            texts.append(page.extract_text() or "")
        except Exception:
            texts.append("")

    leer = [i for i, t in enumerate(texts) if len(t.strip()) < settings.ocr_min_zeichen_pro_seite]
    ocr_count = 0
    if leer and settings.model_ocr:
        sem = asyncio.Semaphore(OCR_PARALLEL)

        async def one(i: int) -> None:
            nonlocal ocr_count
            if not budget.nimm():
                texts[i] = texts[i] or "[nicht per OCR gelesen – Seitenbudget erschöpft]"
                return
            async with sem:
                try:
                    texts[i] = await _ocr_page(settings, pdf_path, i)
                    ocr_count += 1
                except Exception as e:
                    log.warning("OCR %s Seite %d fehlgeschlagen: %s", pdf_path.name, i + 1, e)
                    texts[i] = texts[i] or f"[OCR fehlgeschlagen: {e}]"

        await asyncio.gather(*(one(i) for i in leer))

    n = len(texts)
    method = "ocr" if ocr_count and ocr_count == n else "gemischt" if ocr_count else "pdf-text"
    full = "\n".join(f"--- Seite {i} ---\n{t}" for i, t in enumerate(texts, 1))
    return full, n, ocr_count, method


def _xml_text(raw: bytes) -> str:
    return re.sub(r"\s+\n", "\n", BeautifulSoup(raw, "html.parser").get_text("\n", strip=True))


def other_text(path: Path) -> str | None:
    """Text aus Nicht-PDF-Dateien: DOCX, GAEB-XML (X83/X84 …), HTML, TXT/CSV. None = nicht lesbar."""
    suffix = path.suffix.lower()
    if suffix == ".docx":
        with zipfile.ZipFile(path) as z:
            return _xml_text(z.read("word/document.xml"))
    if re.fullmatch(r"\.(x8\d|xml|html?)", suffix):
        return _xml_text(path.read_bytes())
    if suffix in (".txt", ".csv", ".md", ".d83", ".d84", ".p83", ".p84"):
        return path.read_text(encoding="utf-8", errors="replace")
    return None
