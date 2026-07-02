"""BUG-01 — MQTT staleness thresholds and HTTP fallback push.

Regression tests for the three tunings in const.py and the
`async_set_updated_data` call added to `_async_update_data` after a
successful HTTP fallback fetch.

We assert observable values on the module (constants exist and are within
the ranges that keep HA responsive) and the observable behaviour of the
coordinator on a mock SDK/API — never inspect source text.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest


def test_mqtt_stale_seconds_short_enough() -> None:
    """Silent MQTT outage must surface within ~90 s.

    Upstream default was 300 (~5 min). BUG-01 lowers to 90.
    """
    from custom_components.navimow.const import MQTT_STALE_SECONDS

    assert MQTT_STALE_SECONDS <= 120, (
        f"MQTT_STALE_SECONDS={MQTT_STALE_SECONDS} is too large — "
        "silent outages will linger in HA. See BUG-01."
    )


def test_http_fallback_min_interval_short_enough() -> None:
    """HTTP fallback throttle must not keep the entity stale for >2 min.

    Upstream default was 3600 (1 h). BUG-01 lowers to 60.
    """
    from custom_components.navimow.const import HTTP_FALLBACK_MIN_INTERVAL

    assert HTTP_FALLBACK_MIN_INTERVAL <= 120, (
        f"HTTP_FALLBACK_MIN_INTERVAL={HTTP_FALLBACK_MIN_INTERVAL} is too large — "
        "a stuck entity would take up to that many seconds to recover. See BUG-01."
    )


def test_mqtt_keepalive_seconds_present_and_short() -> None:
    """MQTT_KEEPALIVE_SECONDS did not exist in upstream v1.1.0 — the SDK
    was constructed with a hard-coded 2400. BUG-01 introduces the constant
    and lowers it to a value that catches half-open connections quickly.
    """
    from custom_components.navimow import const

    assert hasattr(
        const, "MQTT_KEEPALIVE_SECONDS"
    ), "MQTT_KEEPALIVE_SECONDS constant is missing — see BUG-01."
    assert const.MQTT_KEEPALIVE_SECONDS <= 180, (
        f"MQTT_KEEPALIVE_SECONDS={const.MQTT_KEEPALIVE_SECONDS} is too large "
        "to detect half-open connections faster than the cloud's own drop."
    )


@pytest.mark.asyncio
async def test_http_fallback_pushes_state_to_entities_immediately() -> None:
    """After a successful HTTP fallback, the coordinator must call
    `async_set_updated_data()` so entities update within the tick.
    Otherwise the fresh state waits for the next 30-s coordinator cycle.

    Regression test for the observable behaviour added by BUG-01.
    """
    from custom_components.navimow.coordinator import NavimowCoordinator

    coordinator = NavimowCoordinator.__new__(NavimowCoordinator)
    coordinator.hass = MagicMock()
    coordinator.logger = MagicMock()
    coordinator.name = "test"
    coordinator.update_interval = None
    coordinator.config_entry = MagicMock()

    device = MagicMock()
    device.id = "REDACTED-ROBOT-SERIAL"
    coordinator.device = device

    sdk = MagicMock()
    sdk.get_cached_state.return_value = None
    sdk.get_cached_attributes.return_value = None
    sdk.is_connected = False
    coordinator.sdk = sdk

    status = MagicMock()
    status.battery = 77
    api = MagicMock()
    api.async_get_device_status = AsyncMock(return_value=status)
    coordinator.api = api

    coordinator._last_state = None
    coordinator._last_attributes = None
    coordinator._last_mqtt_update = None
    coordinator._last_http_fetch = None
    coordinator._last_data_source = None
    coordinator.oauth_session = None  # bypass token refresh path

    coordinator._device_status_to_state = MagicMock(return_value=MagicMock(battery=77))
    coordinator._build_data = MagicMock(return_value={"state": "http_fallback_result"})
    coordinator.async_set_updated_data = MagicMock()

    await coordinator._async_update_data()

    coordinator.async_set_updated_data.assert_called_once()
    (payload,), _kwargs = coordinator.async_set_updated_data.call_args
    assert payload == {"state": "http_fallback_result"}
    assert coordinator._last_data_source == "http_fallback"


@pytest.mark.asyncio
async def test_http_fallback_not_called_when_state_is_fresh() -> None:
    """Guard against the regression where the coordinator would push on
    every tick regardless of staleness — that would spam the API and
    entity listeners.
    """
    import time

    from custom_components.navimow.coordinator import NavimowCoordinator

    coordinator = NavimowCoordinator.__new__(NavimowCoordinator)
    coordinator.hass = MagicMock()
    coordinator.logger = MagicMock()
    coordinator.name = "test"
    coordinator.update_interval = None
    coordinator.config_entry = MagicMock()

    device = MagicMock()
    device.id = "REDACTED-ROBOT-SERIAL"
    coordinator.device = device

    sdk = MagicMock()
    sdk.get_cached_state.return_value = None
    sdk.get_cached_attributes.return_value = None
    sdk.is_connected = True
    coordinator.sdk = sdk

    api = MagicMock()
    api.async_get_device_status = AsyncMock()
    coordinator.api = api

    coordinator._last_state = MagicMock()
    coordinator._last_attributes = None
    # MQTT arrived 5 seconds ago — fresh.
    coordinator._last_mqtt_update = time.monotonic() - 5
    coordinator._last_http_fetch = None
    coordinator._last_data_source = "mqtt_push"
    coordinator.oauth_session = None

    coordinator._build_data = MagicMock(return_value={})
    coordinator.async_set_updated_data = MagicMock()

    await coordinator._async_update_data()

    api.async_get_device_status.assert_not_called()
    coordinator.async_set_updated_data.assert_not_called()
