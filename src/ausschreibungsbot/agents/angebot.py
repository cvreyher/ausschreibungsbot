"""Sub-Agent "Angebot": schreibt aus Recherche, Firmenprofil und Nutzerangaben einen Angebotsentwurf."""

from langchain_core.messages import HumanMessage, SystemMessage

from ..llm import chat_model
from ..profile import load_profile
from ..services import Services
from .common import COMPANY, SECURITY_RULES, tender_brief

PROMPT = f"""Du bist der Angebots-Agent von {COMPANY}. Du erstellst Angebotsentwürfe für öffentliche
Ausschreibungen in sauberem Markdown.

Aufbau des Entwurfs:
1. Kurzüberblick (Ausschreibung, Los, Frist, Abgabeweg)
2. Anschreiben an die Vergabestelle (förmlich, Sie-Form, im Namen von Decocity)
3. Leistungsbeschreibung/Konzept: wie Decocity die Leistung erbringt (Aufmaß, Fertigung, Montage, Zeitplan, Gewährleistung)
4. Checkliste geforderter Nachweise und Formulare: jeweils [x] vorhanden / [ ] fehlt
5. Preisblatt: nur Positionen aus den Unterlagen; Preise NUR, wenn vom Nutzer genannt, sonst [PREIS FEHLT]
6. Offene Punkte, die vor der Abgabe geklärt werden müssen

{SECURITY_RULES}"""


async def schreibe_entwurf(services: Services, tender: dict, hinweise: str) -> str:
    s = services.settings
    folder = s.bids_dir / str(tender["id"])
    folder.mkdir(parents=True, exist_ok=True)
    recherche = (folder / "recherche.md").read_text(encoding="utf-8") if (folder / "recherche.md").exists() else "(noch keine Recherche)"
    entwurf_path = folder / "angebot_entwurf.md"
    vorheriger = entwurf_path.read_text(encoding="utf-8") if entwurf_path.exists() else ""

    content = (
        f"{tender_brief(tender)}\n\n# Firmenprofil\n{load_profile(s)}\n\n# Recherche\n{recherche}\n\n"
        f"# Hinweise und Antworten des Nutzers (vom Delegate-Agent)\n{hinweise}"
    )
    if vorheriger:
        content += f"\n\n# Bisheriger Entwurf (überarbeiten, nicht neu erfinden)\n{vorheriger}"

    llm = chat_model(s, s.model_subagent, temperature=0.3)
    msg = await llm.ainvoke([SystemMessage(PROMPT), HumanMessage(content)])
    entwurf_path.write_text(msg.text, encoding="utf-8")
    return msg.text
