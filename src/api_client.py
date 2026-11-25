"""Client des API Open-Meteo (météo, qualité de l'air, géocodage) avec cache SQLite à durée de validité.

Ordre de récupération : mode simulé demandé → cache valide → API → données simulées si l'API échoue.
"""
from __future__ import annotations

import datetime as dt
import json
import logging
import sqlite3
from pathlib import Path
from typing import Any, Dict, Iterable, Optional

import numpy as np
import requests

from src import config

logger = logging.getLogger(__name__)


class APIError(Exception):
    """Erreur lors d'un appel API (réseau, code HTTP, JSON invalide ou réponse d'erreur)."""


class CacheManager:
    """Cache clé/valeur persistant dans SQLite avec durée de validité (TTL)."""

    def __init__(self, db_path: Path | str = config.CACHE_DB,
                 ttl_minutes: int = config.CACHE_TTL_MINUTES) -> None:
        self.db_path = str(db_path)
        self.ttl = dt.timedelta(minutes=ttl_minutes)
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self.db_path, timeout=5)

    def _init_db(self) -> None:
        """Crée la table de cache si elle n'existe pas."""
        with self._connect() as conn:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS api_cache ("
                " key TEXT PRIMARY KEY, response TEXT NOT NULL, timestamp TEXT NOT NULL)"
            )

    def get(self, key: str) -> Optional[Dict[str, Any]]:
        """Renvoie la réponse en cache si elle existe et n'a pas expiré, sinon None."""
        try:
            with self._connect() as conn:
                row = conn.execute(
                    "SELECT response, timestamp FROM api_cache WHERE key = ?", (key,)
                ).fetchone()
        except sqlite3.Error as exc:
            logger.warning("Lecture du cache impossible : %s", exc)
            return None
        if row is None:
            return None
        response, timestamp = row
        try:
            saved = dt.datetime.fromisoformat(timestamp)
        except ValueError:
            return None
        if dt.datetime.now() - saved > self.ttl:
            logger.info("Cache expiré pour %s", key)
            return None
        try:
            return json.loads(response)
        except json.JSONDecodeError:
            logger.warning("Entrée de cache corrompue pour %s", key)
            return None

    def set(self, key: str, data: Dict[str, Any]) -> None:
        """Enregistre (ou remplace) une réponse dans le cache."""
        try:
            with self._connect() as conn:
                conn.execute(
                    "INSERT OR REPLACE INTO api_cache (key, response, timestamp) VALUES (?, ?, ?)",
                    (key, json.dumps(data), dt.datetime.now().isoformat()),
                )
        except sqlite3.Error as exc:
            logger.error("Écriture du cache impossible : %s", exc)

    def clear(self) -> None:
        """Vide entièrement le cache."""
        with self._connect() as conn:
            conn.execute("DELETE FROM api_cache")


class WeatherAPIClient:
    """Récupère les données météo et qualité de l'air d'Open-Meteo, avec cache et repli simulé."""

    def __init__(self, cache: Optional[CacheManager] = None,
                 timeout: int = config.REQUEST_TIMEOUT) -> None:
        self.cache = cache or CacheManager()
        self.timeout = timeout
        self.session = requests.Session()
        self.last_source: str = "api"  # "api", "cache" ou "mock"
        self.last_error: Optional[str] = None

    # ----------------------------------------------------------------- HTTP
    def _get_json(self, url: str, params: Dict[str, Any]) -> Dict[str, Any]:
        """GET + décodage JSON ; toute erreur est convertie en APIError."""
        try:
            resp = self.session.get(url, params=params, timeout=self.timeout)
            resp.raise_for_status()
            data = resp.json()
        except requests.RequestException as exc:
            # HTTPError hérite de RequestException : on distingue grâce à exc.response.
            if getattr(exc, "response", None) is not None:
                raise APIError(f"Erreur HTTP {exc.response.status_code} sur {url}") from exc
            raise APIError(f"Erreur réseau : {exc}") from exc
        except ValueError as exc:  # JSONDecodeError
            raise APIError(f"Réponse JSON invalide de {url}") from exc
        if not isinstance(data, dict):
            raise APIError("Réponse inattendue (objet JSON attendu)")
        if data.get("error"):
            raise APIError(f"Erreur renvoyée par l'API : {data.get('reason', 'inconnue')}")
        return data

    # ----------------------------------------------------------------- Données
    def fetch(self, lat: float, lon: float, past_days: int = 7, forecast_days: int = 2,
              mock_mode: bool = False) -> Dict[str, Any]:
        """Renvoie {'weather': ..., 'air_quality': ...} pour une position.

        Args:
            lat: latitude.
            lon: longitude.
            past_days: nombre de jours d'historique.
            forecast_days: nombre de jours de prévision.
            mock_mode: force l'utilisation des données simulées.
        """
        if mock_mode:
            self.last_source = "mock"
            return generate_mock_data(lat, lon, past_days, forecast_days)

        key = f"meteo_air_{lat:.4f}_{lon:.4f}_{past_days}_{forecast_days}"
        cached = self.cache.get(key)
        if cached is not None:
            self.last_source = "cache"
            return cached

        common = {"latitude": lat, "longitude": lon, "timezone": "auto",
                  "past_days": past_days, "forecast_days": forecast_days}
        try:
            weather = self._get_json(config.WEATHER_URL, {
                **common, "current": ",".join(config.WEATHER_VARS),
                "hourly": ",".join(config.WEATHER_VARS)})
            air = self._get_json(config.AIR_URL, {
                **common, "current": ",".join(config.AIR_VARS),
                "hourly": ",".join(config.AIR_VARS)})
        except APIError as exc:
            logger.error("API indisponible (%s) : bascule sur les données simulées", exc)
            self.last_error = str(exc)
            self.last_source = "mock"
            return generate_mock_data(lat, lon, past_days, forecast_days)

        data = {"weather": weather, "air_quality": air}
        self.cache.set(key, data)
        self.last_source = "api"
        self.last_error = None
        return data

    def geocode(self, city: str) -> Optional[Dict[str, Any]]:
        """Recherche une ville par son nom ; renvoie {name, country, lat, lon} ou None."""
        city = city.strip()
        if not city:
            return None
        key = f"geo_{city.lower()}"
        cached = self.cache.get(key)
        if cached is None:
            try:
                cached = self._get_json(config.GEOCODING_URL,
                                        {"name": city, "count": 1, "language": "fr", "format": "json"})
            except APIError as exc:
                logger.warning("Géocodage impossible pour %r : %s", city, exc)
                return None
            self.cache.set(key, cached)
        results = cached.get("results") or []
        if not results:
            return None
        r = results[0]
        return {"name": r.get("name", city), "country": r.get("country", ""),
                "lat": float(r["latitude"]), "lon": float(r["longitude"])}


