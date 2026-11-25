"""Traitement des données : construction du DataFrame, nettoyage, score de confort, statistiques, tendances."""
from __future__ import annotations

import logging
from typing import Any, Dict, Iterable, List, Optional

import numpy as np
import pandas as pd

from src import config

logger = logging.getLogger(__name__)

NON_NEGATIVE = [c for c in config.WEATHER_VARS + config.AIR_VARS
                if c not in ("temperature_2m", "apparent_temperature")]


class DataProcessor:
    """Transformations pandas (méthodes statiques, sans état)."""

    @staticmethod
    def to_dataframe(raw: Dict[str, Any]) -> pd.DataFrame:
        """Fusionne les séries horaires météo et air dans un DataFrame indexé par `datetime`.

        Nettoyage : doublons supprimés, valeurs négatives impossibles → NaN,
        interpolation temporelle des trous de 3 h maximum, ajout de `comfort_score`.
        """
        w = (raw.get("weather") or {}).get("hourly") or {}
        a = (raw.get("air_quality") or {}).get("hourly") or {}
        if not w.get("time") or not a.get("time"):
            return pd.DataFrame()
        df_w = pd.DataFrame(w)
        df_a = pd.DataFrame(a)
        df = pd.merge(df_w, df_a, on="time", how="outer")
        df["datetime"] = pd.to_datetime(df.pop("time"))
        df = (df.drop_duplicates(subset="datetime", keep="last")
                .sort_values("datetime").set_index("datetime"))
        df = df.apply(pd.to_numeric, errors="coerce")
        for col in NON_NEGATIVE:
            if col in df:
                df.loc[df[col] < 0, col] = np.nan
        df = df.interpolate(method="time", limit=3, limit_area="inside")
        df["comfort_score"] = DataProcessor.comfort_score(df)
        return df

    @staticmethod
    def comfort_score(df: pd.DataFrame) -> pd.Series:
        """Score Santé/Confort 0-100 (100 = idéal).

        100 − (|T_ressentie − 21| × 2 + |HR − 50| × 0,4 + PM2.5/15 × 15 + NO2/25 × 10)
        Les polluants sont normalisés par leur seuil OMS.
        """
        def col(name: str) -> pd.Series:
            return df[name] if name in df else pd.Series(np.nan, index=df.index)

        feels = col("apparent_temperature").fillna(col("temperature_2m"))
        penalty = ((feels - 21).abs() * 2 + (col("relative_humidity_2m") - 50).abs() * 0.4
                   + col("pm2_5") / 15 * 15 + col("nitrogen_dioxide") / 25 * 10)
        return (100 - penalty).clip(0, 100).round(1)

    @staticmethod
    def split_past_future(df: pd.DataFrame, now: pd.Timestamp) -> tuple[pd.DataFrame, pd.DataFrame]:
        """Sépare l'historique (≤ maintenant) des prévisions (> maintenant)."""
        return df[df.index <= now], df[df.index > now]

    @staticmethod
    def latest_valid(df: pd.DataFrame, cols: Optional[Iterable[str]] = None) -> pd.Series:
        """Dernière valeur non vide de chaque colonne (évite le NaN des prévisions/lacunes)."""
        cols = [c for c in (cols or df.columns) if c in df]
        if df.empty:
            return pd.Series(dtype=float)
        return pd.Series({c: (df[c].dropna().iloc[-1] if df[c].notna().any() else np.nan) for c in cols})

    @staticmethod
    def stats(df: pd.DataFrame, cols: Iterable[str], last_hours: int = 24) -> pd.DataFrame:
        """Moyenne, médiane, min, max et écart-type sur les `last_hours` dernières heures."""
        cols = [c for c in cols if c in df]
        window = df[cols].iloc[-last_hours:] if last_hours else df[cols]
        out = pd.DataFrame({
            "Moyenne": window.mean(), "Médiane": window.median(), "Min": window.min(),
            "Max": window.max(), "Écart-type": window.std(),
        }).round(2)
        out.index = [config.LABELS.get(c, c) for c in out.index]
        return out

    @staticmethod
    def trend(df: pd.DataFrame, col: str, hours: int = 24) -> Dict[str, Any]:
        """Tendance d'une variable sur `hours` heures.

        - delta : moyenne de la fenêtre courante − moyenne de la fenêtre précédente ;
        - delta_pct : même chose en % ;
        - slope_per_day : pente de la régression linéaire sur la fenêtre courante (unité/jour) ;
        - direction : « hausse », « baisse » ou « stable » (|delta| < 2 % ou < 0,1).
        """
        s = df[col].dropna() if col in df else pd.Series(dtype=float)
        cur, prev = s.iloc[-hours:], s.iloc[-2 * hours:-hours]
        res: Dict[str, Any] = {"delta": np.nan, "delta_pct": np.nan, "slope_per_day": np.nan,
                               "direction": "stable"}
        if len(cur) >= 2:
            x = (cur.index - cur.index[0]).total_seconds() / 86400
            res["slope_per_day"] = float(np.polyfit(x, cur.values, 1)[0])
        if len(prev) and len(cur):
            delta = float(cur.mean() - prev.mean())
            res["delta"] = delta
            res["delta_pct"] = float(delta / abs(prev.mean()) * 100) if prev.mean() else np.nan
            tol = max(0.02 * abs(prev.mean()), 0.1)
            res["direction"] = "hausse" if delta > tol else "baisse" if delta < -tol else "stable"
        return res

    @staticmethod
    def trends_table(df: pd.DataFrame, cols: Iterable[str]) -> pd.DataFrame:
        """Tableau des tendances 24 h et 7 jours pour plusieurs variables."""
        rows: List[Dict[str, Any]] = []
        arrows = {"hausse": "↗ hausse", "baisse": "↘ baisse", "stable": "→ stable"}
        for c in cols:
            if c not in df:
                continue
            t24, t7 = DataProcessor.trend(df, c, 24), DataProcessor.trend(df, c, 24 * 7)
            # Pente 7 j jugée significative si elle dépasse 5 % de la moyenne par jour.
            tol = 0.05 * max(abs(float(df[c].mean())), 1.0)
            slope = t7["slope_per_day"]
            dir7 = "hausse" if slope > tol else "baisse" if slope < -tol else "stable"
            rows.append({
                "Variable": config.LABELS.get(c, c),
                "Δ 24 h": round(t24["delta"], 2), "Δ 24 h (%)": round(t24["delta_pct"], 1),
                "Tendance 24 h": arrows[t24["direction"]],
                "Pente 7 j (/jour)": round(slope, 2), "Tendance 7 j": arrows[dir7],
            })
        return pd.DataFrame(rows)

    @staticmethod
    def daily_summary(df: pd.DataFrame) -> pd.DataFrame:
        """Synthèse journalière : T° min/max, pluie, moyennes PM2.5/NO2, AQI max, score moyen."""
        spec = {
            "T° min (°C)": ("temperature_2m", "min"), "T° max (°C)": ("temperature_2m", "max"),
            "Pluie (mm)": ("precipitation", "sum"), "PM2.5 moy.": ("pm2_5", "mean"),
            "NO2 moy.": ("nitrogen_dioxide", "mean"), "AQI US max": ("us_aqi", "max"),
            "Confort moy.": ("comfort_score", "mean"),
        }
        spec = {k: v for k, v in spec.items() if v[0] in df}
        if df.empty or not spec:
            return pd.DataFrame()
        d = df.resample("D").agg(**spec)
        d.index = d.index.strftime("%d/%m/%Y")
        return d.round(1)
