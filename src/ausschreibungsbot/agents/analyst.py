"""Sub-Agent "Analyst".

- bewerte():        schnelle Vorbewertung anhand der Kurzinfo (Scout-Treffer) – Jev oder LLM
- vollbewertung():  Go/No-Go-Bewertung der gesamten Ausschreibung inkl. Vergabeunterlagen –
                    Entscheidungen von Jev, Texte vom LLM
"""

import asyncio
import logging
from typing import Literal

from langchain_core.messages import HumanMessage, SystemMessage
from pydantic import BaseModel, Field

from ..llm import chat_model
from ..profile import load_profile
from ..services import Services
from . import jev
from .common import COMPANY, SECURITY_RULES, tender_brief

log = logging.getLogger(__name__)


class Bewertung(BaseModel):
    score: int = Field(ge=0, le=100, description="Wie gut passt die Ausschreibung zu Decocity (0-100)?")
    passt: bool = Field(description="Lohnt sich ein genauerer Blick?")
    zusammenfassung: str = Field(description="1-2 Sätze: Was wird gesucht?")
    begruendung: str = Field(description="Warum passt es (nicht)?")
    relevante_leistungen: list[str] = Field(description="Welche Decocity-Leistungen sind gefragt?")
    risiken: list[str] = Field(description="Hürden, z.B. großes Bauvorhaben, nur ein Los passt, Frist knapp")


PROMPT = f"""Du bist der Analyst-Agent von {COMPANY}.
Bewerte öffentliche Ausschreibungen danach, ob Decocity sie mit seinen Leistungen
(innen- und außenliegender Sonnen-, Blend- und Sichtschutz, Vorhänge, Rollos, Plissees, Jalousien,
Lamellenvorhänge, Markisen, Rollläden, Raffstores, Insektenschutz inkl. Aufmaß und Montage)
ganz oder in einem Los erbringen kann.

Faustregeln:
- 80-100: Kernleistung von Decocity (z.B. "Lieferung und Montage Blendschutz").
- 50-79: Sonnenschutz ist ein Los oder ein relevanter Teil einer größeren Vergabe.
- 20-49: nur am Rande betroffen (z.B. Generalunternehmer-Paket mit Sonnenschutz als Kleinteil).
- 0-19: passt nicht.
- Weit außerhalb von Berlin/Brandenburg: Punkte abziehen.

{SECURITY_RULES}"""


class Vollbewertung(BaseModel):
    empfehlung: Literal["go", "no_go", "pruefen"] = Field(description="Teilnehmen, nicht teilnehmen oder offene Punkte klären")
    score: int = Field(ge=0, le=100, description="Gesamtpassung inkl. Unterlagen (0-100)")
    gewinnchance: Literal["hoch", "mittel", "niedrig"]
    aufwand: Literal["gering", "mittel", "hoch"] = Field(description="Aufwand für Angebot und Ausführung")
    passende_lose: list[str]
    leistungsumfang: str = Field(description="Was genau ist zu liefern/montieren, Mengen, Zeitraum")
    eignung_erfuellt: list[str] = Field(description="Eignungskriterien, die Decocity laut Profil erfüllt")
    fehlende_nachweise: list[str] = Field(description="Nachweise/Infos, die im Profil fehlen")
    risiken: list[str]
    wichtige_fristen: list[str]
    geschaetzter_auftragswert: str = Field(description="Nur wenn aus den Unterlagen ableitbar, sonst 'unbekannt'")
    begruendung: str

    def als_text(self) -> str:
        def liste(items: list[str]) -> str:
            return "\n".join(f"  • {i}" for i in items) or "  –"

        icon = {"go": "✅ GO", "no_go": "⛔ NO-GO", "pruefen": "🤔 PRÜFEN"}[self.empfehlung]
        return (
            f"{icon} – Passung {self.score}/100, Gewinnchance {self.gewinnchance}, Aufwand {self.aufwand}\n\n"
            f"{self.begruendung}\n\n"
            f"Leistung: {self.leistungsumfang}\n"
            f"Passende Lose:\n{liste(self.passende_lose)}\n"
            f"Auftragswert: {self.geschaetzter_auftragswert}\n"
            f"Fristen:\n{liste(self.wichtige_fristen)}\n"
            f"Eignung erfüllt:\n{liste(self.eignung_erfuellt)}\n"
            f"Fehlt noch:\n{liste(self.fehlende_nachweise)}\n"
            f"Risiken:\n{liste(self.risiken)}"
        )


