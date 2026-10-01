"""Sub-Agent "Analyst": bewertet schnell und günstig, ob eine Ausschreibung zu Decocity passt."""

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


async def bewerte(services: Services, tender: dict) -> Bewertung:
    s = services.settings
    llm = chat_model(s, s.model_fast, temperature=0).with_structured_output(Bewertung, method="function_calling")
    return await llm.ainvoke(
        [
            SystemMessage(PROMPT + "\n\nFirmenprofil:\n" + load_profile(s)),
            HumanMessage(tender_brief(tender)),
        ]
    )
