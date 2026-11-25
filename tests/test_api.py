"""Tests du client API et du cache (réseau entièrement simulé avec unittest.mock)."""
import datetime as dt
import json
import sqlite3
from unittest.mock import MagicMock, patch

import pytest
import requests

from src.api_client import APIError, CacheManager, WeatherAPIClient, generate_mock_data
from src import config


@pytest.fixture
def cache(tmp_path):
    return CacheManager(tmp_path / "cache.db", ttl_minutes=30)


def _response(payload=None, status=200, json_error=False):
    resp = MagicMock()
    resp.status_code = status
    if status >= 400:
        err = requests.HTTPError(f"{status}")
        err.response = resp
        resp.raise_for_status.side_effect = err
    if json_error:
        resp.json.side_effect = json.JSONDecodeError("bad", "x", 0)
    else:
        resp.json.return_value = payload if payload is not None else {}
    return resp


def _api_payload(kind):
    mock = generate_mock_data(48.85, 2.35)
    return mock["weather"] if kind == "weather" else mock["air_quality"]


def test_cache_set_and_get(cache):
    cache.set("k", {"a": 1})
    assert cache.get("k") == {"a": 1}
    assert cache.get("absent") is None


def test_cache_expires_after_ttl(cache):
    cache.set("k", {"a": 1})
    old = (dt.datetime.now() - dt.timedelta(minutes=31)).isoformat()
    with sqlite3.connect(cache.db_path) as conn:
        conn.execute("UPDATE api_cache SET timestamp = ? WHERE key = ?", (old, "k"))
    assert cache.get("k") is None


def test_fetch_uses_api_then_cache(cache):
    client = WeatherAPIClient(cache)
    client.session.get = MagicMock(side_effect=[_response(_api_payload("weather")),
                                                _response(_api_payload("air"))])
    data = client.fetch(48.85, 2.35)
    assert client.last_source == "api"
    assert "hourly" in data["weather"] and "hourly" in data["air_quality"]
    assert client.session.get.call_count == 2
    client.fetch(48.85, 2.35)
    assert client.last_source == "cache"
    assert client.session.get.call_count == 2  # pas de nouvel appel réseau


def test_network_error_falls_back_to_mock(cache):
    client = WeatherAPIClient(cache)
    client.session.get = MagicMock(side_effect=requests.ConnectionError("pas de réseau"))
    data = client.fetch(48.85, 2.35)
    assert client.last_source == "mock"
    assert data["mock"] is True
    assert "réseau" in client.last_error


def test_http_500_raises_api_error(cache):
    client = WeatherAPIClient(cache)
    client.session.get = MagicMock(return_value=_response(status=500))
    with pytest.raises(APIError, match="HTTP 500"):
        client._get_json(config.WEATHER_URL, {})


def test_corrupted_json_raises_api_error(cache):
    client = WeatherAPIClient(cache)
    client.session.get = MagicMock(return_value=_response(json_error=True))
    with pytest.raises(APIError, match="JSON"):
        client._get_json(config.WEATHER_URL, {})


def test_api_error_field_raises_api_error(cache):
    client = WeatherAPIClient(cache)
    client.session.get = MagicMock(return_value=_response({"error": True, "reason": "Latitude invalide"}))
    with pytest.raises(APIError, match="Latitude invalide"):
        client._get_json(config.WEATHER_URL, {})


def test_corrupted_cache_entry_is_ignored(cache):
    with sqlite3.connect(cache.db_path) as conn:
        conn.execute("INSERT INTO api_cache VALUES (?, ?, ?)", ("k", "{pas du json", dt.datetime.now().isoformat()))
    assert cache.get("k") is None


def test_mock_data_has_api_format():
    data = generate_mock_data(48.85, 2.35, past_days=7, forecast_days=2)
    w, a = data["weather"]["hourly"], data["air_quality"]["hourly"]
    assert len(w["time"]) == len(a["time"]) == 24 * 9
    for var in config.WEATHER_VARS:
        assert len(w[var]) == 24 * 9
    for var in config.AIR_VARS:
        assert len(a[var]) == 24 * 9
    assert data == generate_mock_data(48.85, 2.35)  # déterministe (seed 42)
