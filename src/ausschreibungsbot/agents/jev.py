"""Jev (TypeSafe AI) als Entscheidungsmodell für den Analyst.

Jev erzeugt keinen Text, sondern beantwortet typisierte Fragen (Noul = ja/nein, Choice, Score)
mit Wahrscheinlichkeiten. Wir nutzen es für die Entscheidungen; Begründungstexte liefert das LLM.
"""

import warnings
from dataclasses import dataclass

from langchain_core._api import LangChainBetaWarning
from langchain_typesafe import Choice, Noul, Score, TypeSafeClassifier

from ..config import Settings

warnings.filterwarnings("ignore", category=LangChainBetaWarning, module=__name__)

# Jev-Eingaben begrenzen (lange Rechercheberichte)
MAX_STATE_CHARS = 40_000

PASSUNG_STUFEN = [
    "Passt nicht: keine Leistung von Decocity gefragt.",
    "Nur am Rande: Sonnen-/Sichtschutz ist ein Kleinteil eines großen Bau- oder GU-Pakets.",
    "Teilweise: Sonnen-, Blend- oder Sichtschutz ist ein eigenes Los oder ein relevanter Teil.",
    "Gut: Sonnen-, Blend- oder Sichtschutz ist Hauptgegenstand, aber mit Zusatzleistungen außerhalb des Portfolios.",
    "Kernleistung: genau Decocitys Portfolio (Lieferung/Montage von Rollos, Jalousien, Plissees, Vorhängen, Markisen, Raffstores, Blendschutz).",
]

GEWINNCHANCE_STUFEN = ["Niedrig", "Mittel", "Hoch"]
AUFWAND_STUFEN = ["Gering", "Mittel", "Hoch"]


@dataclass
class JevVorbewertung:
    score: int
    kernleistung: float
    region_ok: float
    leistungsart: str
    leistungsart_conf: float

    def als_text(self) -> str:
        return (
            f"[Jev] Kernleistung {self.kernleistung:.0%}, Region passt {self.region_ok:.0%}, "
            f"Art: {self.leistungsart} ({self.leistungsart_conf:.0%})."
        )


@dataclass
class JevEntscheidung:
    empfehlung: str
    empfehlung_conf: float
    gewinnchance: str
    aufwand: str
    eignung_erfuellt: float
    frist_schaffbar: float
    score: int


def enabled(settings: Settings) -> bool:
    return bool(settings.typesafe_api_key)


def _classifier(settings: Settings) -> TypeSafeClassifier:
    return TypeSafeClassifier(model=settings.model_jev, api_key=settings.typesafe_api_key)


def _level(value: float, labels: list[str]) -> str:
    return labels[min(round(value), len(labels) - 1)].lower()


async def vorbewertung(settings: Settings, profil: str, tender_text: str) -> JevVorbewertung:
    r = await _classifier(settings).ainvoke(
        {
            "state": {"firmenprofil": profil, "ausschreibung": tender_text[:MAX_STATE_CHARS]},
            "questions": {
                "passung": Score(
                    instructions="Wie gut passt die Ausschreibung zum Leistungsportfolio der Firma?",
                    criteria=PASSUNG_STUFEN,
                ),
                "kernleistung": Noul(
                    instructions="Ist Sonnen-, Blend- oder Sichtschutz Hauptgegenstand oder ein eigenes Los?"
                ),
                "region_ok": Noul(instructions="Liegt der Erfüllungsort in Berlin, Potsdam oder im Berliner Umland?"),
                "leistungsart": Choice(
                    instructions="Welche Art von Leistung wird ausgeschrieben?",
                    criteria={
                        "lieferung_montage": "Lieferung und Montage von Produkten",
                        "nur_lieferung": "Reine Lieferung ohne Montage",
                        "bauleistung": "Bauleistung/Gewerk in einem Bauvorhaben (VOB)",
                        "wartung": "Wartung, Reparatur, Instandhaltung",
                        "sonstiges": "Planung, Beratung oder etwas anderes",
                    },
                ),
            },
        }
    )
    passung = r.scores["passung"]
    region = r.nouls["region_ok"].noul
    score = passung.score / (len(PASSUNG_STUFEN) - 1) * 100
    if region < 0.5:
        score *= 0.6  # weit weg: deutlich abwerten
    art = r.choices["leistungsart"]
    return JevVorbewertung(
        score=round(score),
        kernleistung=r.nouls["kernleistung"].noul,
        region_ok=region,
        leistungsart=art.choice,
        leistungsart_conf=art.confidence,
    )


async def entscheidung(settings: Settings, profil: str, tender_text: str, recherche: str) -> JevEntscheidung:
    r = await _classifier(settings).ainvoke(
        {
            "state": {
                "firmenprofil": profil,
                "ausschreibung": tender_text,
                "recherchebericht": recherche[:MAX_STATE_CHARS],
            },
            "questions": {
                "empfehlung": Choice(
                    instructions="Soll die Firma an dieser Ausschreibung teilnehmen?",
                    criteria={
                        "go": "Ja: Leistung passt, Eignung erfüllbar, Aufwand lohnt sich.",
                        "no_go": "Nein: Leistung passt nicht, Eignung nicht erfüllbar oder Aufwand zu hoch.",
                        "pruefen": "Unklar: Es müssen erst offene Punkte geklärt werden.",
                    },
                ),
                "gewinnchance": Score(
                    instructions="Wie hoch ist die Chance, den Zuschlag zu bekommen?", criteria=GEWINNCHANCE_STUFEN
                ),
                "aufwand": Score(
                    instructions="Wie hoch ist der Aufwand für Angebot und Ausführung?", criteria=AUFWAND_STUFEN
                ),
                "passung": Score(
                    instructions="Wie gut passt die Ausschreibung zum Leistungsportfolio der Firma?",
                    criteria=PASSUNG_STUFEN,
                ),
                "eignung": Noul(instructions="Erfüllt die Firma laut Profil die geforderten Eignungskriterien?"),
                "frist": Noul(instructions="Ist die Angebotsfrist realistisch schaffbar?"),
            },
        }
    )
    emp = r.choices["empfehlung"]
    return JevEntscheidung(
        # Bei unsicherer Entscheidung lieber "prüfen" statt blind go/no_go
        empfehlung=emp.choice if emp.confidence >= 0.6 else "pruefen",
        empfehlung_conf=emp.confidence,
        gewinnchance=_level(r.scores["gewinnchance"].score, GEWINNCHANCE_STUFEN),
        aufwand=_level(r.scores["aufwand"].score, AUFWAND_STUFEN),
        eignung_erfuellt=r.nouls["eignung"].noul,
        frist_schaffbar=r.nouls["frist"].noul,
        score=round(r.scores["passung"].score / (len(PASSUNG_STUFEN) - 1) * 100),
    )
