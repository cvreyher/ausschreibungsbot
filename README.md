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
- `data/bids/<id>/` – `recherche.md`, `bewertung.md`, `angebot_entwurf.md`
- `data/downloads/` – heruntergeladene Vergabeunterlagen
- `data/*.sqlite` – Ausschreibungen und Gesprächszustände

## Suche anpassen

`SEARCH_TERMS`, `SEARCH_CITY`, `SEARCH_OPENGEO_ID`, `SEARCH_RADIUS_KM` in `.env`.
Die Geo-ID eines anderen Ortes steht nach einer Suche auf service.bund.de in der URL (`solr_opengeo_id=`).
