"""BUG-05 — MQTT `/state` pushes with stale battery.

The Navimow cloud replays the last-buffered `/state` payload at every
WSS reconnect (~40 min). If the buffered payload pre-dates the physical
robot state, the battery field it carries is a lie (e.g. `docked,
battery=100` from before a mowing departure, or `docked, battery=68`
from before charging).

The original BUG-05 fix compared the payload's own `timestamp` field to
the previously held state's timestamp and dropped the push when strictly
older. That worked for the buffered-replay pattern but missed the
2026-07-03 pattern documented in BUG-08 (#45): the cloud forwards stale
battery *content* with a **fresh** firmware timestamp, so the guard
never fires (0/7 pushes dropped over the trace) and the sensor still
flips backward for ~60-90 s until the next HTTP-fallback tick.

BUG-08 retires the timestamp guard and replaces it with a stronger
invariant: HTTP is the sole source of truth for `battery`. Every
`/state` push is accepted — for its state/error/timestamp fields — but
the previously held `battery` value is threaded through, so a stale
battery from any MQTT source can never land in `_last_state.battery`.

These tests lock in that invariant.
"""

from __future__ import annotations

from unittest.mock import MagicMock


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


def _make_state(*, timestamp: int | None, battery: int, state: str = "isRunning"):
    msg = MagicMock()
    msg.device_id = "REDACTED-ROBOT-SERIAL"
    msg.timestamp = timestamp
    msg.battery = battery
    msg.state = state
    return msg


# --------------------------------------------------------------------- #
# _handle_state — clock bump + scheduling                               #
# --------------------------------------------------------------------- #


def test_handle_state_schedules_update_and_bumps_clock() -> None:
    """`_handle_state` accepts every payload whose device_id matches:
    scheduling `_update_from_state` on the HA loop and stamping the
    MQTT state clock. The retired BUG-05 timestamp guard is no longer
    in the way — the battery invariant lives one level down in
    `_update_from_state`.
    """
    coordinator = _make_coordinator()
    fresh = _make_state(timestamp=1_000_000_000_000, battery=85)

    coordinator._handle_state(fresh)

    coordinator.hass.loop.call_soon_threadsafe.assert_called_once_with(
        coordinator._update_from_state, fresh
    )
    assert coordinator._last_mqtt_state_update is not None


def test_handle_state_older_timestamp_no_longer_dropped() -> None:
    """Post-BUG-08: `_handle_state` accepts even a payload whose
    firmware timestamp is strictly older than the currently held
    state's — the timestamp guard is gone. The BUG-08 invariant
    (`battery = HTTP`) is what actually protects the sensor.
    """
    coordinator = _make_coordinator()
    coordinator._last_state = _make_state(timestamp=1_000_000_000_000, battery=85)

    stale = _make_state(timestamp=999_000_000_000, battery=100)
    coordinator._handle_state(stale)

    coordinator.hass.loop.call_soon_threadsafe.assert_called_once_with(
        coordinator._update_from_state, stale
    )


def test_handle_state_wrong_device_id_still_dropped() -> None:
    """The pre-existing device_id gate is unchanged: a foreign robot's
    payload never reaches the scheduler.
    """
    coordinator = _make_coordinator()
    coordinator._last_state = _make_state(timestamp=1_000_000_000_000, battery=85)

    foreign = MagicMock()
    foreign.device_id = "OTHER-ROBOT"
    foreign.timestamp = 2_000_000_000_000
    foreign.battery = 42
    foreign.state = "isRunning"

    coordinator._handle_state(foreign)

    coordinator.hass.loop.call_soon_threadsafe.assert_not_called()


# --------------------------------------------------------------------- #
# _update_from_state — the actual BUG-05/BUG-08 protection              #
# --------------------------------------------------------------------- #


def test_update_from_state_preserves_battery_from_previous_state() -> None:
    """The canonical BUG-05 scenario, restated for BUG-08: a reconnect
    replay push carrying an old battery (`100`) hits `_update_from_state`
    while `_last_state.battery` holds the HTTP truth (`87`). The battery
    must stay at `87`; non-battery fields land freshly.
    """
    coordinator = _make_coordinator()
    coordinator._last_state = _make_state(
        timestamp=1_000_000_000_000, battery=87, state="isRunning"
    )

    replay = _make_state(timestamp=1_000_000_030_000, battery=100, state="isRunning")
    coordinator._update_from_state(replay)

    assert coordinator._last_state is replay
    assert coordinator._last_state.battery == 87  # HTTP truth preserved
    assert coordinator._last_state.state == "isRunning"


def test_update_from_state_first_ever_uses_payload_battery() -> None:
    """Cold start: no `_last_state` to preserve from → the first push's
    battery lands verbatim. The invariant re-arms itself from that
    point on.
    """
    coordinator = _make_coordinator()

    first = _make_state(timestamp=1_000_000_000_000, battery=42)
    coordinator._update_from_state(first)

    assert coordinator._last_state is first
    assert coordinator._last_state.battery == 42


def test_update_from_state_fresh_content_battery_still_ignored() -> None:
    """The invariant is unconditional: even a fresh, plausible battery
    on the MQTT push must not overwrite the HTTP-held value. HTTP is
    the sole writer, whatever the payload happens to say.
    """
    coordinator = _make_coordinator()
    coordinator._last_state = _make_state(
        timestamp=1_000_000_000_000, battery=87, state="isRunning"
    )

    plausible = _make_state(timestamp=1_000_000_030_000, battery=86, state="isRunning")
    coordinator._update_from_state(plausible)

    assert coordinator._last_state.battery == 87


def test_update_from_state_marks_source_as_mqtt_push() -> None:
    """Regression guard on `_last_data_source` telemetry — helpful for
    diagnostics when reasoning about which path last wrote the state.
    """
    coordinator = _make_coordinator()
    coordinator._last_state = _make_state(timestamp=1_000_000_000_000, battery=90)

    coordinator._update_from_state(_make_state(timestamp=1_000_000_030_000, battery=42))

    assert coordinator._last_data_source == "mqtt_push"
