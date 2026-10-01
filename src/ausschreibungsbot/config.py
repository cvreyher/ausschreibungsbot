from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # Telegram
    telegram_bot_token: str
    # Kommagetrennte Chat-IDs. Leer = der erste, der /start schickt, wird Besitzer.
    telegram_allowed_chat_ids: str = ""

    # OpenRouter
    openrouter_api_key: str
    openrouter_base_url: str = "https://openrouter.ai/api/v1"
    # Delegate-Agent: günstiges Reasoning-Modell
    model_delegate: str = "inclusionai/ling-3.0-flash"
    # low | medium | high – leer = Modell-Standard
    delegate_reasoning_effort: str = "low"
    # Sub-Agents (Recherche, Angebot, Vollbewertung)
    model_subagent: str = "deepseek/deepseek-v4.1-flash"
    # Vorbewertung und Zusammenfassungen
    model_fast: str = "google/gemini-2.5-flash-lite"

    # Kalkulations-Agent (Reasoning) – seine Sub-Agents nutzen MODEL_SUBAGENT
    model_kalkulation: str = "deepseek/deepseek-v4.1-flash"
    kalkulation_reasoning_effort: str = "medium"

    # OCR für gescannte PDF-Seiten (Vision-Modell über OpenRouter). Leer = kein OCR.
    model_ocr: str = "google/gemini-2.5-flash-lite"
    # Max. OCR-Seiten pro Ausschreibung und Lauf (Kostenschutz)
    ocr_max_seiten: int = 150
    # Seiten mit weniger eingebettetem Text gelten als Scan und werden per OCR gelesen
    ocr_min_zeichen_pro_seite: int = 80

    # OrderCity-Großhandel (Produkte konfigurieren und bepreisen)
    ordercity_api_key: str = ""
    ordercity_base_url: str = "https://ordercity.vonreyher.media"
    # Aufschlag auf den OrderCity-Preis für Angebote (%)
    angebot_aufschlag_prozent: float = 50.0
    # Montage je Stück netto in EUR (0 = nicht einkalkuliert)
    angebot_montage_pro_stueck_eur: float = 0.0

    # TypeSafe AI – Jev als Entscheidungsmodell für den Analyst (leer = nur LLM)
    typesafe_api_key: str = ""
    model_jev: str = "jev-latest"

    # Suche auf service.bund.de
    search_terms: str = (
        "Blendschutz,Sichtschutz,Sonnenschutz,Jalousien,Rollo,Vorhänge,Markisen,"
        "Rollladen,Raffstore,Insektenschutz,Lamellenvorhang,Plissee"
    )
    search_city: str = "Berlin"
    # Geo-ID von service.bund.de für den Ort (Berlin = 14356). Leer = bundesweit.
    search_opengeo_id: str = "14356"
    # Erlaubt: 10, 20, 30, 50, 75, 90
    search_radius_km: int = 50
    poll_interval_minutes: int = 360
    min_score_notify: int = 50

    # Pfade
    data_dir: Path = Path("data")
    profile_dir: Path = Path("profile")

    # Browser / Abgabe
    playwright_headless: bool = True
    # Sicherheitsschalter: Ohne True gibt der Bot niemals selbst ein Angebot ab.
    allow_auto_submit: bool = False

    @classmethod
    def settings_customise_sources(cls, settings_cls, init_settings, env_settings, dotenv_settings, file_secret_settings):
        # Die .env des Projekts hat Vorrang vor Shell-Variablen – sonst greift z.B. ein
        # TELEGRAM_BOT_TOKEN aus ~/.zshrc und der Bot läuft mit dem Token eines anderen Bots.
        return init_settings, dotenv_settings, env_settings, file_secret_settings

    @property
    def allowed_chat_ids(self) -> set[int]:
        return {int(x) for x in self.telegram_allowed_chat_ids.split(",") if x.strip()}

    @property
    def terms(self) -> list[str]:
        return [t.strip() for t in self.search_terms.split(",") if t.strip()]

    @property
    def bids_dir(self) -> Path:
        return self.data_dir / "bids"

    @property
    def downloads_dir(self) -> Path:
        return self.data_dir / "downloads"


@lru_cache
def get_settings() -> Settings:
    s = Settings()
    for p in (s.data_dir, s.bids_dir, s.downloads_dir, s.profile_dir):
        p.mkdir(parents=True, exist_ok=True)
    return s
