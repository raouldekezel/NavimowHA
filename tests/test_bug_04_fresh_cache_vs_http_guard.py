"""BUG-04 — SDK cache re-application in the coordinator poll path.

The SDK's `get_cached_state()` returns the last MQTT push forever, even
after that push became stale (e.g. `battery=0` after over-discharge, or
`battery=100` while the robot is actively discharging on the lawn). Each
coordinator tick re-applies that cache as `_last_state`.

The original BUG-04 fix skipped the re-application altogether when HTTP
had been fetched more recently than the last MQTT state update. That
guard has been retired as of BUG-08 (#45): the coordinator now applies
the cache unconditionally but preserves `_last_state.battery` from the
previous holder, since HTTP is the sole source of truth for battery.
Non-battery fields (state, error, position, timestamp,
signal_strength) still come from the cache — the 2026-07-03 trace
established they stay coherent with reality.

These tests lock in the surviving BUG-04 protection: HTTP-truth battery
must NOT be clobbered by the SDK cache re-application, whatever the
relative timestamps happen to be.
"""

from __future__ import annotations

import time
from unittest.mock import AsyncMock, MagicMock

import pytest


def _make_coordinator(*, cached_state):
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
    sdk.get_cached_state.return_value = cached_state
    sdk.get_cached_attributes.return_value = None
    sdk.is_connected = True
    coordinator.sdk = sdk

    api = MagicMock()
    api.async_get_device_status = AsyncMock()
    coordinator.api = api

    coordinator._last_state = None
    coordinator._last_attributes = None
    coordinator._last_mqtt_update = None
    # Pretend MQTT pushed a state 5 s ago so `is_state_stale` is False and
    # the HTTP fallback branch stays out of the way of the cache-path test.
    coordinator._last_mqtt_state_update = time.monotonic() - 5
    coordinator._last_http_fetch = None
    coordinator._last_data_source = None
    coordinator.oauth_session = None
    coordinator._mqtt_disconnect_warned = False
    coordinator._mqtt_disconnect_ticks = 0

    coordinator._device_status_to_state = MagicMock()
    coordinator._build_data = MagicMock(return_value={})
    coordinator.async_set_updated_data = MagicMock()
    return coordinator


@pytest.mark.asyncio
async def test_http_battery_survives_stale_cache_reapplication() -> None:
    """The canonical BUG-04 scenario, restated for the BUG-08 invariant:
    SDK cache carries `battery=0` (stale MQTT payload from over-discharge),
    `_last_state` currently holds the HTTP truth (`battery=87`). Ticking
    the coordinator must NOT overwrite the battery — non-battery fields
    are free to be replaced.
    """
    mqtt_cache = MagicMock()
    mqtt_cache.battery = 0
    mqtt_cache.state = "isDocked"
    mqtt_cache.timestamp = 1_000_000_000_000

    coordinator = _make_coordinator(cached_state=mqtt_cache)
    http_state = MagicMock()
    http_state.battery = 87
    http_state.state = "isRunning"
    coordinator._last_state = http_state

    await coordinator._async_update_data()

    assert coordinator._last_state.battery == 87


@pytest.mark.asyncio
async def test_cache_reapplication_still_updates_state_field() -> None:
    """Non-battery fields must still be picked up from the SDK cache —
    the trace shows `state` stays coherent, and BUG-04's guard used to
    block this legitimate refresh whenever HTTP happened to be newer.
    """
    mqtt_cache = MagicMock()
    mqtt_cache.battery = 55  # would be dropped anyway by the preserve
    mqtt_cache.state = "isRunning"

    coordinator = _make_coordinator(cached_state=mqtt_cache)
    prev_state = MagicMock()
    prev_state.battery = 90
    prev_state.state = "isDocked"
    coordinator._last_state = prev_state

    await coordinator._async_update_data()

    # Cache's non-battery fields propagated; battery preserved from HTTP.
    assert coordinator._last_state.state == "isRunning"
    assert coordinator._last_state.battery == 90
    assert coordinator._last_data_source == "mqtt_cache"


@pytest.mark.asyncio
async def test_first_boot_no_prior_state_accepts_cache_verbatim() -> None:
    """Cold start: `_last_state is None`, no prior battery to preserve.
    The SDK cache lands unchanged (previously handled by falling through
    the guard's `http_is_newer=False` branch).
    """
    mqtt_cache = MagicMock()
    mqtt_cache.battery = 42
    mqtt_cache.state = "isDocked"

    coordinator = _make_coordinator(cached_state=mqtt_cache)

    await coordinator._async_update_data()

    assert coordinator._last_state is mqtt_cache
    assert coordinator._last_state.battery == 42
    assert coordinator._last_data_source == "mqtt_cache"


@pytest.mark.asyncio
async def test_no_cache_yet_leaves_state_untouched() -> None:
    """SDK has nothing cached (no MQTT push ever): the poll path does
    not touch `_last_state`; it stays at whatever HTTP left it at.
    """
    coordinator = _make_coordinator(cached_state=None)
    http_state = MagicMock()
    http_state.battery = 77
    http_state.state = "isRunning"
    coordinator._last_state = http_state

    await coordinator._async_update_data()

    assert coordinator._last_state is http_state
    assert coordinator._last_state.battery == 77
