# Decocity Ausschreibungsbot

Telegram-Bot, der für die **Decocity Sonnenschutz GmbH & Co. KG** öffentliche Ausschreibungen auf
service.bund.de findet, bewertet, die Vergabeunterlagen recherchiert, Rückfragen stellt und
Angebotsentwürfe schreibt. Abgegeben wird nur nach Freigabe per Button.

**Stack:** Python 3.13 · uv · LangGraph/LangChain (Agents + Interrupts) · OpenRouter · Playwright MCP · aiogram

**Modelle (über OpenRouter, in `.env` änderbar):**

| Rolle | Modell | $/1 Mio. Tokens (Ein/Aus) |
|---|---|---|
| Delegate-Agent (`MODEL_DELEGATE`) | `inclusionai/ling-3.0-flash` (Reasoning) | 0,021 / 0,063 |
| Sub-Agents: Recherche, Angebot, Vollbewertung (`MODEL_SUBAGENT`) | `deepseek/deepseek-v4.1-flash` | 0,03 / 0,50 |
| Vorbewertung, Zusammenfassungen (`MODEL_FAST`) | `google/gemini-2.5-flash-lite` | 0,10 / 0,40 |
| Kalkulations-Agent (`MODEL_KALKULATION`, Reasoning) | `deepseek/deepseek-v4.1-flash` | 0,03 / 0,50 |

## Architektur

```
                         Telegram (aiogram)
                               │  ▲  Fragen / Freigabe-Buttons
                               ▼  │
                    ┌────────────────────────┐
                    │   Delegate-Agent       │  LangGraph + SQLite-Checkpointer
                    │   (spricht mit euch)   │  pausiert per interrupt() bei Fragen
                    └───────────┬────────────┘
        ┌───────────────┬───────┴────────┬──────────────────┐
        ▼               ▼                ▼                  ▼
  Scout (ohne LLM)   Analyst         Recherche-Agent     Angebots-Agent
  RSS service.bund   Vorbewertung +  Playwright MCP,     schreibt Entwurf
                     Vollbewertung   PDFs/ZIPs lesen     (Markdown)
                     (Go/No-Go)
```

- **Scout** – fragt die RSS-Feeds von service.bund.de ab (alle `POLL_INTERVAL_MINUTES`).
- **Analyst** – Vorbewertung jedes neuen Treffers (schnelles Modell). Nach der Recherche folgt die
  Vollbewertung der gesamten Ausschreibung inkl. Unterlagen (Go/No-Go, Gewinnchance, Aufwand, Risiken).
- **Jev (TypeSafe AI)** – wenn `TYPESAFE_API_KEY` gesetzt ist, trifft Jev die Entscheidungen des
  Analysten: Vorbewertung (Passung, Kernleistung, Region, Leistungsart) und in der Vollbewertung
  Go/No-Go, Gewinnchance, Aufwand, Eignung und Frist – jeweils mit Wahrscheinlichkeit. Ist Jev
  unsicher (< 60 %), lautet die Empfehlung „prüfen“. Begründung, fehlende Nachweise und Risiken
  schreibt parallel das LLM. Ohne Key oder bei Fehlern bewertet das LLM allein.
- **Recherche-Agent** – öffnet die Bekanntmachung auf der Vergabeplattform, lädt Unterlagen herunter
  und liest sie. Meldet sich nie an und schickt nichts ab.
- **Angebots-Agent** – Anschreiben, Konzept, Nachweis-Checkliste und Preisblatt (fehlende Preise
  werden als `[PREIS FEHLT]` markiert, nie erfunden).
- **Delegate-Agent** – steuert den Ablauf, stellt Rückfragen gebündelt per Telegram und merkt sich
  dauerhaft gültige Antworten in `profile/gelernt.md`.

### Sicherheit
- Die Freigabe setzt **nur der Telegram-Button** im Code, nicht das LLM.
- `ALLOW_AUTO_SUBMIT=false` (Standard): Der Bot gibt nie selbst ab, sondern bereitet alles vor
  (Status `bereit_zur_abgabe`).
- Zugriff nur für freigegebene Chat-IDs (oder den ersten `/start`-Nutzer).

## Unterlagen, Datenbank & OCR

Für jede relevante Ausschreibung (automatisch beim Scout-Lauf und vor jeder Recherche):

1. **Herunterladen** – Bekanntmachungs-PDFs und Vergabeunterlagen von der Plattform. Gibt es „Alle
   Dokumente als ZIP“, wird nur das geladen (statt jeder Datei einzeln).
2. **Bekanntmachung als PDF** – bietet die Plattform keine an (nur HTML), druckt der Browser die
   Bekanntmachungs- und Verfahrensseiten als `Bekanntmachung*.pdf`.
3. **Entpacken & registrieren** – jede Datei steht in der Tabelle `documents` (Art, Quelle, SHA-256,
   Seiten, Text). Doppelte Inhalte werden erkannt.
4. **Text & OCR** – PDF-Text wird direkt gelesen; gescannte Seiten liest ein Vision-Modell
   (`MODEL_OCR`). DOCX und GAEB-XML werden ebenfalls gelesen. Der Text liegt in der DB und als
   `*.extrakt.txt` neben der Datei – Recherche- und Mengen-Agent lesen ihn über `dokument_lesen`.

Ablage: `data/bids/<id>/unterlagen/`. Im Chat: `/unterlagen <Nr>` (Liste + Bekanntmachungs-PDFs).

## Live-Protokoll im Chat

Bei jedem Agent-Lauf erscheint eine Nachricht, die laufend ergänzt wird:

