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
    model_delegate: str = "anthropic/claude-sonnet-5.5"
    model_smart: str = "anthropic/claude-sonnet-5.5"
    model_fast: str = "anthropic/claude-haiku-4.5"

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
