"""FEAT-05 step (a) — /location parser extension + type-2 ordering guard.

Prerequisite for the run tracker (#43). Two shifts on the /location
plumbing that the tracker will consume:

1. Parser exposes the firmware `time` field on both type-1 and type-2
   items, plus `mow_start_type` and `sub_action` on type-2. These were
   silently dropped by the FEAT-01/02 parsers; the tracker's ordering
   guard and per-run metadata need them.
2. `NavimowCoordinator._handle_location_stats` drops a type-2 whose
   firmware `time` is not strictly greater than the last accepted
   type-2's. The 2026-05-25 diag (#20) contains a real out-of-order
   packet — firmware time 2026-05-25T12:01:15 UTC delivered at
   13:38:59 UTC, 1 h 37 min late. Its content is a valid *earlier* run
   snapshot; without the guard the accumulators (subtotal, mp, cmp)
   regress and the downstream tracker would misfire.

Guard is intentionally *ordering-only*, not shape-based. The SPIKE-02
answer on #43 shows a shape filter (e.g. "reject mp=0 mid-run") would
misfire on the legitimate first packet of a fresh run (BUG-07 trace).
"""

from __future__ import annotations

from unittest.mock import MagicMock

# --------------------------------------------------------------------- #
# 1. parser — new fields exposed on type-1                              #
# --------------------------------------------------------------------- #


def test_parse_location_type_1_exposes_time() -> None:
    from custom_components.navimow.location import parse_location_type_1

    parsed = parse_location_type_1(
        {
            "type": 1,
            "postureX": "1.0",
            "postureY": "2.0",
            "postureTheta": "0.5",
            "vehicleState": 4,
            "time": 1779694241252,
        }
    )
    assert parsed is not None
    assert parsed["time"] == 1779694241252


def test_parse_location_type_1_time_defaults_to_none() -> None:
    from custom_components.navimow.location import parse_location_type_1

    parsed = parse_location_type_1(
        {"type": 1, "postureX": "0", "postureY": "0"}
    )
    assert parsed is not None
    assert parsed["time"] is None


# --------------------------------------------------------------------- #
# 2. parser — new fields exposed on type-2                              #
# --------------------------------------------------------------------- #


def test_parse_location_type_2_exposes_time_mowstarttype_subaction() -> None:
    """The exact 2026-05-25 late-delivery packet (diag #20, line 18) — the
    canonical fixture for the SPIKE-02 ordering-guard argument.
    """
    from custom_components.navimow.location import parse_location_type_2

    parsed = parse_location_type_2(
        {
            "type": 2,
            "action": 8,
            "currentMowBoundary": 1,
            "currentMowProgress": 16,
            "mowingPercentage": 0,
            "mowingWeekArea": "124.15",
            "subtotalArea": "0.39",
            "mowStartType": 1,
            "subAction": 6,
            "time": 1779710475448,
        }
    )
    assert parsed is not None
    assert parsed["time"] == 1779710475448
    assert parsed["mow_start_type"] == 1
    assert parsed["sub_action"] == 6


def test_parse_location_type_2_new_fields_default_to_none() -> None:
    from custom_components.navimow.location import parse_location_type_2

    parsed = parse_location_type_2({"type": 2, "mowingWeekArea": "42.0"})
    assert parsed is not None
    assert parsed["time"] is None
    assert parsed["mow_start_type"] is None
    assert parsed["sub_action"] is None


# --------------------------------------------------------------------- #
# 3. coordinator — ordering guard                                       #
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
    coordinator.stats = None
    coordinator._last_location_stats_time = None
    coordinator._build_data = MagicMock(return_value={})
    coordinator.async_set_updated_data = MagicMock()
    return coordinator


def _type_2(*, time: int | None, mp: int = 5, subtotal: str = "10.0", boundary: int = 1):
    """Minimal type-2 shape; `time` is the axis under test."""
    item = {
        "type": 2,
        "currentMowBoundary": boundary,
        "currentMowProgress": 500,
        "mowingPercentage": mp,
        "subtotalArea": subtotal,
        "mowingWeekArea": "100.0",
    }
    if time is not None:
        item["time"] = time
    return item


def test_first_type_2_is_accepted_and_stamps_the_clock() -> None:
    coordinator = _make_coordinator()

    coordinator.handle_location_item(_type_2(time=1779694531266, mp=1))

    assert coordinator.stats is not None
    assert coordinator.stats["mowing_percentage"] == 1
    assert coordinator._last_location_stats_time == 1779694531266
    coordinator.async_set_updated_data.assert_called_once()