VOLL_PROMPT = f"""Du bist der Analyst-Agent von {COMPANY}. Du machst die VOLLBEWERTUNG einer Ausschreibung
auf Basis des Rechercheberichts (Bekanntmachung + Vergabeunterlagen) und des Firmenprofils.
Entscheide nüchtern wie ein erfahrener Vertriebsleiter, ob sich eine Teilnahme lohnt:
Passt die Leistung? Erfüllt Decocity die Eignungskriterien? Ist die Frist schaffbar?
Ist der Aufwand im Verhältnis zum Auftragswert vertretbar? Wie stark ist der Wettbewerb vermutlich?
Fehlen Infos im Profil, ist das kein No-Go, sondern ein fehlender Nachweis.

{SECURITY_RULES}"""


async def vollbewertung(services: Services, tender: dict) -> Vollbewertung:
    """LLM liefert die Texte (Begründung, Nachweise, Risiken); Jev – falls aktiv – trifft parallel
    die Entscheidungen (Go/No-Go, Gewinnchance, Aufwand, Passung) mit Wahrscheinlichkeiten."""
    s = services.settings
    folder = s.bids_dir / str(tender["id"])
    recherche_path = folder / "recherche.md"
    if not recherche_path.exists():
        raise ValueError("Noch keine Recherche vorhanden – zuerst recherche_beauftragen.")
    recherche = recherche_path.read_text(encoding="utf-8")
    profil, brief = load_profile(s), tender_brief(tender)

    llm = chat_model(s, s.model_subagent, temperature=0).with_structured_output(Vollbewertung, method="function_calling")
    llm_call = llm.ainvoke(
        [
            SystemMessage(VOLL_PROMPT + "\n\nFirmenprofil:\n" + profil),
            HumanMessage(f"{brief}\n\n# Recherchebericht\n{recherche}"),
        ]
    )
    if jev.enabled(s):
        result, j = await asyncio.gather(llm_call, jev.entscheidung(s, profil, brief, recherche), return_exceptions=True)
        if isinstance(result, BaseException):
            raise result
        if isinstance(j, BaseException):
            log.warning("Jev-Vollbewertung fehlgeschlagen, nutze nur LLM: %s", j)
        else:
            result = result.model_copy(
                update={
                    "empfehlung": j.empfehlung,
                    "gewinnchance": j.gewinnchance,
                    "aufwand": j.aufwand,
                    "score": j.score,
                    "begruendung": (
                        f"[Jev] Empfehlung {j.empfehlung} ({j.empfehlung_conf:.0%} sicher), "
                        f"Eignung erfüllt {j.eignung_erfuellt:.0%}, Frist schaffbar {j.frist_schaffbar:.0%}. "
                        f"LLM-Einschätzung: {result.empfehlung}.\n\n{result.begruendung}"
                    ),
                }
            )
    else:
        result = await llm_call

    (folder / "bewertung.md").write_text(result.als_text(), encoding="utf-8")
    return result


async def bewerte(services: Services, tender: dict) -> Bewertung:
    """Vorbewertung eines Scout-Treffers – mit Jev, falls konfiguriert, sonst mit dem schnellen LLM."""
    s = services.settings
    profil, brief = load_profile(s), tender_brief(tender)
    if jev.enabled(s):
        try:
            j = await jev.vorbewertung(s, profil, brief)
            return Bewertung(
                score=j.score,
                passt=j.score >= 40 or j.kernleistung >= 0.5,
                zusammenfassung=j.als_text(),
                begruendung="",
                relevante_leistungen=[],
                risiken=[] if j.region_ok >= 0.5 else ["Erfüllungsort außerhalb des Einzugsgebiets"],
            )
        except Exception as e:
            log.warning("Jev-Vorbewertung fehlgeschlagen, nutze LLM: %s", e)

    llm = chat_model(s, s.model_fast, temperature=0).with_structured_output(Bewertung, method="function_calling")
    return await llm.ainvoke([SystemMessage(PROMPT + "\n\nFirmenprofil:\n" + profil), HumanMessage(brief)])
