"""HARD-17 — propagate FEAT-09's `refresh_on_zone_rename` opt-in to
`sensor.<slug>_current_zone`.

Both `current_zone` and `last_run_zones` read
`config_entry.options[OPTIONS_KEY_ZONES]` in their `value_fn` and thus
both benefit from an instant re-render when the operator renames a
zone through the options flow — vs the ≤30 s coordinator-tick lag we
had until now (flagged during HARD-11 review). Pure one-line opt-in:
the mechanism is FEAT-09's, the wiring is
`NavimowSensor.async_added_to_hass`.

Every test exercises one seam:

- Description contract: `current_zone` opts in, `last_run_zones` still
  opts in (regression), everything else stays off.
- Wiring: `async_added_to_hass` on `current_zone` subscribes to
  `SIGNAL_ZONE_NAMES_UPDATED_<entry_id>`, and the callback pushes
  `async_write_ha_state`.
- Value regression: `_current_zone_display` output is unchanged (this
  is a wiring-only ticket).
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from custom_components.navimow.const import OPTIONS_KEY_ZONES, SIGNAL_ZONE_NAMES_UPDATED
from custom_components.navimow.run_tracker import STATE_IDLE, STATE_RUNNING
from custom_components.navimow.sensor import (
    SENSOR_DESCRIPTIONS,
    NavimowSensor,
    NavimowSensorEntityDescription,
    _current_zone_display,
)


def _get_description(key: str) -> NavimowSensorEntityDescription | None:
    for d in SENSOR_DESCRIPTIONS:
        if d.key == key:
            return d
    return None


def _make_coordinator_with_boundary(boundary: int | None, options: dict | None = None):
    coord = MagicMock()
    coord.run_tracker = MagicMock()
    coord.run_tracker.state = STATE_RUNNING if boundary else STATE_IDLE
    coord.run_tracker.current_run = (
        {"zones": [{"boundary_id": boundary}]} if boundary else None
    )
    coord.last_finished_run = None
    coord.get_device_state.return_value = None
    entry = MagicMock()
    entry.entry_id = "test-entry"
    entry.options = options or {}
    coord.config_entry = entry
    return coord


# --------------------------------------------------------------------- #
# 1. Description contract                                               #
# --------------------------------------------------------------------- #


def test_current_zone_opts_into_rename_refresh() -> None:
    """HARD-17: the one-line change under test."""
    d = _get_description("current_zone")
    assert d is not None
    assert d.refresh_on_zone_rename is True


def test_last_run_zones_still_opts_in() -> None:
    """FEAT-09 regression — must stay on."""
    d = _get_description("last_run_zones")
    assert d is not None
    assert d.refresh_on_zone_rename is True


def test_other_last_run_descriptors_still_opt_out() -> None:
    """FEAT-09 regression — timestamp/duration/result don't read the
    operator's zone names and stay off."""
    for key in ("last_run_started", "last_run_duration", "last_run_result"):
        d = _get_description(key)
        assert d is not None
        assert d.refresh_on_zone_rename is False, f"{key} unexpectedly opted in"


def test_battery_and_position_still_opt_out() -> None:
    """Non-zone sensors have no reason to wake on a rename. Locks the
    scope discipline of this ticket: only the two zone-name-reading
    descriptors opt in."""
    for key in (
        "battery",
        "weekly_area",
        "run_progress",
        "zone_progress",
        "run_state",
        "current_run_started",
    ):
        d = _get_description(key)
        assert d is not None
        assert d.refresh_on_zone_rename is False, f"{key} unexpectedly opted in"


def test_exactly_two_descriptors_opt_in() -> None:
    """Belt-and-braces: the total count of opted-in descriptors is
    exactly 2 (`current_zone`, `last_run_zones`). A future addition
    trips this test — prompting an explicit decision rather than an
    accidental opt-in."""
    opted_in = [d.key for d in SENSOR_DESCRIPTIONS if d.refresh_on_zone_rename]
    assert set(opted_in) == {"current_zone", "last_run_zones"}, opted_in


