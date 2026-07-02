"""BUG-04 — freshness guard between SDK's MQTT cache and HTTP fallback.

The SDK's `get_cached_state()` returns the last MQTT push forever, even
after that push became stale (e.g. battery=0 after over-discharge, or
battery=100 while the robot is actively discharging on the lawn).
Before this patch, every coordinator tick unconditionally re-applied
that cached state as `_last_state`, clobbering any fresher HTTP status
that had been fetched via the fallback path. Result: battery reading
flickered between the HTTP truth and the MQTT lie every ~30 s.

Fix: when the last HTTP fetch is newer than the last observed MQTT
state push, do NOT re-apply the SDK cache.

Original patch (issue segwaynavimow/NavimowHA#11 by @stefan73 described
the diagnosis in prose; this implementation is ours).
"""

from __future__ import annotations

import time
from unittest.mock import AsyncMock, MagicMock

import pytest


def _make_coordinator(*, cached_state, http_fetch_at, mqtt_state_at):
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

    coordinator._last_state = MagicMock()
    coordinator._last_state.battery = 999  # sentinel — should be overwritten
    coordinator._last_attributes = None
    coordinator._last_mqtt_update = None
    coordinator._last_mqtt_state_update = mqtt_state_at
    coordinator._last_http_fetch = http_fetch_at
    coordinator._last_data_source = None
    coordinator.oauth_session = None

    coordinator._device_status_to_state = MagicMock()
    coordinator._build_data = MagicMock(return_value={})
    coordinator.async_set_updated_data = MagicMock()
    return coordinator


@pytest.mark.asyncio
async def test_fresh_http_beats_stale_mqtt_cache() -> None:
    """The canonical BUG-04 scenario:
    - SDK cache carries battery=0 (stale MQTT payload from over-discharge)
    - Last HTTP fetch was 10 s ago and returned battery=87
    - Coordinator ticks; must NOT re-apply the MQTT cache.
    """
    now = time.monotonic()
    http_state = MagicMock()
    http_state.battery = 87
    mqtt_cache = MagicMock()
    mqtt_cache.battery = 0

    coordinator = _make_coordinator(
        cached_state=mqtt_cache,
        http_fetch_at=now - 10,  # HTTP fresh
        mqtt_state_at=now - 600,  # MQTT state 10 min old
    )
    # Pre-seed _last_state with the HTTP result (as if the previous tick
    # ran the HTTP fallback path).
    coordinator._last_state = http_state

    await coordinator._async_update_data()

    assert coordinator._last_state is http_state
    assert coordinator._last_state.battery == 87
    assert coordinator._last_data_source != "mqtt_cache"


@pytest.mark.asyncio
async def test_fresh_mqtt_beats_older_http() -> None:
    """Symmetric: MQTT push is newer than the last HTTP → the cache
    IS applied (normal happy path).
    """
    now = time.monotonic()
    mqtt_cache = MagicMock()
    mqtt_cache.battery = 55

    coordinator = _make_coordinator(
        cached_state=mqtt_cache,
        http_fetch_at=now - 300,  # HTTP 5 min old
        mqtt_state_at=now - 20,  # MQTT state 20 s old — newer
    )

    await coordinator._async_update_data()

    assert coordinator._last_state is mqtt_cache
    assert coordinator._last_data_source == "mqtt_cache"


@pytest.mark.asyncio
async def test_no_http_ever_falls_back_to_mqtt_cache() -> None:
    """First boot: no HTTP fetch yet, MQTT cache is the only source.
    The guard must not block it.
    """
    now = time.monotonic()
    mqtt_cache = MagicMock()
    mqtt_cache.battery = 42

    coordinator = _make_coordinator(
        cached_state=mqtt_cache,
        http_fetch_at=None,  # no HTTP ever
        mqtt_state_at=now - 20,
    )

    await coordinator._async_update_data()

    assert coordinator._last_state is mqtt_cache
    assert coordinator._last_data_source == "mqtt_cache"


@pytest.mark.asyncio
async def test_no_mqtt_ever_yet_http_fresh_no_cache_apply() -> None:
    """Robot just onboarded, MQTT never delivered a state, HTTP already
    ran. The SDK cache should be None so nothing to guard against, but
    the guard must not raise on the missing timestamp.
    """
    now = time.monotonic()

    coordinator = _make_coordinator(
        cached_state=None,  # SDK has nothing
        http_fetch_at=now - 5,
        mqtt_state_at=None,
    )

    await coordinator._async_update_data()

    # _last_state was pre-seeded MagicMock; nothing overwrote it.
    assert coordinator._last_data_source is None
