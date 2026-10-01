"""Delegate-Agent: spricht über Telegram mit Decocity und verteilt Arbeit an die Sub-Agents.

Nur der Delegate darf den Nutzer etwas fragen (LangGraph-Interrupts). Die Sub-Agents laufen
zustandslos und geben offene Fragen in ihrem Ergebnis an den Delegate zurück.
"""

from datetime import datetime

from langchain.agents import create_agent
from langchain.agents.middleware import ModelRequest, SummarizationMiddleware, dynamic_prompt
from langchain_core.tools import tool
from langgraph.types import interrupt

from ..llm import chat_model
from ..pipeline import scout_und_bewerten
from ..profile import load_profile, remember
from ..services import Services
from . import analyst, angebot, kalkulation, recherche
from .common import COMPANY, SECURITY_RULES, tender_brief, tool_errors

PROMPT = f"""Du bist der Ausschreibungs-Bot von {COMPANY} (Berlin) und arbeitest als Delegate-Agent.
Du sprichst per Telegram mit dem Team von Decocity und hilfst ihnen, an öffentlichen Ausschreibungen
teilzunehmen. Du erledigst nichts selbst, was ein Sub-Agent besser kann, sondern delegierst:

Sub-Agents / Werkzeuge:
- ausschreibungen_suchen: Scout + Analyst – neue Ausschreibungen auf service.bund.de finden und bewerten
- analyst_beauftragen: schnelle Vorbewertung anhand der Kurzinfo
- recherche_beauftragen: Recherche-Agent mit Browser liest Bekanntmachung und Vergabeunterlagen
- vollbewertung_beauftragen: Analyst bewertet die gesamte Ausschreibung (Go/No-Go) nach der Recherche
- kalkulation_beauftragen: Kalkulations-Agent ermittelt Positionen, bepreist Produkte über den
  OrderCity-Großhandel und kalkuliert das Angebot mit Aufschlag. Auch für freie Preisanfragen ohne
  Ausschreibung (z.B. "Was kostet ein Plissee 80×120?") – dann ohne tender_id.
- angebot_beauftragen: Angebots-Agent schreibt/überarbeitet den Angebotsentwurf
- frage_an_nutzer: dem Team eine Frage stellen und auf die Antwort warten
- profil_merken: dauerhaft gültige Fakten über Decocity speichern (z.B. Referenzen, Stundensätze)
- freigabe_anfordern: fertigen Entwurf zur Freigabe vorlegen
- abgabe_durchfuehren: nur nach erteilter Freigabe

Ablauf "Bewerbung vorbereiten":
1. recherche_beauftragen
2. vollbewertung_beauftragen. Zeige dem Team das Ergebnis (Go/No-Go, Gewinnchance, Aufwand, Risiken)
   und frage in EINEM frage_an_nutzer-Aufruf, ob weitergemacht werden soll – zusammen mit den
   offenen Fragen aus Recherche und Bewertung, die das Profil nicht beantwortet (GEBÜNDELT, nummeriert).
   Will das Team nicht weitermachen: kurz bestätigen und aufhören.
   Allgemeingültige Antworten mit profil_merken speichern.
3. kalkulation_beauftragen mit den Antworten als Auftrag (Preise aus dem Großhandel + Aufschlag)
4. angebot_beauftragen mit allen Antworten als Hinweise – das Preisblatt kommt aus der Kalkulation
5. freigabe_anfordern mit kurzer Zusammenfassung inkl. Angebotssumme netto
6. Bei "aenderung": bei Preisänderungen erst kalkulation_beauftragen, dann angebot_beauftragen mit dem Feedback, dann erneut freigabe_anfordern.
   Bei "freigegeben": abgabe_durchfuehren. Bei "verworfen": kurz bestätigen und aufhören.

Interne Zahlen (Einkaufspreise, Aufschlag, Rohertrag) nur dem Decocity-Team zeigen – niemals in
Angebotsunterlagen für die Vergabestelle.

Stil: Deutsch, kurz und klar, Telegram-tauglich (keine Tabellen, wenig Formatierung).
Wenn du mit der Arbeit fertig bist, schreibe eine kurze Statusmeldung.

{SECURITY_RULES}"""