# --------------------------------------------------------------------- #
# 2. Wiring — async_added_to_hass on current_zone                       #
# --------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_current_zone_async_added_subscribes_to_names_updated() -> None:
    """The base class (from FEAT-09) subscribes when the description
    opts in. Nothing to add — this test just pins the fact that the
    `current_zone` descriptor benefits from that wiring."""
    coord = _make_coordinator_with_boundary(None)
    sensor = NavimowSensor(
        coordinator=coord,
        entity_description=_get_description("current_zone"),
    )
    sensor.hass = MagicMock()
    sensor.async_write_ha_state = MagicMock()
    sensor.async_on_remove = MagicMock()
    calls: list = []

    def _fake_dispatcher_connect(hass, signal, cb):
        calls.append((hass, signal, cb))
        return lambda: None

    async def _noop(self):
        return None

    async def _get_last(self):
        return None

    with (
        patch(
            "custom_components.navimow.sensor.async_dispatcher_connect",
            side_effect=_fake_dispatcher_connect,
        ),
        patch(
            "homeassistant.helpers.update_coordinator.CoordinatorEntity"
            ".async_added_to_hass",
            _noop,
        ),
        patch(
            "homeassistant.components.sensor.RestoreSensor.async_get_last_sensor_data",
            _get_last,
        ),
    ):
        await sensor.async_added_to_hass()

    signals = [signal for _, signal, _ in calls]
    assert f"{SIGNAL_ZONE_NAMES_UPDATED}_test-entry" in signals


@pytest.mark.asyncio
async def test_current_zone_rename_callback_writes_ha_state() -> None:
    coord = _make_coordinator_with_boundary(None)
    sensor = NavimowSensor(
        coordinator=coord,
        entity_description=_get_description("current_zone"),
    )
    sensor.hass = MagicMock()
    sensor.async_write_ha_state = MagicMock()
    sensor.async_on_remove = MagicMock()
    captured_cb: list = []

    def _fake_dispatcher_connect(hass, signal, cb):
        captured_cb.append(cb)
        return lambda: None

    async def _noop(self):
        return None

    async def _get_last(self):
        return None

    with (
        patch(
            "custom_components.navimow.sensor.async_dispatcher_connect",
            side_effect=_fake_dispatcher_connect,
        ),
        patch(
            "homeassistant.helpers.update_coordinator.CoordinatorEntity"
            ".async_added_to_hass",
            _noop,
        ),
        patch(
            "homeassistant.components.sensor.RestoreSensor.async_get_last_sensor_data",
            _get_last,
        ),
    ):
        await sensor.async_added_to_hass()

    assert captured_cb, "callback not registered"
    captured_cb[0]()
    sensor.async_write_ha_state.assert_called_once()


# --------------------------------------------------------------------- #
# 3. Value regression — _current_zone_display unchanged                 #
# --------------------------------------------------------------------- #


def test_current_zone_display_named_regression() -> None:
    """HARD-11 / HARD-15 behaviour preserved: with a name set, the
    display is the operator name."""
    coord = _make_coordinator_with_boundary(
        1, {OPTIONS_KEY_ZONES: {"1": {"name": "Prunier"}}}
    )
    assert _current_zone_display(coord) == "Prunier"


def test_current_zone_display_short_hash_fallback_regression() -> None:
    """HARD-11's short `#<id>` fallback preserved (intentional
    divergence with per-zone entities' `Zone #<id>`, see HARD-15
    review on #94)."""
    coord = _make_coordinator_with_boundary(3, None)
    assert _current_zone_display(coord) == "#3"


def test_current_zone_display_none_at_rest_regression() -> None:
    coord = _make_coordinator_with_boundary(None)
    assert _current_zone_display(coord) is None