def fetch_all_cities(client: WeatherAPIClient, cities: Dict[str, Dict[str, float]],
                     mock_mode: bool = False) -> Dict[str, Dict[str, Any]]:
    """Récupère les données de plusieurs villes (utilisé par la carte)."""
    return {name: client.fetch(c["lat"], c["lon"], mock_mode=mock_mode) for name, c in cities.items()}


# --------------------------------------------------------------------- Données simulées
def _series(rng: np.random.Generator, n: int, hours: np.ndarray, base: float, amp: float,
            noise: float, low: float = 0.0, phase: float = 0.0) -> list:
    """Série horaire réaliste : moyenne + cycle journalier + bruit, bornée en bas."""
    values = base + amp * np.sin(2 * np.pi * (hours - phase) / 24) + rng.normal(0, noise, n)
    return [round(float(v), 1) for v in np.maximum(values, low)]


def generate_mock_data(lat: float, lon: float, past_days: int = 7,
                       forecast_days: int = 2) -> Dict[str, Any]:
    """Génère des données déterministes (seed 42) au format exact des API Open-Meteo.

    Les horodatages sont recalés sur l'heure locale actuelle pour que le dashboard
    affiche un historique et des prévisions cohérents.
    """
    rng = np.random.default_rng(42)
    local = dt.datetime.now().astimezone()  # heure locale du poste (comme timezone=auto)
    offset = int(local.utcoffset().total_seconds())
    start = local.replace(hour=0, minute=0, second=0, microsecond=0, tzinfo=None) - dt.timedelta(days=past_days)
    n = 24 * (past_days + forecast_days)
    times = [(start + dt.timedelta(hours=i)).strftime("%Y-%m-%dT%H:%M") for i in range(n)]
    hours = np.array([(start + dt.timedelta(hours=i)).hour for i in range(n)])

    temp = _series(rng, n, hours, 14, 5, 0.8, low=-30, phase=9)
    weather_hourly: Dict[str, Iterable] = {
        "time": times,
        "temperature_2m": temp,
        "apparent_temperature": [round(t - 2 + float(rng.normal(0, 0.5)), 1) for t in temp],
        "relative_humidity_2m": _series(rng, n, hours, 70, -15, 4, low=5, phase=9),
        "pressure_msl": _series(rng, n, hours, 1015, 2, 1, low=950),
        "wind_speed_10m": _series(rng, n, hours, 12, 4, 3),
        "wind_direction_10m": [int(v) % 360 for v in rng.uniform(0, 360, n)],
        "cloud_cover": [int(v) for v in rng.uniform(0, 100, n)],
        "precipitation": [round(float(v), 1) if v > 0.5 else 0.0 for v in rng.exponential(0.3, n)],
    }
    no2 = _series(rng, n, hours, 28, 10, 4, phase=3)
    air_hourly: Dict[str, Iterable] = {
        "time": times,
        "pm2_5": _series(rng, n, hours, 12, 4, 2),
        "pm10": _series(rng, n, hours, 20, 6, 3),
        "nitrogen_dioxide": no2,
        "ozone": _series(rng, n, hours, 55, 25, 6, phase=9),
        "carbon_monoxide": _series(rng, n, hours, 250, 60, 20, phase=3),
        "sulphur_dioxide": _series(rng, n, hours, 3, 1, 0.5),
    }
    air_hourly["us_aqi"] = [int(min(200, max(10, 2.5 * p + 10))) for p in air_hourly["pm2_5"]]
    air_hourly["european_aqi"] = [int(min(150, max(5, 1.5 * v + 5))) for v in no2]

    now_idx = 24 * past_days + local.hour
    current_w = {k: v[now_idx] for k, v in weather_hourly.items() if k != "time"}
    current_a = {k: v[now_idx] for k, v in air_hourly.items() if k != "time"}
    base = {"latitude": lat, "longitude": lon, "timezone": "local", "utc_offset_seconds": offset}
    return {
        "weather": {**base, "current": {"time": times[now_idx], **current_w}, "hourly": weather_hourly},
        "air_quality": {**base, "current": {"time": times[now_idx], **current_a}, "hourly": air_hourly},
        "mock": True,
    }
