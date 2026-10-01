"""Scout → Analyst: neue Ausschreibungen finden, bewerten und relevante melden."""

import logging

from . import scout
from .agents.analyst import bewerte
from .services import Services

log = logging.getLogger(__name__)


async def scout_und_bewerten(services: Services, terms: list[str] | None = None, notify: bool = True) -> list[dict]:
    """Gibt die neu gefundenen Ausschreibungen (inkl. Bewertung) zurück."""
    db, s = services.db, services.settings
    hits = await scout.search(s, terms)
    new: list[dict] = []
    for hit in hits:
        tender_id = await db.insert_tender(hit)
        if not tender_id:
            continue
        try:
            details = await scout.fetch_details(hit["url"])
            await db.update_tender(tender_id, details=details, notice_url=details.get("notice_url"))
            tender = await db.get_tender(tender_id)
            b = await bewerte(services, tender)
            status = "gemeldet" if b.passt and b.score >= s.min_score_notify else "irrelevant"
            summary = f"{b.zusammenfassung} {b.begruendung}".strip()
            if b.risiken:
                summary += " Risiken: " + "; ".join(b.risiken)
            await db.update_tender(tender_id, score=b.score, summary=summary, status=status)
        except Exception:
            log.exception("Bewertung von %s fehlgeschlagen", hit["url"])
            await db.update_tender(tender_id, status="gemeldet", summary="(Automatische Bewertung fehlgeschlagen)")
        new.append(await db.get_tender(tender_id))

    relevant = [t for t in new if t["status"] == "gemeldet"]
    log.info("Scout: %d Treffer, %d neu, %d relevant", len(hits), len(new), len(relevant))
    if notify and relevant and services.notify_tenders:
        await services.notify_tenders(relevant)
    return new
