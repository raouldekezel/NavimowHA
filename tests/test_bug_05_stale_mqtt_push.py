"""BUG-05 — drop stale MQTT /state pushes replayed at reconnect.

The Navimow cloud replays the last buffered `/state` payload at every WSS
reconnect (~40 min). If that buffered payload pre-dates the physical robot
state (e.g. a `docked, battery=100` from before a mowing departure), it
overwrites the fresher discharging value. The fix guards `_handle_state` by
comparing the incoming payload's own `timestamp` (epoch ms) to the timestamp
of the state we already hold.
"""

from __future__ import annotations

import logging
from unittest.mock import MagicMock

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

    coordinator._last_state = None
    coordinator._last_attributes = None
    coordinator._last_mqtt_update = None
    coordinator._last_mqtt_state_update = None
    coordinator._last_http_fetch = None
    coordinator._last_data_source = None
    coordinator._mqtt_disconnect_warned = False
    coordinator._update_from_state = MagicMock()
    coordinator.async_set_updated_data = MagicMock()
    return coordinator


def _make_state(*, timestamp: int | None, battery: int, state: str = "isRunning"):
    msg = MagicMock()
    msg.device_id = "REDACTED-ROBOT-SERIAL"
    msg.timestamp = timestamp
    msg.battery = battery
    msg.state = state
    return msg


def test_first_ever_state_is_accepted() -> None:
    """Nothing previously held; the very first MQTT push must land."""
    coordinator = _make_coordinator()
    fresh = _make_state(timestamp=1_000_000_000_000, battery=85)

    coordinator._handle_state(fresh)

    coordinator.hass.loop.call_soon_threadsafe.assert_called_once_with(
        coordinator._update_from_state, fresh
    )
    assert coordinator._last_mqtt_state_update is not None


def test_newer_timestamp_replaces_previous_state() -> None:
    """Happy path: descending discharge from 85 to 82 with strictly newer
    timestamps must land.
    """
    coordinator = _make_coordinator()
    coordinator._last_state = _make_state(timestamp=1_000_000_000_000, battery=85)

    fresher = _make_state(timestamp=1_000_000_030_000, battery=82)
    coordinator._handle_state(fresher)

    coordinator.hass.loop.call_soon_threadsafe.assert_called_once_with(
        coordinator._update_from_state, fresher
    )


def test_stale_timestamp_is_dropped_and_scheduler_not_called(caplog) -> None:
    """Core BUG-05 contract: a `/state` push whose payload timestamp is
    strictly older than the currently held state must be dropped — no
    `_update_from_state` scheduling, no clock bump.
    """
    coordinator = _make_coordinator()
    coordinator._last_state = _make_state(timestamp=1_000_000_000_000, battery=85)
    coordinator._last_mqtt_state_update = 12345.0

    stale = _make_state(timestamp=999_000_000_000, battery=100)
    caplog.set_level(logging.DEBUG, logger="custom_components.navimow.coordinator")

    coordinator._handle_state(stale)

    coordinator.hass.loop.call_soon_threadsafe.assert_not_called()
    # Clock did NOT bump — the state clock stays where it was.
    assert coordinator._last_mqtt_state_update == 12345.0
    drops = [r for r in caplog.records if "DROPPED as stale" in r.message]
    assert len(drops) == 1


def test_equal_timestamp_is_treated_as_fresh_not_stale() -> None:
    """Boundary: strictly less-than only. Same-timestamp payloads are
    accepted (they could carry a corrected value, and blocking them would
    be over-aggressive for zero benefit).
    """
    coordinator = _make_coordinator()
    coordinator._last_state = _make_state(timestamp=1_000_000_000_000, battery=85)

    same_ts = _make_state(timestamp=1_000_000_000_000, battery=85)
    coordinator._handle_state(same_ts)

    coordinator.hass.loop.call_soon_threadsafe.assert_called_once_with(
        coordinator._update_from_state, same_ts
    )


def test_missing_timestamp_falls_through_to_old_behaviour() -> None:
    """Defensive: a payload without a `timestamp` field cannot be
    compared, so it must fall through to the pre-BUG-05 behaviour (accept).
    Otherwise a firmware that never populates the field would break silently.
    """
    coordinator = _make_coordinator()
    coordinator._last_state = _make_state(timestamp=1_000_000_000_000, battery=85)

    no_ts = _make_state(timestamp=None, battery=42)
    coordinator._handle_state(no_ts)

    coordinator.hass.loop.call_soon_threadsafe.assert_called_once_with(
        coordinator._update_from_state, no_ts
    )


def test_first_state_without_timestamp_is_still_accepted() -> None:
    """Combined defensive path: no prior state AND no timestamp → accept."""
    coordinator = _make_coordinator()
    no_ts = _make_state(timestamp=None, battery=42)

    coordinator._handle_state(no_ts)

    coordinator.hass.loop.call_soon_threadsafe.assert_called_once_with(
        coordinator._update_from_state, no_ts
    )


def test_wrong_device_id_is_dropped_before_the_ts_check() -> None:
    """Sanity: the pre-existing device_id gate still short-circuits — a
    payload for a different robot never reaches the timestamp guard.
    """
    coordinator = _make_coordinator()
    coordinator._last_state = _make_state(timestamp=1_000_000_000_000, battery=85)

    foreign = MagicMock()
    foreign.device_id = "OTHER-ROBOT"
    foreign.timestamp = 2_000_000_000_000  # would beat any check
    foreign.battery = 42
    foreign.state = "isRunning"

    coordinator._handle_state(foreign)

    coordinator.hass.loop.call_soon_threadsafe.assert_not_called()
