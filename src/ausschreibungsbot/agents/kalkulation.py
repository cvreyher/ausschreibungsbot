"""Kalkulations-Agent (Reasoning) mit zwei Sub-Agents.

  Kalkulations-Agent  plant, delegiert, prüft Plausibilität, fasst zusammen
   ├─ Mengen-Agent        liest Recherche + Leistungsverzeichnis → Positionen (Art, Maße, Menge)
   └─ Konfigurator-Agent  findet je Position das passende OrderCity-Produkt (MCP) und konfiguriert es

Regeln: Rechnen macht ausschließlich der Code. Jeder Preis wird nach dem Konfigurator-Agent noch
einmal direkt gegen die OrderCity-API geprüft; das LLM gibt nie selbst Beträge weiter.
"""

import asyncio
import json
from datetime import datetime
from pathlib import Path

from langchain.agents import create_agent
from langchain.agents.middleware import ModelCallLimitMiddleware
from langchain.agents.structured_output import ToolStrategy
from langchain_core.messages import HumanMessage
from langchain_core.tools import tool
from pydantic import BaseModel, Field

from ..llm import chat_model
from ..ordercity.client import OrderCityClient, OrderCitySettings
from ..ordercity.kalkulation import als_markdown, eur, kalkuliere
from .. import unterlagen
from ..profile import load_profile
from ..services import Services
from .common import COMPANY, SECURITY_RULES, tender_brief, tool_errors
from ..trace import melde
from .recherche import doc_tools

MAX_PARALLEL = 3

# ----------------------------------------------------------------------------- Schemata


class Position(BaseModel):
    nr: str = Field(description="Positionsnummer aus dem LV, sonst fortlaufend")
    bezeichnung: str = Field(description="Kurzbezeichnung, z.B. 'Innenrollo Büro 2.OG'")
    produktart: str = Field(description="z.B. Innenrollo, Plissee, Jalousie, Lamellenvorhang, Markise, Raffstore, Rollladen, Insektenschutz, Vorhang")
    menge: int = Field(ge=1)
    breite_mm: int | None = Field(default=None, description="Breite in mm, falls angegeben")
    hoehe_mm: int | None = Field(default=None, description="Höhe in mm, falls angegeben")
    anforderungen: list[str] = Field(default_factory=list, description="Stoff/Verdunkelung, Bedienung, Farbe, Brandschutz, Montageart …")
    quelle: str = Field(default="", description="Dokument und Seite")
    unsicher: bool = Field(default=False, description="True, wenn Maße/Mengen geschätzt oder unklar sind")


class Positionsliste(BaseModel):
    positionen: list[Position]
    offene_fragen: list[str] = Field(default_factory=list)


class Konfigwert(BaseModel):
    schluessel: str
    wert: str = Field(description="Wert als Text, z.B. '1200', 'A50Ku', 'true'")


class Konfiguration(BaseModel):
    gefunden: bool = Field(description="Gibt es ein passendes, gültig berechnetes Produkt?")
    hersteller: str | None = None
    produkt_id: str | None = None
    produkt_name: str | None = None
    werte: list[Konfigwert] = Field(default_factory=list, description="Exakt die Werte, mit denen konfiguration_berechnen gueltig=true lieferte")
    annahmen: list[str] = Field(default_factory=list, description="Getroffene Annahmen, z.B. Standardstoff gewählt")
    problem: str | None = Field(default=None, description="Warum nichts gefunden wurde")


# ----------------------------------------------------------------------------- Prompts

MENGEN_PROMPT = f"""Du bist der Mengen-Agent von {COMPANY}. Ermittle aus Recherchebericht und Leistungsverzeichnis (LV)
ALLE Positionen, die Decocity liefern kann (Sonnen-, Blend-, Sichtschutz, Rollos, Plissees, Jalousien,
Lamellenvorhänge, Markisen, Raffstores, Rollläden, Insektenschutz, Vorhänge).
Lies dazu die LV-Dateien mit den Datei-Tools (downloads_auflisten, dokument_lesen). Gescannte PDFs sind
bereits per OCR in Text umgewandelt – dokument_lesen liefert diesen Text automatisch. Übernimm Maße und Mengen
exakt aus dem LV. Fehlen Maße, lass sie leer und setze unsicher=true. Positionen anderer Gewerke ignorieren.
{SECURITY_RULES}"""

