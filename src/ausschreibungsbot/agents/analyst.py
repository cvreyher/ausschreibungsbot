"""Sub-Agent "Analyst".

- bewerte():        schnelle, günstige Vorbewertung anhand der Kurzinfo (Scout-Treffer)
- vollbewertung():  Go/No-Go-Bewertung der gesamten Ausschreibung inkl. Vergabeunterlagen
"""

from typing import Literal

from langchain_core.messages import HumanMessage, SystemMessage
from pydantic import BaseModel, Field

from ..llm import chat_model
from ..profile import load_profile
from ..services import Services
from .common import COMPANY, SECURITY_RULES, tender_brief


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
    s = services.settings
    folder = s.bids_dir / str(tender["id"])
    recherche = folder / "recherche.md"
    if not recherche.exists():
        raise ValueError("Noch keine Recherche vorhanden – zuerst recherche_beauftragen.")
    llm = chat_model(s, s.model_smart, temperature=0).with_structured_output(Vollbewertung, method="function_calling")
    result: Vollbewertung = await llm.ainvoke(
        [
            SystemMessage(VOLL_PROMPT + "\n\nFirmenprofil:\n" + load_profile(s)),
            HumanMessage(f"{tender_brief(tender)}\n\n# Recherchebericht\n{recherche.read_text(encoding='utf-8')}"),
        ]
    )
    (folder / "bewertung.md").write_text(result.als_text(), encoding="utf-8")
    return result


async def bewerte(services: Services, tender: dict) -> Bewertung:
    s = services.settings
    llm = chat_model(s, s.model_fast, temperature=0).with_structured_output(Bewertung, method="function_calling")
    return await llm.ainvoke(
        [
            SystemMessage(PROMPT + "\n\nFirmenprofil:\n" + load_profile(s)),
            HumanMessage(tender_brief(tender)),
        ]
    )
