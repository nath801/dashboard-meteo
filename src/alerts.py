"""Moteur d'alertes : compare les moyennes glissantes aux seuils OMS 2021 et détecte la canicule."""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import List, Optional, Tuple

import pandas as pd

from src import config

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class Alert:
    """Une alerte affichée dans le dashboard."""

    level: str        # "warning" (vigilance) ou "danger" (alerte)
    metric: str       # colonne concernée
    title: str        # ex. « Alerte Pic de Pollution NO2 »
    value: float      # moyenne glissante mesurée
    threshold: float  # seuil dépassé
    unit: str

    @property
    def message(self) -> str:
        """Texte lisible de l'alerte."""
        return f"{self.title} : {self.value:.1f} {self.unit} (seuil {self.threshold:g} {self.unit})"


def aqi_category(aqi: Optional[float]) -> Tuple[str, str]:
    """Renvoie (libellé, couleur) de la catégorie AQI US ; « Inconnu » si la valeur manque."""
    if aqi is None or pd.isna(aqi):
        return "Inconnu", "#bdbdbd"
    for upper, label, color in config.AQI_SCALE:
        if aqi <= upper:
            return label, color
    return config.AQI_SCALE[-1][1], config.AQI_SCALE[-1][2]  # pragma: no cover


class AlertEngine:
    """Évalue les règles d'alerte sur un DataFrame horaire (historique uniquement)."""

    @staticmethod
    def _rolling_last(df: pd.DataFrame, col: str, hours: int) -> Optional[float]:
        """Moyenne des `hours` dernières valeurs valides de la colonne."""
        if col not in df:
            return None
        s = df[col].dropna()
        if s.empty:
            return None
        return float(s.iloc[-hours:].mean())

    @classmethod
    def evaluate(cls, df: pd.DataFrame) -> List[Alert]:
        """Liste des alertes actives, les alertes « danger » en premier."""
        alerts: List[Alert] = []
        if df is None or df.empty:
            return alerts

        for col, (name, warn, danger, window, unit) in config.THRESHOLDS.items():
            value = cls._rolling_last(df, col, window)
            if value is None:
                continue
            if value >= danger:
                alerts.append(Alert("danger", col, f"Alerte Pic de Pollution {name}", value, danger, unit))
            elif value >= warn:
                alerts.append(Alert("warning", col, f"Vigilance {name}", value, warn, unit))

        warn, danger, window = config.HEAT_THRESHOLD
        feels = cls._rolling_last(df, "apparent_temperature", window)
        if feels is not None:
            if feels >= danger:
                alerts.append(Alert("danger", "apparent_temperature", "Alerte Canicule", feels, danger, "°C"))
            elif feels >= warn:
                alerts.append(Alert("warning", "apparent_temperature", "Vigilance Forte Chaleur",
                                    feels, warn, "°C"))

        alerts.sort(key=lambda a: 0 if a.level == "danger" else 1)
        for a in alerts:
            logger.info("Alerte détectée : %s", a.message)
        return alerts
