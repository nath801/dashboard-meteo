"""Tests du traitement des données, du score de confort et du moteur d'alertes."""
import numpy as np
import pandas as pd
import pytest

from src.alerts import AlertEngine, aqi_category
from src.api_client import generate_mock_data
from src.data_processing import DataProcessor


def test_to_dataframe_merges_weather_and_air():
    df = DataProcessor.to_dataframe(generate_mock_data(48.85, 2.35))
    assert isinstance(df.index, pd.DatetimeIndex)
    assert df.index.is_monotonic_increasing and df.index.is_unique
    assert {"temperature_2m", "pm2_5", "us_aqi", "comfort_score"} <= set(df.columns)


def test_to_dataframe_handles_empty_input():
    assert DataProcessor.to_dataframe({}).empty


def test_interpolation_and_negative_values():
    times = [f"2026-01-01T{h:02d}:00" for h in range(6)]
    raw = {
        "weather": {"hourly": {"time": times, "temperature_2m": [10, None, None, 13, 14, 15],
                               "relative_humidity_2m": [50] * 6}},
        "air_quality": {"hourly": {"time": times, "pm2_5": [5, -3, 7, 8, 9, 10]}},
    }
    df = DataProcessor.to_dataframe(raw)
    assert df["temperature_2m"].tolist()[:4] == pytest.approx([10, 11, 12, 13])
    assert df["pm2_5"].iloc[1] == pytest.approx(6)  # -3 → NaN → interpolé
    assert (df["pm2_5"] >= 0).all()


def test_comfort_score_ideal_and_bounds():
    df = pd.DataFrame({"apparent_temperature": [21.0, 45.0], "relative_humidity_2m": [50.0, 95.0],
                       "pm2_5": [0.0, 80.0], "nitrogen_dioxide": [0.0, 100.0]})
    score = DataProcessor.comfort_score(df)
    assert score.iloc[0] == 100
    assert score.iloc[1] == 0
    df2 = pd.DataFrame({"apparent_temperature": [23.0], "relative_humidity_2m": [60.0],
                        "pm2_5": [15.0], "nitrogen_dioxide": [25.0]})
    assert DataProcessor.comfort_score(df2).iloc[0] == pytest.approx(100 - (4 + 4 + 15 + 10))


def test_stats_mean_and_median(hourly_df):
    df = hourly_df.copy()
    df.loc[df.index[-4:], "pm2_5"] = [1, 2, 3, 10]
    s = DataProcessor.stats(df, ["pm2_5"], last_hours=4)
    assert s.loc["PM2.5 (µg/m³)", "Moyenne"] == 4
    assert s.loc["PM2.5 (µg/m³)", "Médiane"] == 2.5
    assert s.loc["PM2.5 (µg/m³)", "Max"] == 10


def test_trend_rising(hourly_df):
    t = DataProcessor.trend(hourly_df, "temperature_2m", 24)
    assert t["direction"] == "hausse"
    assert t["delta"] > 0
    assert t["slope_per_day"] == pytest.approx(2.0, rel=0.02)  # 18 °C sur 9 jours


def test_trend_stable(hourly_df):
    assert DataProcessor.trend(hourly_df, "pm2_5", 24)["direction"] == "stable"


def test_latest_valid_skips_forecast_nan():
    df = pd.DataFrame({"us_aqi": [40.0, 42.0, np.nan]},
                      index=pd.date_range("2026-01-01", periods=3, freq="h"))
    assert DataProcessor.latest_valid(df)["us_aqi"] == 42


def test_no_alert_when_air_is_clean(hourly_df):
    assert AlertEngine.evaluate(hourly_df) == []


def test_no2_alert(hourly_df):
    df = hourly_df.copy()
    df["nitrogen_dioxide"] = 60.0
    alerts = AlertEngine.evaluate(df)
    assert len(alerts) == 1
    assert alerts[0].level == "danger"
    assert alerts[0].title == "Alerte Pic de Pollution NO2"


def test_pm25_vigilance(hourly_df):
    df = hourly_df.copy()
    df["pm2_5"] = 18.0
    alerts = AlertEngine.evaluate(df)
    assert [(a.level, a.title) for a in alerts] == [("warning", "Vigilance PM2.5")]


def test_heatwave_alert_first(hourly_df):
    df = hourly_df.copy()
    df["apparent_temperature"] = 39.0
    df["pm2_5"] = 18.0
    alerts = AlertEngine.evaluate(df)
    assert alerts[0].title == "Alerte Canicule"
    assert "seuil 38" in alerts[0].message


@pytest.mark.parametrize("aqi, label", [(10, "Bon"), (75, "Modéré"), (120, "Mauvais (sensibles)"),
                                        (180, "Mauvais"), (250, "Très mauvais"), (400, "Dangereux"),
                                        (None, "Inconnu")])
def test_aqi_category(aqi, label):
    assert aqi_category(aqi)[0] == label
