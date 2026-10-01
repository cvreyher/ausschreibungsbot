"""Firmenprofil von Decocity: feste Stammdaten + Fakten, die der Bot im Gespräch gelernt hat."""

from datetime import date

from .config import Settings

LEARNED_FILE = "gelernt.md"


def load_profile(settings: Settings) -> str:
    parts = []
    for f in sorted(settings.profile_dir.glob("*.md")):
        parts.append(f.read_text(encoding="utf-8").strip())
    return "\n\n".join(parts) or "(Kein Firmenprofil hinterlegt.)"


def remember(settings: Settings, fact: str) -> None:
    path = settings.profile_dir / LEARNED_FILE
    if not path.exists():
        path.write_text("# Vom Bot gelernte Fakten über Decocity\n\n", encoding="utf-8")
    with path.open("a", encoding="utf-8") as f:
        f.write(f"- ({date.today().isoformat()}) {fact.strip()}\n")
