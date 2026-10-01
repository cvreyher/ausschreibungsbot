"""OrderCity-MCP-Server: Produkte konfigurieren, bepreisen und Angebote mit Aufschlag kalkulieren.

Start (stdio):  uv run ordercity-mcp
Konfiguration:  ORDERCITY_API_KEY, ORDERCITY_BASE_URL, ANGEBOT_AUFSCHLAG_PROZENT,
                ANGEBOT_MONTAGE_PRO_STUECK_EUR (aus .env oder Umgebung)

Bewusst ohne Warenkorb/Bestellung – über diesen Server kann nichts gekauft werden.
"""

import logging
from typing import Any

from mcp.server.fastmcp import FastMCP

from .client import OrderCityClient, OrderCitySettings
from .kalkulation import als_markdown, kalkuliere

mcp = FastMCP(
    "ordercity",
    log_level="WARNING",
    instructions=(
        "Großhandelskatalog für Sonnenschutz (OrderCity). Ablauf: hersteller_auflisten → produkte_suchen → "
        "produkt_konfigurator (Felder & Grenzwerte) → konfiguration_berechnen (Preis prüfen) → "
        "angebot_kalkulieren (Aufschlag, Summen). Beträge in Cent. Maße in mm."
    ),
)

_client: OrderCityClient | None = None


def client() -> OrderCityClient:
    global _client
    if _client is None:
        _client = OrderCityClient()
    return _client


def _kompakt_feld(f: dict) -> dict:
    out = {"schluessel": f["schluessel"], "label": f["label"], "art": f["art"]}
    for k in ("einheit", "min", "max", "schritt", "standard", "pflicht", "hinweis", "sichtbarWenn"):
        if f.get(k) not in (None, "", []):
            out[k] = f[k]
    if f.get("optionen"):
        out["optionen"] = [
            {"wert": o["wert"], "label": o["label"], **({"aufpreisCents": o["aufpreisCents"]} if o.get("aufpreisCents") else {})}
            for o in f["optionen"]
        ]
    return out


def _preis_ergebnis(r: dict, menge: int) -> dict:
    preis = r.get("preis") or {}
    einzel_netto = preis.get("vkNettoCents")
    return {
        "gueltig": r["gueltig"],
        "fehler": r.get("fehler"),
        "einzelpreis_netto_cents": einzel_netto,
        "einzelpreis_brutto_cents": preis.get("vkBruttoCents"),
        "menge": menge,
        "gesamt_netto_cents": einzel_netto * menge if einzel_netto is not None else None,
        "posten": [{"label": p["label"], "betragCents": p["betragCents"]} for p in r.get("posten", [])],
        "hinweise": r.get("hinweise", []),
    }


@mcp.tool()
async def hersteller_auflisten() -> list[dict]:
    """Alle Hersteller mit verfügbarem Konfigurator (schluessel, name, beschreibung, anzahlProdukte, hatKategorien)."""
    return await client().hersteller()


@mcp.tool()
async def kategorien(hersteller: str) -> Any:
    """Kategoriebaum eines Herstellers (nur wenn hatKategorien=true)."""
    return await client().kategorien(hersteller)


@mcp.tool()
async def produkte_suchen(
    hersteller: str, suche: str | None = None, kategorie: str | None = None, limit: int = 20, offset: int = 0
) -> dict:
    """Produkte eines Herstellers, optional per Suchbegriff (z.B. 'Rollo', 'Plissee') oder Kategorie gefiltert."""
    seite = await client().produkte(hersteller, suche, kategorie, min(limit, 50), offset)
    return {
        "gesamt": seite["gesamt"],
        "produkte": [
            {k: p.get(k) for k in ("id", "name", "beschreibung", "kategorieName", "variante") if p.get(k)}
            for p in seite["produkte"]
        ],
    }


@mcp.tool()
async def produkt_konfigurator(hersteller: str, produkt_id: str) -> dict:
    """Konfigurator eines Produkts: Felder (Maße in mm mit min/max, Auswahl-Optionen, Schalter) und Grenzwerte.
    Felder mit sichtbarWenn gelten nur, wenn die Regel zutrifft."""
    s = await client().produkt_schema(hersteller, produkt_id)
    return {
        "produkt": {k: s["produkt"].get(k) for k in ("id", "name", "beschreibung", "kategorieName")},
        "felder": [_kompakt_feld(f) for f in s["felder"]],
        "grenzwerte": s["grenzwerte"],
    }


@mcp.tool()
async def stoffe_suchen(hersteller: str | None = None, suche: str | None = None, limit: int = 30) -> list[dict]:
    """Stoffe (z.B. für Rollos/Plissees), optional gefiltert nach Hersteller und Suchbegriff
    (Name, Nummer, Farbe, Typ wie 'dimout', 'verdunkelnd', 'schwer entflammbar')."""
    stoffe = await client().stoffe(hersteller)
    if suche:
        words = suche.lower().split()
        stoffe = [
            st for st in stoffe
            if all(
                w in " ".join(str(st.get(k) or "") for k in ("name", "nummer", "farbe", "typ", "material", "kollektion")).lower()
                or (w.startswith("schwer") and st.get("schwerEntflammbar"))
                for w in words
            )
        ]
    keys = ("id", "name", "nummer", "farbe", "typ", "preisgruppe", "schwerEntflammbar", "ballenbreiteMm")
    return [{k: st.get(k) for k in keys} for st in stoffe if st.get("aktiv", True)][:limit]


@mcp.tool()
async def konfiguration_berechnen(hersteller: str, produkt_id: str, werte: dict[str, Any], menge: int = 1) -> dict:
    """Prüft eine Konfiguration und liefert den Verkaufspreis (= unser Einkauf) je Stück und gesamt.
    werte: {feldschluessel: wert}, z.B. {"breiteMm": 1200, "hoeheMm": 1600, "typ": "A50Ku"}.
    Bei gueltig=false steht in 'fehler' der Grund (z.B. Maß außerhalb der Grenzen) – dann anpassen."""
    r = await client().berechne(hersteller, produkt_id, werte, 1)  # Einzelpreis; Gesamt rechnet der Code
    return _preis_ergebnis(r, menge)


@mcp.tool()
async def angebot_kalkulieren(positionen: list[dict], aufschlag_prozent: float | None = None) -> dict:
    """Kalkuliert ein Angebot aus bepreisten Positionen mit Aufschlag, Montage, USt und Summen.
    positionen: [{"nr": "1.1", "bezeichnung": "...", "menge": 4, "einkauf_netto_cents": 12345}]
    (einkauf_netto_cents = einzelpreis_netto_cents aus konfiguration_berechnen).
    aufschlag_prozent: leer = Standard aus der Konfiguration."""
    s = OrderCitySettings()
    k = kalkuliere(
        positionen,
        s.angebot_aufschlag_prozent if aufschlag_prozent is None else aufschlag_prozent,
        s.angebot_montage_pro_stueck_eur,
        s.ust_prozent,
    )
    return {**k.as_dict(), "markdown_intern": als_markdown(k, intern=True), "markdown_angebot": als_markdown(k, intern=False)}


def main() -> None:
    logging.basicConfig(level=logging.WARNING)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    mcp.run()


if __name__ == "__main__":
    main()