KONFIG_PROMPT = f"""Du bist der Konfigurator-Agent von {COMPANY}. Finde für EINE Position das passende Produkt im
OrderCity-Katalog und konfiguriere es gültig.

Vorgehen:
1. hersteller_auflisten, dann produkte_suchen mit passendem Suchbegriff zur Produktart.
2. produkt_konfigurator laden und alle Pflichtfelder sinnvoll belegen (Maße in mm aus der Position;
   bei Auswahlfeldern die Option, die die Anforderungen am besten erfüllt, sonst den Standard).
3. konfiguration_berechnen aufrufen. Bei gueltig=false den Fehler lesen und die Werte anpassen
   (höchstens 4 Versuche). Fehlen Maße, nimm plausible Standardmaße und notiere das als Annahme.
4. Antworte mit den exakten Werten der letzten GÜLTIGEN Berechnung.
Preise gibst du NICHT an – die rechnet das System selbst nach.
{SECURITY_RULES}"""

KALK_PROMPT = f"""Du bist der Kalkulations-Agent von {COMPANY}. Du erstellst eine ungefähre Angebotskalkulation
mit Preisen aus dem OrderCity-Großhandel plus Aufschlag. Denke gründlich nach und plane.

Werkzeuge:
- positionen_ermitteln: Mengen-Agent liest Recherche und LV und liefert die Positionen (nur bei Ausschreibungen)
- position_bepreisen: Konfigurator-Agent bepreist EINE Position (kann parallel für mehrere laufen)
- kalkulation_abschliessen: rechnet Aufschlag, Summen und speichert die Kalkulation

Ablauf:
1. Bei einer Ausschreibung: positionen_ermitteln. Bei einer freien Anfrage: Positionen selbst aus dem Auftrag ableiten.
2. Jede Position einzeln mit position_bepreisen bepreisen (mehrere Aufrufe gleichzeitig sind erlaubt).
   Schlägt eine Position fehl, versuche sie höchstens einmal mit anderer Produktart/Beschreibung erneut.
3. kalkulation_abschliessen aufrufen.
4. Antworte mit: Summe netto/brutto, nicht bepreisbaren Positionen, getroffenen Annahmen und offenen Fragen.
   Nenne nie Beträge, die nicht aus kalkulation_abschliessen stammen.
{SECURITY_RULES}"""


# ----------------------------------------------------------------------------- Agent


def _wert(text: str):
    t = text.strip()
    if t.lower() in ("true", "false"):
        return t.lower() == "true"
    try:
        return int(t)
    except ValueError:
        try:
            return float(t.replace(",", "."))
        except ValueError:
            return t


