"""Configuration commune des tests : chemins, fichiers temporaires et jeux de données."""
import os
import sys
import tempfile
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

# Avant tout import de src : les tests ne doivent jamais écrire dans les vrais fichiers de data/.
_TMP = Path(tempfile.mkdtemp(prefix="dashboard_tests_"))
os.environ["SECURITY_LOG"] = str(_TMP / "security.log")
os.environ["USERS_FILE"] = str(_TMP / "users.json")
os.environ["CACHE_DB"] = str(_TMP / "cache.db")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


@pytest.fixture
def hourly_df() -> pd.DataFrame:
    """9 jours de données horaires, température en hausse linéaire, pollution modérée."""
    idx = pd.date_range("2026-01-01", periods=24 * 9, freq="h")
    n = len(idx)
    return pd.DataFrame({
        "temperature_2m": np.linspace(5, 23, n),
        "apparent_temperature": np.linspace(3, 21, n),
        "relative_humidity_2m": np.full(n, 60.0),
        "pm2_5": np.full(n, 8.0),
        "pm10": np.full(n, 15.0),
        "nitrogen_dioxide": np.full(n, 12.0),
        "ozone": np.full(n, 40.0),
        "sulphur_dioxide": np.full(n, 2.0),
        "carbon_monoxide": np.full(n, 200.0),
        "us_aqi": np.full(n, 35.0),
    }, index=idx)