def test_strictly_newer_type_2_is_accepted() -> None:
    coordinator = _make_coordinator()
    coordinator._last_location_stats_time = 1779694531266

    coordinator.handle_location_item(
        _type_2(time=1779694541261, mp=2, subtotal="20.0")
    )

    assert coordinator.stats["mowing_percentage"] == 2
    assert coordinator._last_location_stats_time == 1779694541261


def test_late_delivery_type_2_is_dropped(caplog) -> None:
    """The canonical fixture: after accepting the resumption packet
    (`time=1779724117341` = 2026-05-25T15:48:37 UTC), a *later-delivered*
    packet whose firmware `time=1779710475448` is 3 h 46 min in the past
    must not overwrite the accumulators.
    """
    coordinator = _make_coordinator()
    # Prime with the "fresh" resumption packet.
    coordinator.handle_location_item(
        _type_2(time=1779724117341, mp=63, subtotal="227.82", boundary=1)
    )
    assert coordinator.stats["mowing_percentage"] == 63

    caplog.clear()
    # Now inject the late-delivered packet — same run marker but earlier
    # firmware timestamp.
    with caplog.at_level("DEBUG", logger="custom_components.navimow.coordinator"):
        coordinator.handle_location_item(
            _type_2(time=1779710475448, mp=0, subtotal="0.39", boundary=1)
        )

    # Stats untouched by the drop.
    assert coordinator.stats["mowing_percentage"] == 63
    assert coordinator.stats["area_session"] == 227.82
    # Clock untouched by the drop.
    assert coordinator._last_location_stats_time == 1779724117341
    # DEBUG line emitted.
    assert any(
        "DROPPED as out-of-order" in rec.getMessage() for rec in caplog.records
    ), [rec.getMessage() for rec in caplog.records]


def test_equal_timestamp_type_2_is_dropped() -> None:
    """Strict-less-than: a re-delivered packet with the same firmware
    `time` as the last accepted one carries no new information and must
    not spuriously refresh downstream entities.
    """
    coordinator = _make_coordinator()
    coordinator.handle_location_item(_type_2(time=1779694531266, mp=10, subtotal="50.0"))
    coordinator.async_set_updated_data.reset_mock()

    coordinator.handle_location_item(_type_2(time=1779694531266, mp=99, subtotal="99.0"))

    assert coordinator.stats["mowing_percentage"] == 10  # unchanged
    assert coordinator.stats["area_session"] == 50.0
    coordinator.async_set_updated_data.assert_not_called()


def test_guard_recovers_after_drop() -> None:
    """A dropped packet must not corrupt the clock: the next fresh
    packet (strictly greater `time` than the *last accepted*) still
    lands.
    """
    coordinator = _make_coordinator()
    coordinator.handle_location_item(_type_2(time=1000, mp=5))
    coordinator.handle_location_item(_type_2(time=500, mp=1))  # dropped
    coordinator.handle_location_item(_type_2(time=2000, mp=7, subtotal="30.0"))

    assert coordinator.stats["mowing_percentage"] == 7
    assert coordinator.stats["area_session"] == 30.0
    assert coordinator._last_location_stats_time == 2000


def test_type_2_without_time_is_accepted_and_leaves_clock_alone() -> None:
    """Defensive tolerance: a firmware variant that omits `time`
    entirely (never observed on i210 in ~180 committed packets, but
    the parser tolerates the shape) still populates stats. The
    ordering clock stays at whatever the last *timestamped* packet
    left it at.
    """
    coordinator = _make_coordinator()
    coordinator.handle_location_item(_type_2(time=1000, mp=5))

    coordinator.handle_location_item(_type_2(time=None, mp=6, subtotal="12.0"))

    assert coordinator.stats["mowing_percentage"] == 6
    assert coordinator.stats["area_session"] == 12.0
    assert coordinator._last_location_stats_time == 1000  # unchanged


def test_first_type_2_without_time_is_accepted() -> None:
    """Cold start with a time-less payload: no ordering signal to gate
    on, but data lands and the clock stays None until a timestamped
    packet arrives.
    """
    coordinator = _make_coordinator()

    coordinator.handle_location_item(_type_2(time=None, mp=3))

    assert coordinator.stats["mowing_percentage"] == 3
    assert coordinator._last_location_stats_time is None
