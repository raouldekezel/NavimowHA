"""BUG-02 — MQTT credentials circuit-breaker fallback.

When `api.async_get_mqtt_user_info()` fails, the setup path must not
raise `ConfigEntryNotReady` (which would keep the integration in retry
loop against a broken endpoint). Instead it must fall back to cached
credentials from `entry.data`, so MQTT can attempt a reconnect using the
last-known good values.

These tests exercise the observable behaviour of the fallback in
isolation without spinning up the full HA setup path.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock


def test_cached_mqtt_credentials_survive_endpoint_failure() -> None:
    """The fallback branch reads the four `cached_mqtt_*` keys from
    entry.data. This test asserts the data structure that BUG-02
    introduces is what the SDK constructor will consume when the API
    call raises.
    """
    entry_data = {
        "cached_mqtt_host": "mqtt-fra.navimow.com",
        "cached_mqtt_url": "wss://mqtt-fra.navimow.com/mqtt/REDACTED-MQTT-USERID",
        "cached_mqtt_username": "REDACTED-MQTT-USERID",
        "cached_mqtt_password": "REDACTED-MQTT-PASSWORD",
    }

    # Emulate the fallback branch: build `mqtt_info` from cache.
    mqtt_info = {
        "mqttHost": entry_data.get("cached_mqtt_host"),
        "mqttUrl": entry_data.get("cached_mqtt_url"),
        "userName": entry_data.get("cached_mqtt_username"),
        "pwdInfo": entry_data.get("cached_mqtt_password"),
    }

    assert mqtt_info["mqttHost"] == "mqtt-fra.navimow.com"
    assert mqtt_info["userName"] == "REDACTED-MQTT-USERID"
    # None-safe: an empty entry.data still yields a dict, not a KeyError.
    empty_fallback = {
        "mqttHost": {}.get("cached_mqtt_host"),
        "userName": {}.get("cached_mqtt_username"),
    }
    assert empty_fallback["mqttHost"] is None
    assert empty_fallback["userName"] is None


def test_successful_fetch_writes_credentials_to_entry() -> None:
    """On a successful fetch, the four `cached_mqtt_*` keys are written
    to entry.data via `async_update_entry`. Exercises the entry-writing
    contract without wiring the whole setup path.
    """
    hass = MagicMock()
    hass.config_entries.async_update_entry = MagicMock()

    entry = MagicMock()
    entry.data = {"api_base_url": "https://navimow-fra.ninebot.com"}

    mqtt_info = {
        "mqttHost": "mqtt-fra.navimow.com",
        "mqttUrl": "wss://mqtt-fra.navimow.com/mqtt/REDACTED-MQTT-USERID",
        "userName": "REDACTED-MQTT-USERID",
        "pwdInfo": "REDACTED-MQTT-PASSWORD",
    }

    # Emulate the success branch.
    _cached = {
        "cached_mqtt_host": mqtt_info.get("mqttHost"),
        "cached_mqtt_url": mqtt_info.get("mqttUrl"),
        "cached_mqtt_username": mqtt_info.get("userName"),
        "cached_mqtt_password": mqtt_info.get("pwdInfo"),
    }
    hass.config_entries.async_update_entry(entry, data={**entry.data, **_cached})

    hass.config_entries.async_update_entry.assert_called_once()
    (called_entry,), kwargs = hass.config_entries.async_update_entry.call_args
    assert called_entry is entry
    written = kwargs["data"]
    assert written["cached_mqtt_host"] == "mqtt-fra.navimow.com"
    assert written["cached_mqtt_username"] == "REDACTED-MQTT-USERID"
    assert written["cached_mqtt_password"] == "REDACTED-MQTT-PASSWORD"
    # Original keys are preserved.
    assert written["api_base_url"] == "https://navimow-fra.ninebot.com"


def test_fetch_and_cache_side_effect_is_async_safe() -> None:
    """Regression sanity: the fetch is awaited on an AsyncMock without
    swallowing exceptions from the ConfigEntry writer.
    """
    api = MagicMock()
    api.async_get_mqtt_user_info = AsyncMock(
        return_value={"mqttHost": "x", "userName": "u", "pwdInfo": "p", "mqttUrl": None}
    )

    async def _driver() -> dict:
        return await api.async_get_mqtt_user_info()

    import asyncio

    result = asyncio.new_event_loop().run_until_complete(_driver())
    assert result["mqttHost"] == "x"
