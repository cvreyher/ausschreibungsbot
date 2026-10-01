"""Scout: holt Ausschreibungen deterministisch (ohne LLM) über den RSS-Feed von service.bund.de."""

import asyncio
import logging
import re
import xml.etree.ElementTree as ET
from email.utils import parsedate_to_datetime
from urllib.parse import urlencode

import httpx
from bs4 import BeautifulSoup

from .config import Settings

log = logging.getLogger(__name__)

SEARCH_URL = "https://www.service.bund.de/Content/DE/Ausschreibungen/Suche/Formular.html"
HEADERS = {"User-Agent": "Mozilla/5.0 (Decocity Ausschreibungsbot)"}


def feed_url(term: str, settings: Settings) -> str:
    params = {
        "nn": "4641482",
        "type": "0",
        "submit": "Finden",
        "resultsPerPage": "100",
        "sortOrder": "dateOfIssue_dt desc",
        "jobsrss": "true",
        "templateQueryString": term,
    }
    if settings.search_opengeo_id:
        params |= {
            "city_zipcode": settings.search_city,
            "solr_opengeo_id": settings.search_opengeo_id,
            "ambit_distance": str(settings.search_radius_km),
        }
    return f"{SEARCH_URL}?{urlencode(params)}"


def _field(text: str, label: str) -> str | None:
    m = re.search(rf"{label}:\s*(.+?)(?=\s*(?:Erfüllungsort|Vergabestelle|Angebotsfrist|Veröffentlichungsende):|$)", text)
    return m.group(1).strip() if m else None


def parse_feed(xml_text: str, term: str) -> list[dict]:
    root = ET.fromstring(xml_text)
    hits = []
    for item in root.iter("item"):
        desc = BeautifulSoup(item.findtext("description") or "", "html.parser").get_text(" ", strip=True)
        guid = (item.findtext("guid") or item.findtext("link") or "").strip()
        pub = item.findtext("pubDate")
        hits.append(
            {
                "guid": guid,
                "title": (item.findtext("title") or "").strip(),
                "url": guid,
                "place": _field(desc, "Erfüllungsort"),
                "authority": _field(desc, "Vergabestelle"),
                "deadline": _field(desc, "Angebotsfrist"),
                "published": parsedate_to_datetime(pub).isoformat() if pub else None,
                "search_term": term,
            }
        )
    return hits


async def search(settings: Settings, terms: list[str] | None = None) -> list[dict]:
    """Durchsucht alle Suchbegriffe und gibt eindeutige Treffer zurück."""
    terms = terms or settings.terms
    seen: dict[str, dict] = {}
    async with httpx.AsyncClient(headers=HEADERS, timeout=30, follow_redirects=True) as client:
        for term in terms:
            try:
                r = await client.get(feed_url(term, settings))
                r.raise_for_status()
                for hit in parse_feed(r.text, term):
                    seen.setdefault(hit["guid"], hit)
            except Exception as e:  # ein kaputter Suchbegriff soll den Lauf nicht abbrechen
                log.warning("Suche '%s' fehlgeschlagen: %s", term, e)
            await asyncio.sleep(1)  # höflich zum Server
    return list(seen.values())


async def fetch_details(url: str) -> dict:
    """Liest die Kurzinfo einer Detailseite und den Link zur Bekanntmachung auf der Vergabeplattform."""
    async with httpx.AsyncClient(headers=HEADERS, timeout=30, follow_redirects=True) as client:
        r = await client.get(url)
        r.raise_for_status()
    soup = BeautifulSoup(r.text, "html.parser")
    main = soup.select_one("article#main") or soup
    kurzinfo = {}
    for dt in main.select("dl dt"):
        dd = dt.find_next_sibling("dd")
        if dd:
            value = re.sub(r"\s+", " ", dd.get_text(" ", strip=True)).split(" Karte anschauen")[0]
            kurzinfo[dt.get_text(" ", strip=True)] = value
    notice_url = None
    for a in main.select("a[href^=http]"):
        if (a.get("title") or "").startswith("Bekanntmachung") or "Bekanntmachung" in a.get_text():
            notice_url = a["href"]
            break
    # Die eigentliche Leistungsbeschreibung steht erst auf der Vergabeplattform (notice_url).
    return {"kurzinfo": kurzinfo, "notice_url": notice_url}