async def kalkuliere_angebot(services: Services, tender: dict | None, auftrag: str) -> str:
    s = services.settings
    if not s.ordercity_api_key:
        return "OrderCity ist nicht konfiguriert (ORDERCITY_API_KEY fehlt in der .env)."
    oc_settings = OrderCitySettings()
    api = OrderCityClient(oc_settings)
    folder = s.bids_dir / (str(tender["id"]) if tender else f"schnell-{datetime.now():%Y%m%d-%H%M%S}")
    folder.mkdir(parents=True, exist_ok=True)

    oc_tools = [t for t in await services.ordercity.tools() if t.name != "angebot_kalkulieren"]
    sem = asyncio.Semaphore(MAX_PARALLEL)
    bepreist: dict[str, dict] = {}  # nr -> geprüftes Ergebnis
    probleme: dict[str, str] = {}
    annahmen: list[str] = []

    @tool
    async def positionen_ermitteln(hinweise: str = "") -> str:
        """Mengen-Agent: ermittelt alle relevanten Positionen (Art, Maße, Menge) aus Recherche und LV."""
        if not tender:
            return "Keine Ausschreibung – Positionen bitte selbst aus dem Auftrag ableiten."
        recherche = folder / "recherche.md"
        udir = unterlagen.tender_dir(services, tender["id"])
        if not await services.db.list_documents(tender["id"]):
            await unterlagen.sichern(services, tender)  # Unterlagen fehlen noch → jetzt sichern (inkl. OCR)
        docs = await services.db.list_documents(tender["id"])
        agent = create_agent(
            chat_model(s, s.model_subagent),
            tools=doc_tools(udir),
            system_prompt=MENGEN_PROMPT,
            name="mengen",
            response_format=ToolStrategy(Positionsliste),
            middleware=[tool_errors(), ModelCallLimitMiddleware(run_limit=25, exit_behavior="end")],
        )
        task = (
            f"{tender_brief(tender)}\n\n# Recherchebericht\n"
            f"{recherche.read_text(encoding='utf-8') if recherche.exists() else '(keine Recherche vorhanden)'}"
            f"\n\n# Unterlagen (LV zuerst lesen)\n{unterlagen.uebersicht(docs, udir)}"
            f"\n\n# Hinweise\n{hinweise}"
        )
        result = await agent.ainvoke({"messages": [HumanMessage(task)]}, {"recursion_limit": 150})
        liste: Positionsliste | None = result.get("structured_response")
        if not liste:
            return "Der Mengen-Agent hat keine Positionsliste geliefert. Letzte Nachricht: " + result["messages"][-1].text[:1500]
        (folder / "positionen.json").write_text(liste.model_dump_json(indent=1), encoding="utf-8")
        return liste.model_dump_json(indent=1)

    @tool
    async def position_bepreisen(
        nr: str,
        bezeichnung: str,
        produktart: str,
        menge: int,
        breite_mm: int | None = None,
        hoehe_mm: int | None = None,
        anforderungen: list[str] | None = None,
    ) -> str:
        """Konfigurator-Agent: sucht das passende OrderCity-Produkt für EINE Position, konfiguriert es und
        prüft den Preis. Ergebnis: Produkt und geprüfter Einkaufspreis je Stück – oder das Problem."""
        pos = {
            "nr": nr, "bezeichnung": bezeichnung, "produktart": produktart, "menge": menge,
            "breite_mm": breite_mm, "hoehe_mm": hoehe_mm, "anforderungen": anforderungen or [],
        }
        async with sem:
            agent = create_agent(
                chat_model(s, s.model_subagent),
                tools=oc_tools,
                system_prompt=KONFIG_PROMPT,
                name="konfigurator",
                response_format=ToolStrategy(Konfiguration),
                middleware=[tool_errors(), ModelCallLimitMiddleware(run_limit=20, exit_behavior="end")],
            )
            result = await agent.ainvoke(
                {"messages": [HumanMessage("Position:\n" + json.dumps(pos, ensure_ascii=False))]}, {"recursion_limit": 120}
            )
        k: Konfiguration | None = result.get("structured_response")
        if not k or not k.gefunden or not k.produkt_id or not k.hersteller:
            probleme[nr] = (k.problem if k else None) or "kein passendes Produkt gefunden"
            return f"Position {nr}: nicht bepreisbar – {probleme[nr]}"

        # Preis unabhängig vom LLM direkt gegen die API prüfen
        werte = {w.schluessel: _wert(w.wert) for w in k.werte}
        check = await api.berechne(k.hersteller, k.produkt_id, werte, 1)
        if not check["gueltig"]:
            probleme[nr] = f"Konfiguration ungültig: {check.get('fehler')}"
            await melde(f"❌ Pos. {nr}: Preisprüfung abgelehnt – {check.get('fehler')}")
            return f"Position {nr}: {probleme[nr]}"
        ek = check["preis"]["vkNettoCents"]
        await melde(f"✅ Pos. {nr}: {k.produkt_name or k.produkt_id} – Preis bei OrderCity geprüft ({eur(ek)}/Stk)")
        bepreist[nr] = {
            "nr": nr, "bezeichnung": f"{bezeichnung} ({k.produkt_name or k.produkt_id})", "menge": menge,
            "einkauf_netto_cents": ek, "hersteller": k.hersteller, "produkt_id": k.produkt_id, "werte": werte,
        }
        probleme.pop(nr, None)
        annahmen.extend(f"Pos. {nr}: {a}" for a in k.annahmen)
        return (
            f"Position {nr}: {k.produkt_name or k.produkt_id} ({k.hersteller}) – geprüft, "
            f"Einkauf {eur(ek)} je Stück × {menge}. Annahmen: {'; '.join(k.annahmen) or 'keine'}"
        )

    @tool
    def kalkulation_abschliessen(aufschlag_prozent: float | None = None) -> str:
        """Rechnet aus allen geprüften Positionen das Angebot (Aufschlag, Montage, USt, Summen) und speichert es.
        aufschlag_prozent leer = Standard aus der Konfiguration."""
        if not bepreist:
            return "Keine bepreisten Positionen – nichts zu kalkulieren."
        aufschlag = oc_settings.angebot_aufschlag_prozent if aufschlag_prozent is None else aufschlag_prozent
        k = kalkuliere(list(bepreist.values()), aufschlag, oc_settings.angebot_montage_pro_stueck_eur, oc_settings.ust_prozent)
        if probleme:
            k.hinweise.append("Nicht bepreist: " + "; ".join(f"Pos. {n}: {p}" for n, p in probleme.items()))
        if annahmen:
            k.hinweise.append("Annahmen: " + "; ".join(annahmen))
        intern = als_markdown(k, intern=True)
        (folder / "kalkulation.md").write_text(f"# Kalkulation (INTERN)\n\n{intern}\n", encoding="utf-8")
        (folder / "kalkulation_angebot.md").write_text(
            f"# Preisblatt\n\n{als_markdown(k, intern=False)}\n", encoding="utf-8"
        )
        (folder / "kalkulation.json").write_text(
            json.dumps({**k.as_dict(), "positionen_details": list(bepreist.values())}, ensure_ascii=False, indent=1),
            encoding="utf-8",
        )
        return intern

    agent = create_agent(
        chat_model(s, s.model_kalkulation, reasoning_effort=s.kalkulation_reasoning_effort or None),
        tools=[positionen_ermitteln, position_bepreisen, kalkulation_abschliessen],
        system_prompt=KALK_PROMPT + "\n\nFirmenprofil:\n" + load_profile(s),
        name="kalkulation",
        middleware=[tool_errors(), ModelCallLimitMiddleware(run_limit=40, exit_behavior="end")],
    )
    task = (f"{tender_brief(tender)}\n\n" if tender else "Freie Anfrage (keine Ausschreibung).\n\n") + f"Auftrag:\n{auftrag}"
    try:
        result = await agent.ainvoke({"messages": [HumanMessage(task)]}, {"recursion_limit": 300})
    finally:
        await api.close()
    antwort = result["messages"][-1].text
    if not (folder / "kalkulation.md").exists() and bepreist:
        antwort += "\n\n" + kalkulation_abschliessen.invoke({})  # Sicherheitsnetz, falls der Agent abbricht
    return f"{antwort}\n\n(Gespeichert unter {folder})"


def kalkulation_fuer_angebot(folder: Path) -> str:
    """Preisblatt OHNE interne Daten (Einkauf, Aufschlag, Rohertrag) für den Angebots-Agent."""
    p = folder / "kalkulation_angebot.md"
    return p.read_text(encoding="utf-8") if p.exists() else ""
