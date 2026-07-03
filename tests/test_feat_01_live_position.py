"""FEAT-01 — live position tracking via the /realtimeDate/location channel.

Tests cover:
1. The pure parsers (`parse_location_type_1`, `parse_location_payload`)
   in isolation — no HA, no MQTT.
2. The coordinator side: `handle_location_item` type 1 populates
   `coordinator.position` + `coordinator.vehicle_state`, dispatches via
   the position signal, and refreshes CoordinatorEntity when
   vehicleState changes.
3. The binary_sensor `charging` description reads `vehicle_state`
   correctly (2 = on, other = off, None = unknown).
"""

from __future__ import annotations

import time
from unittest.mock import MagicMock, patch

# --------------------------------------------------------------------- #
# 1. pure parsers                                                       #
# --------------------------------------------------------------------- #


def test_parse_location_type_1_full_payload() -> None:
    from custom_components.navimow.location import parse_location_type_1

    item = {
        "type": 1,
        "postureX": "1.23",
        "postureY": "-4.56",
        "postureTheta": "0.78",
        "vehicleState": 4,
    }
    parsed = parse_location_type_1(item)

    assert parsed is not None
    assert parsed["x"] == 1.23
    assert parsed["y"] == -4.56
    assert parsed["theta"] == 0.78
    assert parsed["vehicle_state"] == 4
    assert parsed["distance"] == 4.72  # √(1.23² + 4.56²) rounded to 2


def test_parse_location_type_1_drops_when_x_missing() -> None:
    from custom_components.navimow.location import parse_location_type_1

    assert parse_location_type_1({"type": 1, "postureY": "0.5"}) is None
    assert parse_location_type_1({"type": 1, "postureX": "x"}) is None


def test_parse_location_type_1_optional_fields_default_none() -> None:
    from custom_components.navimow.location import parse_location_type_1

    parsed = parse_location_type_1({"type": 1, "postureX": "0", "postureY": "0"})
    assert parsed is not None
    assert parsed["theta"] is None
    assert parsed["vehicle_state"] is None
    assert parsed["distance"] == 0.0


def test_parse_location_payload_valid_array() -> None:
    from custom_components.navimow.location import parse_location_payload

    payload = b'[{"type":1,"postureX":"1","postureY":"2"}]'
    items = parse_location_payload(payload)
    assert items == [{"type": 1, "postureX": "1", "postureY": "2"}]


def test_parse_location_payload_invalid_json_returns_none() -> None:
    from custom_components.navimow.location import parse_location_payload

    assert parse_location_payload(b"not json") is None
    assert parse_location_payload(b"") is None


def test_parse_location_payload_non_array_returns_none() -> None:
    from custom_components.navimow.location import parse_location_payload

    assert parse_location_payload(b'{"type":1}') is None


# --------------------------------------------------------------------- #
# 2. coordinator wiring                                                 #
# --------------------------------------------------------------------- #


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

    coordinator.position = None
    coordinator.vehicle_state = None
    coordinator._last_position_dispatch = 0.0
    # FEAT-05 layer-1 guard cursors + drop streaks, initialised so
    # `__new__`-built test coordinators can invoke the /location handlers.
    coordinator._last_accepted_time_type1 = None
    coordinator._last_accepted_time_type2 = None
    coordinator._type1_drop_streak = 0
    coordinator._type2_drop_streak = 0
    # FEAT-05 (b): run tracker (idle, emits nothing until fed).
    from custom_components.navimow.run_tracker import RunTracker

    coordinator.run_tracker = RunTracker()
    coordinator._build_data = MagicMock(return_value={})
    coordinator.async_set_updated_data = MagicMock()
    return coordinator


