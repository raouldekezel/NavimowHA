"""MQTT `/state` push path — battery now sourced from MQTT (FEAT-11).

BUG-05/BUG-08 made HTTP the sole writer of `battery`: every `/state`
push had its battery replaced with the previously held value, because
the old firmware forwarded stale battery content on reconnect replays.

OS V4.3.0 inverts that premise — the live mowing capture in
`docs/diag/2026-08-23_feat-11_v43-battery-mowing/` showed `/state`
carries the correct, changing battery while mowing and charging, and the
discard was pinning HA to a stale value. FEAT-11 reverts the suppression:
`_update_from_state` accepts the push as-is, battery included.

These tests lock the reverted behaviour on the push path, plus the
`_handle_state` gating that is unchanged by the revert.
"""

from __future__ import annotations

from unittest.mock import MagicMock

from mower_sdk.models import DeviceStateMessage


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
    coordinator._mqtt_disconnect_ticks = 0
    coordinator._build_data = MagicMock(return_value={})
    coordinator.async_set_updated_data = MagicMock()
    return coordinator


def _state(
    *,
    battery: int | None,
    timestamp: int | None = 1_000_000_000_000,
    state: str = "isRunning",
    device_id: str = "REDACTED-ROBOT-SERIAL",
):
    return DeviceStateMessage(
        device_id=device_id,
        timestamp=timestamp,
        state=state,
        battery=battery,
    )


# --------------------------------------------------------------------- #
# _handle_state — clock bump + scheduling (unchanged by FEAT-11)        #
# --------------------------------------------------------------------- #


def test_handle_state_schedules_update_and_bumps_clock() -> None:
    """`_handle_state` accepts every payload whose device_id matches:
    scheduling `_update_from_state` on the HA loop and stamping the
    MQTT state clock.
    """
    coordinator = _make_coordinator()
    fresh = _state(battery=85)

    coordinator._handle_state(fresh)

    coordinator.hass.loop.call_soon_threadsafe.assert_called_once_with(
        coordinator._update_from_state, fresh
    )
    assert coordinator._last_mqtt_state_update is not None


def test_handle_state_older_timestamp_not_dropped() -> None:
    """`_handle_state` accepts even a payload whose firmware timestamp is
    strictly older than the currently held state's — there is no
    timestamp guard on this path.
    """
    coordinator = _make_coordinator()
    coordinator._last_state = _state(battery=85, timestamp=1_000_000_000_000)

    stale = _state(battery=100, timestamp=999_000_000_000)
    coordinator._handle_state(stale)

    coordinator.hass.loop.call_soon_threadsafe.assert_called_once_with(
        coordinator._update_from_state, stale
    )


def test_handle_state_wrong_device_id_still_dropped() -> None:
    """The pre-existing device_id gate is unchanged: a foreign robot's
    payload never reaches the scheduler.
    """
    coordinator = _make_coordinator()
    coordinator._last_state = _state(battery=85)

    foreign = _state(
        battery=42,
        timestamp=2_000_000_000_000,
        device_id="OTHER-ROBOT",
    )
    coordinator._handle_state(foreign)

    coordinator.hass.loop.call_soon_threadsafe.assert_not_called()


# --------------------------------------------------------------------- #
# _update_from_state — MQTT battery is written (FEAT-11 revert)         #
# --------------------------------------------------------------------- #


def test_update_from_state_writes_incoming_battery() -> None:
    """A fresh MQTT push carrying a changed battery updates
    `_last_state.battery` — MQTT is the battery source again. Non-battery
    fields land freshly too.
    """
    coordinator = _make_coordinator()
    coordinator._last_state = _state(
        battery=100, timestamp=1_000_000_000_000, state="isRunning"
    )

    push = _state(battery=99, timestamp=1_000_000_030_000, state="isRunning")
    coordinator._update_from_state(push)

    assert coordinator._last_state.battery == 99
    assert coordinator._last_state.state == "isRunning"
    # The payload lands as-is (same reference); no copy is made.
    assert coordinator._last_state is push


def test_update_from_state_first_ever_uses_payload_battery() -> None:
    """Cold start: no `_last_state` yet → the first push's battery lands
    verbatim.
    """
    coordinator = _make_coordinator()

    first = _state(battery=42, timestamp=1_000_000_000_000)
    coordinator._update_from_state(first)

    assert coordinator._last_state is first
    assert coordinator._last_state.battery == 42


def test_update_from_state_marks_source_as_mqtt_push() -> None:
    """Regression guard on `_last_data_source` telemetry — helpful for
    diagnostics when reasoning about which path last wrote the state.
    """
    coordinator = _make_coordinator()
    coordinator._last_state = _state(battery=90, timestamp=1_000_000_000_000)

    coordinator._update_from_state(_state(battery=42, timestamp=1_000_000_030_000))

    assert coordinator._last_data_source == "mqtt_push"