def build_delegate(services: Services, checkpointer):
    db, s = services.db, services.settings

    async def _tender(tender_id: int) -> dict:
        t = await db.get_tender(tender_id)
        if not t:
            raise ValueError(f"Ausschreibung #{tender_id} existiert nicht.")
        return t

    @tool
    async def ausschreibungen_suchen(suchbegriffe: list[str] | None = None) -> str:
        """Sucht neue Ausschreibungen auf service.bund.de (Standard-Suchbegriffe, wenn leer) und bewertet sie."""
        new = await scout_und_bewerten(services, suchbegriffe)
        if not new:
            return "Keine neuen Ausschreibungen gefunden."
        return "\n".join(f"#{t['id']} [{t['status']}, {t['score']}/100] {t['title']}" for t in new)

    @tool
    async def ausschreibungen_auflisten(status: str | None = None) -> str:
        """Listet bekannte Ausschreibungen, optional gefiltert nach Status
        (gemeldet, in_bearbeitung, freigegeben, bereit_zur_abgabe, abgegeben, ignoriert, verworfen)."""
        rows = await db.list_tenders(status)
        if not rows:
            return "Keine Einträge."
        return "\n".join(
            f"#{t['id']} [{t['status']}, {t['score']}/100] {t['title']} – Frist {t['deadline']}" for t in rows
        )

    @tool
    async def ausschreibung_details(tender_id: int) -> str:
        """Zeigt alle gespeicherten Infos zu einer Ausschreibung inkl. Recherche und Entwurf."""
        t = await _tender(tender_id)
        out = tender_brief(t)
        folder = s.bids_dir / str(tender_id)
        for name in ("bewertung.md", "kalkulation.md", "recherche.md", "angebot_entwurf.md"):
            if (folder / name).exists():
                out += f"\n\n=== {name} ===\n" + (folder / name).read_text(encoding="utf-8")[-8000:]
        return out

    @tool
    async def analyst_beauftragen(tender_id: int) -> str:
        """Lässt den Analyst-Agent eine Ausschreibung (neu) bewerten."""
        b = await analyst.bewerte(services, await _tender(tender_id))
        await db.update_tender(tender_id, score=b.score, summary=f"{b.zusammenfassung} {b.begruendung}")
        return b.model_dump_json(indent=1)

    @tool
    async def vollbewertung_beauftragen(tender_id: int) -> str:
        """Lässt den Analyst-Agent die GESAMTE Ausschreibung (inkl. Recherche/Vergabeunterlagen)
        bewerten: Go/No-Go, Gewinnchance, Aufwand, fehlende Nachweise, Risiken."""
        b = await analyst.vollbewertung(services, await _tender(tender_id))
        await db.update_tender(tender_id, score=b.score, summary=f"[{b.empfehlung}] {b.begruendung}")
        return b.als_text()

    @tool
    async def recherche_beauftragen(tender_id: int, auftrag: str) -> str:
        """Beauftragt den Recherche-Agent (Browser), Bekanntmachung und Vergabeunterlagen auszuwerten.
        'auftrag' beschreibt, worauf er achten soll. Dauert einige Minuten."""
        t = await _tender(tender_id)
        if t["status"] in ("gemeldet", "neu"):
            await db.update_tender(tender_id, status="in_bearbeitung")
        return await recherche.recherchiere(services, t, auftrag)

    @tool
    async def kalkulation_beauftragen(auftrag: str, tender_id: int | None = None) -> str:
        """Beauftragt den Kalkulations-Agent: Positionen ermitteln, Produkte im OrderCity-Großhandel
        konfigurieren und bepreisen, Angebot mit Aufschlag kalkulieren. Ohne tender_id für freie
        Preisanfragen – dann im Auftrag Produktart, Maße und Menge nennen. Dauert einige Minuten."""
        if not services.ordercity:
            return "OrderCity ist nicht konfiguriert (ORDERCITY_API_KEY fehlt in der .env)."
        t = await _tender(tender_id) if tender_id else None
        return await kalkulation.kalkuliere_angebot(services, t, auftrag)

    @tool
    async def angebot_beauftragen(tender_id: int, hinweise: str) -> str:
        """Beauftragt den Angebots-Agent, den Angebotsentwurf zu schreiben oder zu überarbeiten.
        'hinweise' enthält alle Antworten und Wünsche des Nutzers."""
        text = await angebot.schreibe_entwurf(services, await _tender(tender_id), hinweise)
        return f"Entwurf gespeichert ({len(text)} Zeichen). Anfang:\n{text[:3000]}"

    @tool
    def frage_an_nutzer(frage: str, tender_id: int | None = None) -> str:
        """Stellt dem Decocity-Team eine Frage per Telegram und wartet auf die Antwort."""
        return interrupt({"art": "frage", "frage": frage, "tender_id": tender_id})

    @tool
    def profil_merken(fakt: str) -> str:
        """Speichert einen dauerhaft gültigen Fakt über Decocity im Firmenprofil."""
        remember(s, fakt)
        return "Gespeichert."

    @tool
    async def freigabe_anfordern(tender_id: int, zusammenfassung: str) -> str:
        """Legt dem Nutzer den Angebotsentwurf zur Freigabe vor.
        Antwort: 'freigegeben', 'verworfen' oder 'aenderung: <Feedback>'."""
        await _tender(tender_id)
        return interrupt({"art": "freigabe", "tender_id": tender_id, "zusammenfassung": zusammenfassung})

    @tool
    async def abgabe_durchfuehren(tender_id: int) -> str:
        """Gibt ein FREIGEGEBENES Angebot ab bzw. bereitet die manuelle Abgabe vor."""
        t = await _tender(tender_id)
        # Die Freigabe kann nur der Nutzer per Telegram-Button setzen, nie das LLM.
        if t["status"] != "freigegeben":
            return "Abgelehnt: Das Angebot wurde nicht per Button freigegeben."
        entwurf_path = s.bids_dir / str(tender_id) / "angebot_entwurf.md"
        if not s.allow_auto_submit:
            await db.update_tender(tender_id, status="bereit_zur_abgabe")
            return (
                "Automatische Abgabe ist deaktiviert (ALLOW_AUTO_SUBMIT=false). Status: bereit_zur_abgabe. "
                f"Sag dem Nutzer, dass er das Angebot selbst über {t.get('notice_url') or t['url']} abgeben muss "
                f"und der Entwurf unter {entwurf_path} liegt."
            )
        report = await recherche.gib_ab(services, t, entwurf_path.read_text(encoding="utf-8"))
        ok = report.strip().endswith("ABGABE_ERFOLGREICH")
        await db.update_tender(tender_id, status="abgegeben" if ok else "bereit_zur_abgabe")
        return report

    @dynamic_prompt
    def prompt(request: ModelRequest) -> str:
        return (
            f"{PROMPT}\n\nHeute ist {datetime.now():%A, %d.%m.%Y %H:%M}.\n\n"
            f"# Firmenprofil Decocity\n{load_profile(s)}"
        )

    tools = [
        ausschreibungen_suchen,
        ausschreibungen_auflisten,
        ausschreibung_details,
        analyst_beauftragen,
        recherche_beauftragen,
        vollbewertung_beauftragen,
        kalkulation_beauftragen,
        angebot_beauftragen,
        frage_an_nutzer,
        profil_merken,
        freigabe_anfordern,
        abgabe_durchfuehren,
    ]
    return create_agent(
        chat_model(s, s.model_delegate, reasoning_effort=s.delegate_reasoning_effort or None),
        tools=tools,
        middleware=[
            tool_errors(),
            prompt,
            SummarizationMiddleware(chat_model(s, s.model_fast), trigger=("tokens", 80_000), keep=("messages", 20)),
        ],
        checkpointer=checkpointer,
        name="decocity_delegate",
    )
