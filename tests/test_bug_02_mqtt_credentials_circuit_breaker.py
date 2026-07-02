"""BUG-02 — MQTT credentials circuit-breaker fallback.

Tests hit the real production helpers in
`custom_components.navimow._mqtt_credentials`. Each test is red against
the unpatched code (the module didn't exist there) and green after the
patch. No inline re-implementation of the logic — the helper is
called with mocked API + hass and its return value + side effects are
asserted.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

# --------------------------------------------------------------------- #
# 1. pure data-shape helpers                                            #
# --------------------------------------------------------------------- #


def test_build_credentials_cache_maps_the_four_keys() -> None:
    from custom_components.navimow._mqtt_credentials import build_credentials_cache

    cache = build_credentials_cache(
        {
            "mqttHost": "mqtt-fra.navimow.com",
            "mqttUrl": "wss://mqtt-fra.navimow.com/mqtt/REDACTED-MQTT-USERID",
            "userName": "REDACTED-MQTT-USERID",
            "pwdInfo": "REDACTED-MQTT-PASSWORD",
        }
    )
    assert cache == {
        "cached_mqtt_host": "mqtt-fra.navimow.com",
        "cached_mqtt_url": "wss://mqtt-fra.navimow.com/mqtt/REDACTED-MQTT-USERID",
        "cached_mqtt_username": "REDACTED-MQTT-USERID",
        "cached_mqtt_password": "REDACTED-MQTT-PASSWORD",
    }


def test_build_credentials_cache_null_safe_on_missing_keys() -> None:
    from custom_components.navimow._mqtt_credentials import build_credentials_cache

    cache = build_credentials_cache({"mqttHost": "x"})
    assert cache == {
        "cached_mqtt_host": "x",
        "cached_mqtt_url": None,
        "cached_mqtt_username": None,
        "cached_mqtt_password": None,
    }


def test_mqtt_info_from_cache_all_none_on_first_setup() -> None:
    """First-ever setup that fails outright leaves entry.data without
    any `cached_mqtt_*` keys. The rebuild must not crash and yield an
    all-None mqtt_info so the SDK setup path can fall back to the hard-
    coded defaults from const.py.
    """
    from custom_components.navimow._mqtt_credentials import mqtt_info_from_cache

    rebuilt = mqtt_info_from_cache({"unrelated": "value"})
    assert rebuilt == {
        "mqttHost": None,
        "mqttUrl": None,
        "userName": None,
        "pwdInfo": None,
    }


# --------------------------------------------------------------------- #
# 2. async resolver — success path                                      #
# --------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_resolve_success_writes_cache_and_returns_fresh_info() -> None:
    from custom_components.navimow._mqtt_credentials import resolve_mqtt_info

    fresh = {
        "mqttHost": "mqtt-fra.navimow.com",
        "mqttUrl": "wss://mqtt-fra.navimow.com/mqtt/REDACTED-MQTT-USERID",
        "userName": "REDACTED-MQTT-USERID",
        "pwdInfo": "REDACTED-MQTT-PASSWORD",
    }
    api = MagicMock()
    api.async_get_mqtt_user_info = AsyncMock(return_value=fresh)

    hass = MagicMock()
    hass.config_entries.async_update_entry = MagicMock()

    entry = MagicMock()
    entry.data = {"api_base_url": "https://navimow-fra.ninebot.com"}

    result = await resolve_mqtt_info(api, hass, entry)

    # Contract 1: the fresh dict flows through unchanged.
    assert result == fresh
    # Contract 2: cache is written to entry.data with the four keys.
    hass.config_entries.async_update_entry.assert_called_once()
    _args, kwargs = hass.config_entries.async_update_entry.call_args
    written = kwargs["data"]
    assert written["cached_mqtt_host"] == "mqtt-fra.navimow.com"
    assert written["cached_mqtt_username"] == "REDACTED-MQTT-USERID"
    assert written["cached_mqtt_password"] == "REDACTED-MQTT-PASSWORD"
    # Existing keys are preserved.
    assert written["api_base_url"] == "https://navimow-fra.ninebot.com"


# --------------------------------------------------------------------- #
# 3. async resolver — circuit-breaker fallback path                     #
# --------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_resolve_endpoint_failure_uses_cache_no_raise(caplog) -> None:
    """Core BUG-02 contract: when the endpoint circuit-breaks, the
    resolver must NOT raise ConfigEntryNotReady (which would keep HA in
    a retry loop against the broken endpoint). Instead it returns the
    cached credentials and logs a WARNING once.
    """
    import logging

    from mower_sdk.errors import MowerAPIError

    from custom_components.navimow._mqtt_credentials import resolve_mqtt_info

    api = MagicMock()
    api.async_get_mqtt_user_info = AsyncMock(side_effect=MowerAPIError("504 Gateway"))

    hass = MagicMock()
    hass.config_entries.async_update_entry = MagicMock()

    entry = MagicMock()
    entry.data = {
        "cached_mqtt_host": "mqtt-fra.navimow.com",
        "cached_mqtt_url": "wss://mqtt-fra.navimow.com/mqtt/REDACTED-MQTT-USERID",
        "cached_mqtt_username": "REDACTED-MQTT-USERID",
        "cached_mqtt_password": "REDACTED-MQTT-PASSWORD",
    }

    caplog.set_level(
        logging.WARNING, logger="custom_components.navimow._mqtt_credentials"
    )

    result = await resolve_mqtt_info(api, hass, entry)

    # No cache write — the endpoint failed.
    hass.config_entries.async_update_entry.assert_not_called()
    # Cached credentials returned in mqtt_info shape.
    assert result == {
        "mqttHost": "mqtt-fra.navimow.com",
        "mqttUrl": "wss://mqtt-fra.navimow.com/mqtt/REDACTED-MQTT-USERID",
        "userName": "REDACTED-MQTT-USERID",
        "pwdInfo": "REDACTED-MQTT-PASSWORD",
    }
    # Warning logged exactly once.
    warnings = [
        r
        for r in caplog.records
        if r.levelno == logging.WARNING and "Failed to get MQTT user info" in r.message
    ]
    assert len(warnings) == 1


@pytest.mark.asyncio
async def test_resolve_endpoint_failure_first_ever_setup_yields_null() -> None:
    """No cache yet (first-ever setup during an outage): the resolver
    returns an all-None mqtt_info shape so the SDK setup path falls
    back to const.py defaults instead of raising.
    """
    from mower_sdk.errors import MowerAPIError

    from custom_components.navimow._mqtt_credentials import resolve_mqtt_info

    api = MagicMock()
    api.async_get_mqtt_user_info = AsyncMock(side_effect=MowerAPIError("504"))
    hass = MagicMock()
    entry = MagicMock()
    entry.data = {}

    result = await resolve_mqtt_info(api, hass, entry)

    assert result == {
        "mqttHost": None,
        "mqttUrl": None,
        "userName": None,
        "pwdInfo": None,
    }
