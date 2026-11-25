"""Configuration centrale du dashboard : villes, variables, seuils OMS, chemins et .env."""
from __future__ import annotations

import os
from pathlib import Path
from typing import Dict, Tuple

from dotenv import load_dotenv

ROOT: Path = Path(__file__).resolve().parent.parent
DATA_DIR: Path = ROOT / "data"
DATA_DIR.mkdir(exist_ok=True)

# Les éventuelles clés / réglages sont lus depuis .env (jamais commité).
load_dotenv(ROOT / ".env")


def _env_int(name: str, default: int) -> int:
    """Lit un entier dans les variables d'environnement (valeur par défaut si absente ou invalide)."""
    try:
        return int(os.getenv(name, default))
    except (TypeError, ValueError):
        return default


def _env_bool(name: str, default: bool) -> bool:
    """Lit un booléen (true/1/yes/oui) dans les variables d'environnement."""
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "oui", "on"}


# --------------------------------------------------------------------- Cache / réseau
CACHE_DB: Path = Path(os.getenv("CACHE_DB", DATA_DIR / "cache.db"))
CACHE_TTL_MINUTES: int = _env_int("CACHE_TTL_MINUTES", 30)
REQUEST_TIMEOUT: int = _env_int("REQUEST_TIMEOUT", 8)

WEATHER_URL = "https://api.open-meteo.com/v1/forecast"
AIR_URL = "https://air-quality-api.open-meteo.com/v1/air-quality"
GEOCODING_URL = "https://geocoding-api.open-meteo.com/v1/search"

# --------------------------------------------------------------------- Villes
CITIES: Dict[str, Dict[str, float]] = {
    "Paris": {"lat": 48.8566, "lon": 2.3522},
    "Lyon": {"lat": 45.7640, "lon": 4.8357},
    "Marseille": {"lat": 43.2965, "lon": 5.3698},
    "Londres": {"lat": 51.5074, "lon": -0.1278},
    "Madrid": {"lat": 40.4168, "lon": -3.7038},
}

# --------------------------------------------------------------------- Variables collectées
WEATHER_VARS = [
    "temperature_2m", "apparent_temperature", "relative_humidity_2m", "pressure_msl",
    "wind_speed_10m", "wind_direction_10m", "cloud_cover", "precipitation",
]
AIR_VARS = [
    "us_aqi", "european_aqi", "pm2_5", "pm10", "nitrogen_dioxide",
    "ozone", "carbon_monoxide", "sulphur_dioxide",
]

LABELS: Dict[str, str] = {
    "temperature_2m": "Température (°C)",
    "apparent_temperature": "T° ressentie (°C)",
    "relative_humidity_2m": "Humidité (%)",
    "pressure_msl": "Pression (hPa)",
    "wind_speed_10m": "Vent (km/h)",
    "wind_direction_10m": "Direction du vent (°)",
    "cloud_cover": "Nuages (%)",
    "precipitation": "Précipitations (mm)",
    "us_aqi": "AQI US",
    "european_aqi": "AQI européen",
    "pm2_5": "PM2.5 (µg/m³)",
    "pm10": "PM10 (µg/m³)",
    "nitrogen_dioxide": "NO2 (µg/m³)",
    "ozone": "O3 (µg/m³)",
    "carbon_monoxide": "CO (µg/m³)",
    "sulphur_dioxide": "SO2 (µg/m³)",
    "comfort_score": "Score confort (/100)",
}

# --------------------------------------------------------------------- Seuils OMS 2021
# colonne -> (nom court, vigilance, alerte, fenêtre de moyenne glissante en heures, unité)
THRESHOLDS: Dict[str, Tuple[str, float, float, int, str]] = {
    "pm2_5": ("PM2.5", 15.0, 25.0, 24, "µg/m³"),
    "pm10": ("PM10", 45.0, 75.0, 24, "µg/m³"),
    "nitrogen_dioxide": ("NO2", 25.0, 50.0, 24, "µg/m³"),
    "ozone": ("O3", 100.0, 160.0, 8, "µg/m³"),
    "sulphur_dioxide": ("SO2", 40.0, 125.0, 24, "µg/m³"),
    "carbon_monoxide": ("CO", 4000.0, 10000.0, 24, "µg/m³"),
}
HEAT_THRESHOLD: Tuple[float, float, int] = (33.0, 38.0, 1)  # T° ressentie : vigilance, alerte, fenêtre

# Échelle AQI US : (borne max, libellé, couleur)
AQI_SCALE = [
    (50, "Bon", "#00e400"),
    (100, "Modéré", "#ffff00"),
    (150, "Mauvais (sensibles)", "#ff7e00"),
    (200, "Mauvais", "#ff0000"),
    (300, "Très mauvais", "#8f3f97"),
    (float("inf"), "Dangereux", "#7e0023"),
]
