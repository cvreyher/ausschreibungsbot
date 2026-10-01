"""Deterministische Angebotskalkulation – Rechnen macht der Code, nie das LLM."""

from dataclasses import asdict, dataclass, field


@dataclass
class KalkPosition:
    nr: str
    bezeichnung: str
    menge: int
    einkauf_netto_cents: int  # OrderCity-VK netto je Stück = unser Einkauf
    montage_netto_cents: int = 0  # je Stück
    angebot_netto_cents: int = 0  # Einheitspreis im Angebot
    gesamt_netto_cents: int = 0


@dataclass
class Kalkulation:
    positionen: list[KalkPosition]
    aufschlag_prozent: float
    montage_pro_stueck_cents: int
    ust_prozent: float
    einkauf_netto_cents: int = 0
    angebot_netto_cents: int = 0
    ust_cents: int = 0
    angebot_brutto_cents: int = 0
    rohertrag_cents: int = 0
    hinweise: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return asdict(self)


def eur(cents: int) -> str:
    return f"{cents / 100:,.2f} €".replace(",", "X").replace(".", ",").replace("X", ".")


def kalkuliere(
    positionen: list[dict], aufschlag_prozent: float, montage_pro_stueck_eur: float, ust_prozent: float
) -> Kalkulation:
    """positionen: [{nr, bezeichnung, menge, einkauf_netto_cents}] – Einkauf je Stück."""
    montage_cents = round(montage_pro_stueck_eur * 100)
    k = Kalkulation(
        positionen=[], aufschlag_prozent=aufschlag_prozent, montage_pro_stueck_cents=montage_cents, ust_prozent=ust_prozent
    )
    for p in positionen:
        menge = int(p["menge"])
        ek = int(p["einkauf_netto_cents"])
        # Aufschlag auf Ware + Montage, Einheitspreis auf volle Euro aufrunden (sieht im Angebot sauberer aus)
        einheit = (ek + montage_cents) * (1 + aufschlag_prozent / 100)
        einheit_cents = int(-(-einheit // 100) * 100)
        pos = KalkPosition(
            nr=str(p.get("nr", "")),
            bezeichnung=p["bezeichnung"],
            menge=menge,
            einkauf_netto_cents=ek,
            montage_netto_cents=montage_cents,
            angebot_netto_cents=einheit_cents,
            gesamt_netto_cents=einheit_cents * menge,
        )
        k.positionen.append(pos)
        k.einkauf_netto_cents += (ek + montage_cents) * menge
        k.angebot_netto_cents += pos.gesamt_netto_cents
    k.ust_cents = round(k.angebot_netto_cents * ust_prozent / 100)
    k.angebot_brutto_cents = k.angebot_netto_cents + k.ust_cents
    k.rohertrag_cents = k.angebot_netto_cents - k.einkauf_netto_cents
    if not montage_cents:
        k.hinweise.append("Montage ist NICHT einkalkuliert (ANGEBOT_MONTAGE_PRO_STUECK_EUR=0).")
    return k


def als_markdown(k: Kalkulation, intern: bool = True) -> str:
    """intern=True: mit Einkauf und Rohertrag (nur für Decocity). intern=False: nur Angebotspreise."""
    lines = []
    if intern:
        lines.append("| Pos. | Bezeichnung | Menge | EK/Stk | Angebot/Stk | Gesamt netto |")
        lines.append("|---|---|---:|---:|---:|---:|")
        for p in k.positionen:
            lines.append(
                f"| {p.nr} | {p.bezeichnung} | {p.menge} | {eur(p.einkauf_netto_cents + p.montage_netto_cents)} "
                f"| {eur(p.angebot_netto_cents)} | {eur(p.gesamt_netto_cents)} |"
            )
    else:
        lines.append("| Pos. | Bezeichnung | Menge | Einheitspreis netto | Gesamt netto |")
        lines.append("|---|---|---:|---:|---:|")
        for p in k.positionen:
            lines.append(f"| {p.nr} | {p.bezeichnung} | {p.menge} | {eur(p.angebot_netto_cents)} | {eur(p.gesamt_netto_cents)} |")
    lines.append("")
    lines.append(f"Summe netto: **{eur(k.angebot_netto_cents)}**  ")
    lines.append(f"USt {k.ust_prozent:g} %: {eur(k.ust_cents)}  ")
    lines.append(f"Summe brutto: **{eur(k.angebot_brutto_cents)}**")
    if intern:
        lines.append("")
        lines.append(
            f"Intern: Einkauf {eur(k.einkauf_netto_cents)} · Aufschlag {k.aufschlag_prozent:g} % · "
            f"Rohertrag {eur(k.rohertrag_cents)}"
        )
    for h in k.hinweise:
        lines.append(f"\n⚠️ {h}")
    return "\n".join(lines)