```
📜 #12 Med. Schule – Sonnenschutz
0:02 🤖 Delegate → 🔎 Recherche: auftrag=Lose und Nachweise prüfen
0:03    📥 Vergabeunterlagen_CXP9Y6EHX4E.zip (20454 KB)
0:41    🖨 Bekanntmachung.pdf aus der Webseite erzeugt (kein PDF angeboten)
0:55 🔎 Recherche → 🤖 Delegate: 1. Leistungsgegenstand: Los 3 Sonnenschutz …
1:20 🧮 Kalkulation → 🛠 Konfigurator: Pos. 3.1 Innenrollo – 24× 1250×1800 mm
1:31    ✅ Pos. 3.1: Rollo A50 – Preis bei OrderCity geprüft (272,00 €/Stk)
```

`/verlauf kurz` (Standard: Übergaben zwischen Agents) · `/verlauf voll` (+ jeder Tool-Aufruf) · `/verlauf aus`.

## Kalkulation mit OrderCity

```
Kalkulations-Agent (Reasoning)
 ├─ Mengen-Agent         liest Recherche + Leistungsverzeichnis → Positionen (Art, Maße, Menge)
 ├─ Konfigurator-Agent   je Position: Produkt suchen, konfigurieren, berechnen  ──► OrderCity-MCP
 └─ kalkulation_abschliessen   Aufschlag, Montage, USt, Summen (reiner Code)
```

- **OrderCity-MCP** (`uv run ordercity-mcp`, stdio): `hersteller_auflisten`, `kategorien`, `produkte_suchen`,
  `produkt_konfigurator`, `stoffe_suchen`, `konfiguration_berechnen`, `angebot_kalkulieren`.
  **Ohne Warenkorb/Bestellung** – über den Bot kann nichts gekauft werden. Lässt sich auch in
  Claude Desktop o. ä. einbinden (`command: uv`, `args: ["run", "ordercity-mcp"]`, im Projektordner).
- Gerechnet wird nur im Code. Jeder Preis wird nach dem Konfigurator-Agent nochmals direkt gegen
  die API geprüft; das LLM gibt keine Beträge weiter.
- Ergebnis in `data/bids/<id>/`: `kalkulation.md` (**intern**: Einkauf, Aufschlag, Rohertrag),
  `kalkulation_angebot.md` (Preisblatt ohne interne Zahlen – nur das nutzt der Angebots-Agent),
  `kalkulation.json`.
- Freie Preisanfragen im Chat: „Was kosten 4 Plissees 80×120 mit Verdunkelung?“

## Einrichtung

```bash
uv sync
cp example.env .env        # Token & Keys eintragen
```

- Telegram-Token: bei [@BotFather](https://t.me/BotFather) `/newbot`
- OpenRouter-Key: https://openrouter.ai/keys
- Node.js wird für Playwright MCP benötigt (`npx`). Er nutzt standardmäßig das installierte Chrome;
  fehlt es: `npx playwright install chrome`

## Starten

```bash
./scripts/bot.sh start     # im Hintergrund
./scripts/bot.sh logs      # Logs verfolgen
./scripts/bot.sh status
./scripts/bot.sh stop

uv run ausschreibungsbot   # alternativ im Vordergrund
```

Dann in Telegram `/start` an den Bot schicken.

## Bedienung

| Befehl | |
|---|---|
| `/suchen` | sofort nach neuen Ausschreibungen suchen |
| `/liste` | bekannte Ausschreibungen |
| `/unterlagen <Nr>` | gesicherte Unterlagen + Bekanntmachungs-PDFs |
| `/verlauf kurz\|voll\|aus` | Live-Protokoll der Agents |
| `/profil` | Firmenprofil anzeigen |
| `/neu` | neues Gespräch |
| freier Text | z.B. „Such nach Markisen“, „Was ist mit #3?“ |

Auf jeder gemeldeten Ausschreibung: **📝 Bewerbung vorbereiten**, **🔎 Details**, **🙈 Ignorieren**.
Fragen des Bots einfach per „Antworten“ beantworten.

## Nutzer freischalten

Wer zuerst `/start` schickt, wird Admin (ebenso alle IDs in `TELEGRAM_ALLOWED_CHAT_IDS`).
Neue Personen schicken dem Bot `/start` – alle Admins bekommen eine Anfrage mit
**✅ Freigeben / ❌ Ablehnen**.

| Befehl (nur Admins) | |
|---|---|
| `/nutzer` | alle Nutzer und offenen Anfragen mit Chat-ID |
| `/freigeben <ID>` | Nutzer freischalten |
| `/admin <ID>` | Nutzer zum Admin machen |
| `/entziehen <ID>` | Zugriff entziehen |

Freigeschaltete Nutzer bekommen die Ausschreibungs-Meldungen und können mit dem Bot arbeiten.
**Angebote freigeben dürfen nur Admins** – arbeitet ein Nutzer an einer Bewerbung, geht die
Freigabe-Anfrage zusätzlich an alle Admins.

## Dateien

- `profile/decocity.md` – Firmenprofil (ergänzen: Referenzen, Zertifikate, Umsätze …)
- `profile/gelernt.md` – vom Bot gelernte Fakten
- `data/bids/<id>/` – `recherche.md`, `bewertung.md`, `kalkulation*.md`, `angebot_entwurf.md`
- `data/downloads/` – heruntergeladene Vergabeunterlagen
- `data/*.sqlite` – Ausschreibungen und Gesprächszustände

## Suche anpassen

`SEARCH_TERMS`, `SEARCH_CITY`, `SEARCH_OPENGEO_ID`, `SEARCH_RADIUS_KM` in `.env`.
Die Geo-ID eines anderen Ortes steht nach einer Suche auf service.bund.de in der URL (`solr_opengeo_id=`).
