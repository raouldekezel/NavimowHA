"""BUG-03 — state vs attribute staleness separation.

Regression tests for the split of `_last_mqtt_update` into two clocks:
- `_last_mqtt_state_update` — bumped only by `_handle_state()`
- `_last_mqtt_update` — bumped by both handlers (catch-all)

The staleness check in `_async_update_data` must read the state-specific
clock, so that attribute packets can NOT keep the coordinator thinking
MQTT is fresh while the actual vehicle state is stale.
"""

from __future__ import annotations

import time
from unittest.mock import AsyncMock, MagicMock

import pytest


def _make_coordinator():
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
    coordinator.api = api

    coordinator._last_state = MagicMock()
    coordinator._last_attributes = None
    coordinator._last_mqtt_update = None
    coordinator._last_mqtt_state_update = None
    coordinator._last_http_fetch = None
    coordinator._last_data_source = None
    coordinator.oauth_session = None
    coordinator._mqtt_disconnect_warned = False  # introduced by BUG-01

    coordinator._device_status_to_state = MagicMock(return_value=MagicMock())
    coordinator._build_data = MagicMock(return_value={})
    coordinator.async_set_updated_data = MagicMock()
    return coordinator, api


def test_state_update_bumps_both_clocks() -> None:
    """`_handle_state` must bump both `_last_mqtt_update` and
    `_last_mqtt_state_update` so state freshness is tracked correctly.
    """
    coordinator, _api = _make_coordinator()
    coordinator._update_from_state = MagicMock()

    msg = MagicMock()
    msg.device_id = coordinator.device.id
    msg.state = "docked"
    msg.battery = 78

    before = time.monotonic()
    coordinator._handle_state(msg)
    after = time.monotonic()

    assert coordinator._last_mqtt_update is not None
    assert coordinator._last_mqtt_state_update is not None
    assert before <= coordinator._last_mqtt_update <= after
    assert coordinator._last_mqtt_state_update == coordinator._last_mqtt_update


def test_attribute_update_bumps_only_generic_clock() -> None:
    """`_handle_attributes` must NOT bump `_last_mqtt_state_update`.
    Regression: without this split, periodic attribute pushes on a
    docked robot suppress the HTTP fallback even while its state is
    genuinely stale.
    """
    coordinator, _api = _make_coordinator()
    coordinator._update_from_attributes = MagicMock()

    msg = MagicMock()
    msg.device_id = coordinator.device.id

    coordinator._handle_attributes(msg)

    assert coordinator._last_mqtt_update is not None
    assert coordinator._last_mqtt_state_update is None


@pytest.mark.asyncio
async def test_fallback_fires_when_state_stale_but_attrs_fresh() -> None:
    """The staleness check must key on `_last_mqtt_state_update` alone.
    Setup: 5 min ago the last MQTT state; attribute packets keep bumping
    `_last_mqtt_update` up to now. HTTP fallback must fire despite the
    generic clock being fresh.
    """
    coordinator, api = _make_coordinator()
    api.async_get_device_status = AsyncMock(return_value=MagicMock())

    now = time.monotonic()
    coordinator._last_mqtt_update = now - 2  # attribute packet 2 s ago
    coordinator._last_mqtt_state_update = now - 600  # state 10 min ago
    coordinator._last_http_fetch = None

    await coordinator._async_update_data()

    api.async_get_device_status.assert_awaited_once()
    assert coordinator._last_data_source == "http_fallback"


@pytest.mark.asyncio
async def test_fallback_skipped_when_state_fresh_regardless_of_attrs() -> None:
    """Symmetric guard: fresh state must not trigger the HTTP fallback
    even if the generic clock (attribute packets) is somehow stale.
    """
    coordinator, api = _make_coordinator()
    api.async_get_device_status = AsyncMock()

    now = time.monotonic()
    coordinator._last_mqtt_update = now - 600  # no attributes for 10 min
    coordinator._last_mqtt_state_update = now - 5  # state 5 s ago — fresh
    coordinator._last_http_fetch = None

    await coordinator._async_update_data()

    api.async_get_device_status.assert_not_called()
