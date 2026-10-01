"""Client für die öffentliche OrderCity-API (/api/v1). Alle Beträge sind Cent in EUR.

Bewusst nur lesende Endpunkte und /berechne – Warenkorb und Bestellung werden nicht angebunden,
damit der Bot niemals etwas kauft.
"""

import asyncio
import time
from typing import Any

import httpx
from pydantic_settings import BaseSettings, SettingsConfigDict


class OrderCitySettings(BaseSettings):
    """Eigene Settings, damit der MCP-Server auch ohne Telegram-/OpenRouter-Konfiguration läuft."""

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    ordercity_api_key: str = ""
    ordercity_base_url: str = "https://ordercity.vonreyher.media"
    # Aufschlag auf den OrderCity-Verkaufspreis für Ausschreibungsangebote
    angebot_aufschlag_prozent: float = 50.0
    # Montage je Stück (netto, EUR). 0 = nicht einkalkuliert (wird als offene Frage markiert)
    angebot_montage_pro_stueck_eur: float = 0.0
    ust_prozent: float = 19.0

    @classmethod
    def settings_customise_sources(cls, settings_cls, init_settings, env_settings, dotenv_settings, file_secret_settings):
        return init_settings, dotenv_settings, env_settings, file_secret_settings


class OrderCityError(RuntimeError):
    pass


class OrderCityClient:
    CACHE_TTL_S = 3600

    def __init__(self, settings: OrderCitySettings | None = None):
        self.settings = settings or OrderCitySettings()
        if not self.settings.ordercity_api_key:
            raise OrderCityError("ORDERCITY_API_KEY fehlt in der .env")
        self._http = httpx.AsyncClient(
            base_url=self.settings.ordercity_base_url.rstrip("/"),
            headers={"Authorization": f"Bearer {self.settings.ordercity_api_key}"},
            timeout=60,
        )
        self._cache: dict[str, tuple[float, Any]] = {}

    async def close(self) -> None:
        await self._http.aclose()

    async def _request(self, method: str, path: str, *, cache: bool = False, **kw) -> Any:
        key = f"{method} {path} {kw.get('params')}"
        if cache and (hit := self._cache.get(key)) and time.monotonic() - hit[0] < self.CACHE_TTL_S:
            return hit[1]
        for attempt in range(4):
            r = await self._http.request(method, path, **kw)
            if r.status_code == 429 or r.status_code >= 502:
                await asyncio.sleep(2**attempt)
                continue
            break
        if r.status_code >= 400:
            try:
                msg = r.json().get("error")
            except Exception:
                msg = r.text[:300]
            raise OrderCityError(f"OrderCity {r.status_code} bei {method} {path}: {msg}")
        data = r.json()
        if cache:
            self._cache[key] = (time.monotonic(), data)
        return data

    async def me(self) -> dict:
        return await self._request("GET", "/api/v1/me")

    async def hersteller(self) -> list[dict]:
        return (await self._request("GET", "/api/v1/hersteller", cache=True))["hersteller"]

    async def kategorien(self, hersteller: str) -> Any:
        return await self._request("GET", f"/api/v1/hersteller/{hersteller}/kategorien", cache=True)

    async def produkte(
        self, hersteller: str, suche: str | None = None, kategorie: str | None = None, limit: int = 20, offset: int = 0
    ) -> dict:
        params = {k: v for k, v in {"suche": suche, "kategorie": kategorie, "limit": limit, "offset": offset}.items() if v is not None}
        return await self._request("GET", f"/api/v1/hersteller/{hersteller}/produkte", params=params, cache=True)

    async def produkt_schema(self, hersteller: str, produkt_id: str) -> dict:
        return await self._request("GET", f"/api/v1/hersteller/{hersteller}/produkte/{produkt_id}", cache=True)

    async def stoffe(self, hersteller: str | None = None) -> list[dict]:
        params = {"hersteller": hersteller} if hersteller else None
        return (await self._request("GET", "/api/v1/stoffe", params=params, cache=True))["stoffe"]

    async def berechne(self, hersteller: str, produkt_id: str, werte: dict[str, Any], menge: int = 1) -> dict:
        return await self._request(
            "POST",
            f"/api/v1/hersteller/{hersteller}/produkte/{produkt_id}/berechne",
            json={"werte": werte, "menge": menge},
        )
