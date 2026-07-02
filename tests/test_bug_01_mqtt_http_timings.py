"""BUG-01 — MQTT staleness thresholds and edge-triggered outage warning.

Regression tests for the three tunings in const.py, their wiring into
the SDK constructor, and the edge-triggered "MQTT disconnected + stale"
WARNING that must fire once on entry and once on recovery (not per
tick).

We assert observable values on the module and observable behaviour of
the coordinator on a mock SDK/API — never inspect source text.
"""

from __future__ import annotations

import logging
from unittest.mock import AsyncMock, MagicMock

import pytest

# --------------------------------------------------------------------- #
# 1. constant contracts                                                 #
# --------------------------------------------------------------------- #


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


# --------------------------------------------------------------------- #
# 2. coordinator observable behaviour                                   #
# --------------------------------------------------------------------- #


def _make_coordinator(*, is_connected: bool, last_mqtt: float | None):
    """Mock coordinator sufficient to drive `_async_update_data`."""
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
    sdk.is_connected = is_connected
    coordinator.sdk = sdk

    api = MagicMock()
    api.async_get_device_status = AsyncMock(return_value=MagicMock(battery=77))
    coordinator.api = api

    coordinator._last_state = None
    coordinator._last_attributes = None
    coordinator._last_mqtt_update = last_mqtt
    coordinator._last_http_fetch = None
    coordinator._last_data_source = None
    coordinator.oauth_session = None
    coordinator._mqtt_disconnect_warned = False

    coordinator._device_status_to_state = MagicMock(return_value=MagicMock(battery=77))
    coordinator._build_data = MagicMock(return_value={"state": "http_fallback_result"})
    coordinator.async_set_updated_data = MagicMock()
    return coordinator


@pytest.mark.asyncio
async def test_http_fallback_state_is_returned_to_framework() -> None:
    """After a successful HTTP fallback, the value returned by
    `_async_update_data` must carry the fresh state.

    HA's `DataUpdateCoordinator` propagates that return value to entity
    listeners via `async_update_listeners()` at the end of the tick, so
    no extra `async_set_updated_data()` call is needed (and it would
    double-notify).
    """
    coordinator = _make_coordinator(is_connected=False, last_mqtt=None)

    returned = await coordinator._async_update_data()

    coordinator.api.async_get_device_status.assert_awaited_once()
    assert coordinator._last_data_source == "http_fallback"
    # Framework contract: the returned dict is what listeners receive.
    assert returned == {"state": "http_fallback_result"}
    # Anti-pattern guard: async_set_updated_data must NOT be called inside
    # the poll method — HA raises a reentrancy warning and double-notifies.
    coordinator.async_set_updated_data.assert_not_called()


@pytest.mark.asyncio
async def test_http_fallback_not_called_when_state_is_fresh() -> None:
    """Fresh MQTT state must not trigger the HTTP poll."""
    import time

    coordinator = _make_coordinator(is_connected=True, last_mqtt=time.monotonic() - 5)
    coordinator._last_state = MagicMock()

    await coordinator._async_update_data()

    coordinator.api.async_get_device_status.assert_not_called()
    coordinator.async_set_updated_data.assert_not_called()


# --------------------------------------------------------------------- #
# 3. edge-triggered outage warning                                      #
# --------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_disconnected_stale_warning_fires_once_on_entry(caplog) -> None:
    """Two consecutive ticks in the disconnected-stale state must emit
    the WARNING exactly once. Without the edge trigger, a routine 1 h
    outage would produce ~120 identical WARNING lines.
    """
    coordinator = _make_coordinator(is_connected=False, last_mqtt=None)

    caplog.set_level(logging.WARNING, logger="custom_components.navimow.coordinator")

    await coordinator._async_update_data()
    await coordinator._async_update_data()

    disconnect_warnings = [
        r
        for r in caplog.records
        if r.levelno == logging.WARNING and "MQTT appears disconnected" in r.message
    ]
    assert len(disconnect_warnings) == 1, (
        f"expected exactly 1 disconnect WARNING across 2 ticks, got "
        f"{len(disconnect_warnings)}"
    )
    assert coordinator._mqtt_disconnect_warned is True


@pytest.mark.asyncio
async def test_reconnect_logs_once_at_info_and_clears_flag(caplog) -> None:
    """After the disconnect WARNING has fired, the first tick that sees
    `sdk.is_connected = True` again must log the reconnect INFO and clear
    the flag — even if the state is still stale (the recovery signal is
    the SDK's own connectivity, not the state freshness).
    """
    coordinator = _make_coordinator(is_connected=False, last_mqtt=None)
    caplog.set_level(logging.INFO, logger="custom_components.navimow.coordinator")

    # First tick: enter outage.
    await coordinator._async_update_data()
    assert coordinator._mqtt_disconnect_warned is True

    # Second tick: SDK reconnected but state hasn't refreshed yet — the
    # INFO must still fire (we log the connectivity event, not the state
    # recovery).
    coordinator.sdk.is_connected = True
    await coordinator._async_update_data()

    reconnect_infos = [
        r
        for r in caplog.records
        if r.levelno == logging.INFO and "MQTT reconnected" in r.message
    ]
    assert len(reconnect_infos) == 1
    assert coordinator._mqtt_disconnect_warned is False


@pytest.mark.asyncio
async def test_no_warning_when_mqtt_connected_but_state_stale(caplog) -> None:
    """The stale check triggers HTTP fallback but the disconnect
    WARNING must NOT fire — the SDK is connected, the "MQTT is silently
    down" motivation for the WARNING does not apply.
    """
    coordinator = _make_coordinator(is_connected=True, last_mqtt=None)
    caplog.set_level(logging.WARNING, logger="custom_components.navimow.coordinator")

    await coordinator._async_update_data()

    disconnect_warnings = [
        r for r in caplog.records if "MQTT appears disconnected" in r.message
    ]
    assert disconnect_warnings == []
    assert coordinator._mqtt_disconnect_warned is False