def test_handle_location_item_type_1_populates_position() -> None:
    coordinator = _make_coordinator()
    with patch(
        "custom_components.navimow.coordinator.async_dispatcher_send"
    ) as dispatcher:
        coordinator.handle_location_item(
            {"type": 1, "postureX": "3.0", "postureY": "4.0", "vehicleState": 2}
        )

    assert coordinator.position is not None
    assert coordinator.position["x"] == 3.0
    assert coordinator.position["y"] == 4.0
    assert coordinator.position["distance"] == 5.0
    assert coordinator.vehicle_state == 2
    # vehicleState transitioned None→2, dispatcher fires + coordinator refreshes.
    dispatcher.assert_called_once()
    coordinator.async_set_updated_data.assert_called_once()


def test_handle_location_item_ignores_type_3_heartbeat() -> None:
    """type 3 is a heartbeat (`{time, type}`) with no pose payload — it
    must be silently ignored and not affect coordinator.position or
    vehicle_state. FEAT-02 handles type 2; FEAT-01 covers only that the
    non-pose paths don't leak into the pose state.
    """
    coordinator = _make_coordinator()
    with patch(
        "custom_components.navimow.coordinator.async_dispatcher_send"
    ) as dispatcher:
        coordinator.handle_location_item({"type": 3, "time": 1_779_570_004_762})

    assert coordinator.position is None
    assert coordinator.vehicle_state is None
    dispatcher.assert_not_called()
    coordinator.async_set_updated_data.assert_not_called()


def test_position_dispatch_is_throttled_when_state_stable() -> None:
    """Second and third type-1 payloads within POSITION_THROTTLE_SECONDS
    with unchanged vehicleState must NOT fire a fresh dispatcher.
    """
    coordinator = _make_coordinator()
    coordinator.vehicle_state = 4  # mowing — steady

    now = time.monotonic()
    coordinator._last_position_dispatch = now  # just dispatched

    with patch(
        "custom_components.navimow.coordinator.async_dispatcher_send"
    ) as dispatcher:
        coordinator.handle_location_item(
            {"type": 1, "postureX": "5.0", "postureY": "0.0", "vehicleState": 4}
        )
    # 0 s since last dispatch, throttle 5 s → no fresh dispatch.
    dispatcher.assert_not_called()


def test_vehicle_state_change_bypasses_throttle() -> None:
    """A transition (mowing → docked → charging) must dispatch immediately
    to update the charging binary_sensor.
    """
    coordinator = _make_coordinator()
    coordinator.vehicle_state = 4  # mowing
    coordinator._last_position_dispatch = time.monotonic()  # just dispatched

    with patch(
        "custom_components.navimow.coordinator.async_dispatcher_send"
    ) as dispatcher:
        coordinator.handle_location_item(
            {"type": 1, "postureX": "0.5", "postureY": "0.1", "vehicleState": 2}
        )

    assert coordinator.vehicle_state == 2
    dispatcher.assert_called_once()
    coordinator.async_set_updated_data.assert_called_once()


# --------------------------------------------------------------------- #
# 3. charging binary_sensor description                                 #
# --------------------------------------------------------------------- #


def _find_desc(key):
    from custom_components.navimow.binary_sensor import BINARY_SENSOR_DESCRIPTIONS

    return next(d for d in BINARY_SENSOR_DESCRIPTIONS if d.key == key)


def test_charging_binary_sensor_on_when_vehicle_state_2() -> None:
    desc = _find_desc("charging")
    coordinator = MagicMock()
    coordinator.vehicle_state = 2

    assert desc.is_on_fn(coordinator) is True


def test_charging_binary_sensor_off_when_vehicle_state_other() -> None:
    desc = _find_desc("charging")
    coordinator = MagicMock()
    coordinator.vehicle_state = 4  # mowing

    assert desc.is_on_fn(coordinator) is False


def test_charging_binary_sensor_unknown_before_first_location_payload() -> None:
    """Before any /location payload arrives, `vehicle_state` is None →
    the binary_sensor returns None → HA renders "unknown".
    """
    desc = _find_desc("charging")
    coordinator = MagicMock()
    coordinator.vehicle_state = None

    assert desc.is_on_fn(coordinator) is None
