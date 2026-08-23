"""SDK cache re-application in the coordinator poll path — battery now
sourced from MQTT (FEAT-11).

Each coordinator tick re-applies `sdk.get_cached_state()` as
`_last_state`. BUG-04/BUG-08 preserved the previously held battery here,
so only HTTP could write it. OS V4.3.0 makes the `/state` battery
reliable (see `docs/diag/2026-08-23_feat-11_v43-battery-mowing/`), and
the preservation was pinning HA to a stale value, so FEAT-11 reverts it:
the cache is applied as-is, battery included.

These tests lock the reverted cache-path behaviour: the SDK cache
battery is accepted, cold start lands the cache verbatim, and a missing
cache leaves `_last_state` untouched.
"""

from __future__ import annotations

import time
from unittest.mock import AsyncMock, MagicMock

import pytest
from mower_sdk.models import DeviceStateMessage


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
    # FEAT-05 (b): run tracker (idle, emits nothing until fed).
    from custom_components.navimow.run_tracker import RunTracker

    coordinator.run_tracker = RunTracker()

    # FEAT-05 (c) persistence + history attributes.

    coordinator.history = []

    coordinator.last_finished_run = None

    coordinator._store = None

    coordinator._last_store_save_monotonic = 0.0
    return coordinator


def _state(
    *, battery: int | None, state: str = "isRunning", ts: int = 1_000_000_000_000
):
    return DeviceStateMessage(
        device_id="REDACTED-ROBOT-SERIAL",
        timestamp=ts,
        state=state,
        battery=battery,
    )


@pytest.mark.asyncio
async def test_cache_battery_is_accepted() -> None:
    """The cache re-application writes the SDK cache's battery: the mowing
    `/state` battery (94) replaces the stale held value (100), and the
    non-battery fields come from the cache too.
    """
    mqtt_cache = _state(battery=94, state="isRunning")

    coordinator = _make_coordinator(cached_state=mqtt_cache)
    coordinator._last_state = _state(battery=100, state="isDocked")

    await coordinator._async_update_data()

    assert coordinator._last_state.battery == 94
    assert coordinator._last_state.state == "isRunning"
    assert coordinator._last_data_source == "mqtt_cache"


@pytest.mark.asyncio
async def test_first_boot_no_prior_state_accepts_cache_verbatim() -> None:
    """Cold start: `_last_state is None`. The SDK cache lands unchanged."""
    mqtt_cache = _state(battery=42, state="isDocked")

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
    http_state = _state(battery=77, state="isRunning")
    coordinator = _make_coordinator(cached_state=None)
    coordinator._last_state = http_state

    await coordinator._async_update_data()

    assert coordinator._last_state is http_state
    assert coordinator._last_state.battery == 77
