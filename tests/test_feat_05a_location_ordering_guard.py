"""FEAT-05 step (a) — /location parser extension + layer-1 ordering guard.

Prerequisite for the run tracker (#43). Two shifts on the /location
plumbing that the tracker will consume:

1. Parser exposes the firmware `time` field on both type-1 and type-2
   items, plus `mow_start_type` and `sub_action` on type-2. These were
   silently dropped by the FEAT-01/02 parsers; the tracker's layered
   guard and per-run metadata need them.
2. `NavimowCoordinator` drops a /location item whose firmware `time`
   is not strictly greater than the last accepted item's — layer-1 of
   the three-layer guard the tracker in step (b) completes with `wk`
   monotonicity + wk₀+sub invariant. Cursors are per-stream because
   type-1 (~2 s) and type-2 (~30-90 s) have independent cadences: a
   single shared cursor would drop the whole slower stream after every
   faster-stream update.

Guard is intentionally *ordering-only*, not shape-based. The SPIKE
answer on #43 shows a shape filter (e.g. "reject mp=0 mid-run") would
misfire on the legitimate first packet of a fresh run (BUG-07 trace).

Fixture note (Fable brief 2026-07-03): the committed 2026-05-25
sequence alone does *not* exercise any guard — within it, `time` and
`wk` both advance across the late packet. The realistic reproduction
of live conditions needs one synthetic predecessor stamped at the
findings-timeline (~13:37:25 UTC, `wk ≈ 338`), followed by the real
late packet (fw time 12:01:15 UTC, `wk 124.15`).
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

# Fixed wall-clock instant used across the clamp/streak tests (2026-07-03
# 12:00:00 UTC). Keeping it constant avoids `time.time()` drift between
# invocations within a single test and matches the diag session dates.
_NOW_MS = 1783080000000

# --------------------------------------------------------------------- #
# 1. parser — new fields on type-1                                      #
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

    parsed = parse_location_type_1({"type": 1, "postureX": "0", "postureY": "0"})
    assert parsed is not None
    assert parsed["time"] is None


# --------------------------------------------------------------------- #
# 2. parser — new fields on type-2                                      #
# --------------------------------------------------------------------- #


def test_parse_location_type_2_exposes_time_mowstarttype_subaction() -> None:
    """The exact 2026-05-25 late-delivery packet (diag `#20`, line 18) —
    the canonical fixture for the SPIKE ordering-guard argument.
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
    """`sub_action` in particular must stay `None` when the JSON field is
    absent — the packed `mapWorkPosition` word uses 0 for "absent" but
    the parser stays faithful to the JSON (Fable brief).
    """
    from custom_components.navimow.location import parse_location_type_2

    parsed = parse_location_type_2({"type": 2, "mowingWeekArea": "42.0"})
    assert parsed is not None
    assert parsed["time"] is None
    assert parsed["mow_start_type"] is None
    assert parsed["sub_action"] is None


# --------------------------------------------------------------------- #
# 3. coordinator — layer-1 guard on type-2                              #
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
    coordinator._last_accepted_time_type1 = None
    coordinator._last_accepted_time_type2 = None
    coordinator._type1_drop_streak = 0
    coordinator._type2_drop_streak = 0
    coordinator._build_data = MagicMock(return_value={})
    coordinator.async_set_updated_data = MagicMock()
    return coordinator


def _type_2(
    *,
    time: int | None,
    mp: int = 5,
    subtotal: str = "10.0",
    wk: str = "100.0",
    boundary: int = 1,
):
    """Minimal type-2 shape; `time` is the axis under test."""
    item = {
        "type": 2,
        "currentMowBoundary": boundary,
        "currentMowProgress": 500,
        "mowingPercentage": mp,
        "subtotalArea": subtotal,
        "mowingWeekArea": wk,
    }
    if time is not None:
        item["time"] = time
    return item


def _type_1(*, time: int | None, x: str = "1.0", y: str = "2.0", vs: int = 4):
    item = {
        "type": 1,
        "postureX": x,
        "postureY": y,
        "vehicleState": vs,
    }
    if time is not None:
        item["time"] = time
    return item


def test_first_type_2_is_accepted_and_stamps_the_clock() -> None:
    coordinator = _make_coordinator()

    coordinator.handle_location_item(_type_2(time=1779694531266, mp=1))

    assert coordinator.stats is not None
    assert coordinator.stats["mowing_percentage"] == 1
    assert coordinator._last_accepted_time_type2 == 1779694531266
    coordinator.async_set_updated_data.assert_called_once()


def test_strictly_newer_type_2_is_accepted() -> None:
    coordinator = _make_coordinator()
    coordinator._last_accepted_time_type2 = 1779694531266

    coordinator.handle_location_item(_type_2(time=1779694541261, mp=2, subtotal="20.0"))

    assert coordinator.stats["mowing_percentage"] == 2
    assert coordinator._last_accepted_time_type2 == 1779694541261


def test_late_delivered_type_2_is_dropped(caplog) -> None:
    """Reproduce live conditions: after the coordinator has accepted a
    packet stamped at the findings-timeline live values (~13:37:25 UTC
    `2026-05-25`, `wk ≈ 338`), inject the *real* late-delivered packet
    from the committed log — fw `time=1779710475448` (12:01:15 UTC),
    `wk 124.15`, `sub 0.39`. Its `time` is 1 h 36 min in the past
    relative to the synthetic predecessor, so layer-1 must drop it.
    Stats and the type-2 cursor stay untouched.

    The synthetic predecessor is necessary because the committed
    sequence alone leaves the late packet with `time > all prior
    committed times` and would pass the guard — see the Fable brief
    fixture caveat.
    """
    coordinator = _make_coordinator()
    # Synthetic predecessor at findings-timeline live values.
    coordinator.handle_location_item(
        _type_2(time=1779716245448, mp=48, subtotal="180.0", wk="338.0")
    )
    assert coordinator.stats["mowing_percentage"] == 48

    caplog.clear()
    with caplog.at_level("DEBUG", logger="custom_components.navimow.coordinator"):
        # Real late packet from docs/diag/2026-05-25_feat-02_multizone-run/.
        coordinator.handle_location_item(
            _type_2(
                time=1779710475448,
                mp=0,
                subtotal="0.39",
                wk="124.15",
                boundary=1,
            )
        )

    # Stats untouched by the drop.
    assert coordinator.stats["mowing_percentage"] == 48
    assert coordinator.stats["area_session"] == 180.0
    # Cursor untouched by the drop.
    assert coordinator._last_accepted_time_type2 == 1779716245448
    # DEBUG line emitted with the BUG-05-style wording.
    assert any("DROPPED as stale" in rec.getMessage() for rec in caplog.records), [
        rec.getMessage() for rec in caplog.records
    ]


def test_equal_timestamp_type_2_is_dropped() -> None:
    """Strict-less-than: a duplicate carries no new information and must
    not spuriously refresh downstream entities.
    """
    coordinator = _make_coordinator()
    coordinator.handle_location_item(
        _type_2(time=1779694531266, mp=10, subtotal="50.0")
    )
    coordinator.async_set_updated_data.reset_mock()

    coordinator.handle_location_item(
        _type_2(time=1779694531266, mp=99, subtotal="99.0")
    )

    assert coordinator.stats["mowing_percentage"] == 10  # unchanged
    assert coordinator.stats["area_session"] == 50.0
    coordinator.async_set_updated_data.assert_not_called()


def test_guard_recovers_after_drop() -> None:
    """A dropped packet does not corrupt the cursor: the next fresh
    packet (strictly greater `time` than the *last accepted*) still
    lands.
    """
    coordinator = _make_coordinator()
    coordinator.handle_location_item(_type_2(time=1000, mp=5))
    coordinator.handle_location_item(_type_2(time=500, mp=1))  # dropped
    coordinator.handle_location_item(_type_2(time=2000, mp=7, subtotal="30.0"))

    assert coordinator.stats["mowing_percentage"] == 7
    assert coordinator.stats["area_session"] == 30.0
    assert coordinator._last_accepted_time_type2 == 2000


def test_type_2_without_time_is_accepted_and_leaves_cursor_alone() -> None:
    """Defensive tolerance: a firmware variant that omits `time` still
    populates stats. The ordering cursor stays at whatever the last
    *timestamped* packet left it at.
    """
    coordinator = _make_coordinator()
    coordinator.handle_location_item(_type_2(time=1000, mp=5))

    coordinator.handle_location_item(_type_2(time=None, mp=6, subtotal="12.0"))

    assert coordinator.stats["mowing_percentage"] == 6
    assert coordinator.stats["area_session"] == 12.0
    assert coordinator._last_accepted_time_type2 == 1000  # unchanged


def test_first_type_2_without_time_is_accepted() -> None:
    """Cold start with a time-less payload: no ordering signal to gate
    on, data lands, cursor stays None until a timestamped packet
    arrives.
    """
    coordinator = _make_coordinator()

    coordinator.handle_location_item(_type_2(time=None, mp=3))

    assert coordinator.stats["mowing_percentage"] == 3
    assert coordinator._last_accepted_time_type2 is None


# --------------------------------------------------------------------- #
# 4. coordinator — layer-1 guard on type-1                              #
# --------------------------------------------------------------------- #


def test_first_type_1_is_accepted_and_stamps_the_clock() -> None:
    coordinator = _make_coordinator()

    with patch("custom_components.navimow.coordinator.async_dispatcher_send"):
        coordinator.handle_location_item(_type_1(time=1779521583454))

    assert coordinator.position is not None
    assert coordinator.position["x"] == 1.0
    assert coordinator._last_accepted_time_type1 == 1779521583454


def test_late_delivered_type_1_is_dropped(caplog) -> None:
    coordinator = _make_coordinator()
    coordinator._last_accepted_time_type1 = 2000

    caplog.clear()
    with (
        patch("custom_components.navimow.coordinator.async_dispatcher_send") as disp,
        caplog.at_level("DEBUG", logger="custom_components.navimow.coordinator"),
    ):
        coordinator.handle_location_item(_type_1(time=1000, x="99.0"))

    # Position untouched.
    assert coordinator.position is None
    # Dispatch not called.
    disp.assert_not_called()
    # DEBUG line emitted.
    assert any(
        "type-1 DROPPED as stale" in rec.getMessage() for rec in caplog.records
    ), [rec.getMessage() for rec in caplog.records]


def test_equal_timestamp_type_1_is_dropped() -> None:
    coordinator = _make_coordinator()
    coordinator._last_accepted_time_type1 = 1000

    with patch("custom_components.navimow.coordinator.async_dispatcher_send") as disp:
        coordinator.handle_location_item(_type_1(time=1000, x="99.0"))

    assert coordinator.position is None
    disp.assert_not_called()


def test_type_1_without_time_is_accepted_and_leaves_cursor_alone() -> None:
    coordinator = _make_coordinator()
    coordinator._last_accepted_time_type1 = 1000

    with patch("custom_components.navimow.coordinator.async_dispatcher_send"):
        coordinator.handle_location_item(_type_1(time=None, x="3.0"))

    assert coordinator.position is not None
    assert coordinator.position["x"] == 3.0
    assert coordinator._last_accepted_time_type1 == 1000  # unchanged


# --------------------------------------------------------------------- #
# 5. cursors independence                                               #
# --------------------------------------------------------------------- #


def test_type_1_and_type_2_cursors_are_independent() -> None:
    """The two streams have independent cadences (~2 s vs ~30-90 s). A
    fresh type-1 must not push the type-2 cursor forward, and vice
    versa — otherwise the slower stream would be drop-flooded.
    """
    coordinator = _make_coordinator()

    with patch("custom_components.navimow.coordinator.async_dispatcher_send"):
        coordinator.handle_location_item(_type_1(time=5000))
        coordinator.handle_location_item(_type_2(time=1000, mp=1))

    # Each cursor stamped by its own stream only.
    assert coordinator._last_accepted_time_type1 == 5000
    assert coordinator._last_accepted_time_type2 == 1000
    # Type-2 packet with time=1000 was NOT dropped by the type-1 cursor
    # (would happen with a single shared cursor).
    assert coordinator.stats is not None
    assert coordinator.stats["mowing_percentage"] == 1


def test_type_1_flood_does_not_starve_type_2() -> None:
    """A realistic sequence: type-1 arrives at 2 s cadence, type-2 at
    30-90 s. Interleave them and verify each stream keeps making
    progress under the per-stream guard.
    """
    coordinator = _make_coordinator()

    with patch("custom_components.navimow.coordinator.async_dispatcher_send"):
        # type-1 at 1000, 3000, 5000 (fast cadence)
        coordinator.handle_location_item(_type_1(time=1000))
        coordinator.handle_location_item(_type_1(time=3000))
        coordinator.handle_location_item(_type_1(time=5000))
        # type-2 at 2000, 60000 (slower — some type-2 times fall BEFORE
        # some type-1 times; per-stream cursors let both land)
        coordinator.handle_location_item(_type_2(time=2000, mp=1))
        coordinator.handle_location_item(_type_2(time=60000, mp=2, subtotal="20.0"))

    assert coordinator._last_accepted_time_type1 == 5000
    assert coordinator._last_accepted_time_type2 == 60000
    assert coordinator.stats["mowing_percentage"] == 2
    assert coordinator.position["x"] == 1.0  # last type-1 accepted


# --------------------------------------------------------------------- #
# 6. cursor clamp — future-stamped packets can't poison the guard       #
# --------------------------------------------------------------------- #


def test_future_stamped_type_2_is_accepted_but_cursor_is_clamped() -> None:
    """A packet 24 h in the future must not lock the guard until HA
    restarts. Content-level judgement lives in step (b); the accept-
    but-clamp behaviour in (a) makes the guard self-heal within the
    tolerance window (5 min) of a subsequent legitimate packet.
    """
    from custom_components.navimow.const import FUTURE_TIMESTAMP_TOLERANCE_MS

    coordinator = _make_coordinator()

    future_time = _NOW_MS + 24 * 3600 * 1000  # +24 h
    with patch(
        "custom_components.navimow.coordinator.time.time", return_value=_NOW_MS / 1000
    ):
        coordinator.handle_location_item(_type_2(time=future_time, mp=1))

    # Packet was accepted (stats populated).
    assert coordinator.stats["mowing_percentage"] == 1
    # But the cursor is clamped, not stamped at the future value.
    assert coordinator._last_accepted_time_type2 == (
        _NOW_MS + FUTURE_TIMESTAMP_TOLERANCE_MS
    )


def test_present_time_type_2_is_stamped_unchanged() -> None:
    """The clamp is a ceiling, not a rewriter: a packet whose `time` is
    below the ceiling stamps the cursor at its own value (not the
    ceiling).
    """
    coordinator = _make_coordinator()

    with patch(
        "custom_components.navimow.coordinator.time.time", return_value=_NOW_MS / 1000
    ):
        coordinator.handle_location_item(_type_2(time=_NOW_MS - 10_000, mp=1))

    assert coordinator._last_accepted_time_type2 == _NOW_MS - 10_000


def test_cursor_self_heals_after_future_clamp() -> None:
    """The recovery contract: after a future-stamped packet clamps the
    cursor to `now + 5 min`, a subsequent packet whose `time` exceeds
    that ceiling is accepted and restamps the cursor. No indefinite
    freeze on the stream.
    """
    from custom_components.navimow.const import FUTURE_TIMESTAMP_TOLERANCE_MS

    coordinator = _make_coordinator()

    future_time = _NOW_MS + 24 * 3600 * 1000
    with patch(
        "custom_components.navimow.coordinator.time.time", return_value=_NOW_MS / 1000
    ):
        coordinator.handle_location_item(_type_2(time=future_time, mp=1))

    # Elapse enough wall-clock that a legitimate packet has `time` above
    # the clamped ceiling.
    later = _NOW_MS + FUTURE_TIMESTAMP_TOLERANCE_MS + 60_000
    with patch(
        "custom_components.navimow.coordinator.time.time", return_value=later / 1000
    ):
        coordinator.handle_location_item(_type_2(time=later, mp=2, subtotal="20.0"))

    assert coordinator.stats["mowing_percentage"] == 2
    # Cursor advanced to the healing packet's own value.
    assert coordinator._last_accepted_time_type2 == later


def test_future_stamped_type_1_cursor_is_clamped() -> None:
    from custom_components.navimow.const import FUTURE_TIMESTAMP_TOLERANCE_MS

    coordinator = _make_coordinator()

    future_time = _NOW_MS + 24 * 3600 * 1000
    with (
        patch("custom_components.navimow.coordinator.async_dispatcher_send"),
        patch(
            "custom_components.navimow.coordinator.time.time",
            return_value=_NOW_MS / 1000,
        ),
    ):
        coordinator.handle_location_item(_type_1(time=future_time))

    assert coordinator.position is not None
    assert coordinator._last_accepted_time_type1 == (
        _NOW_MS + FUTURE_TIMESTAMP_TOLERANCE_MS
    )


# --------------------------------------------------------------------- #
# 7. streak WARNING — observability without spam                        #
# --------------------------------------------------------------------- #


def test_streak_warning_fires_at_threshold_type_2(caplog) -> None:
    """Fires exactly once, at the threshold — not earlier, not once per
    subsequent drop.
    """
    from custom_components.navimow.const import STALE_DROP_STREAK_TO_WARN

    coordinator = _make_coordinator()
    # Prime the cursor at 1000 so all subsequent time<=1000 packets drop.
    coordinator._last_accepted_time_type2 = 1000

    caplog.clear()
    with caplog.at_level("WARNING", logger="custom_components.navimow.coordinator"):
        for _ in range(STALE_DROP_STREAK_TO_WARN - 1):
            coordinator.handle_location_item(_type_2(time=500, mp=1))
        # N-1 drops → no WARNING yet.
        assert not any("dropped" in r.getMessage() for r in caplog.records), [
            r.getMessage() for r in caplog.records
        ]

        coordinator.handle_location_item(_type_2(time=500, mp=1))  # Nth drop

    warns = [
        r
        for r in caplog.records
        if r.levelname == "WARNING" and "type-2" in r.getMessage()
    ]
    assert len(warns) == 1, [r.getMessage() for r in warns]
    assert coordinator._type2_drop_streak == STALE_DROP_STREAK_TO_WARN


def test_streak_warning_does_not_repeat_after_threshold_type_2(caplog) -> None:
    from custom_components.navimow.const import STALE_DROP_STREAK_TO_WARN

    coordinator = _make_coordinator()
    coordinator._last_accepted_time_type2 = 1000

    with caplog.at_level("WARNING", logger="custom_components.navimow.coordinator"):
        # Push past the threshold — additional drops must not re-fire.
        for _ in range(STALE_DROP_STREAK_TO_WARN + 10):
            coordinator.handle_location_item(_type_2(time=500, mp=1))

    warns = [
        r
        for r in caplog.records
        if r.levelname == "WARNING" and "type-2" in r.getMessage()
    ]
    assert len(warns) == 1


def test_streak_resets_on_acceptance_type_2() -> None:
    """A single legitimate packet zeroes the counter, so the next drop
    starts a fresh streak rather than compounding with the old one.
    """
    coordinator = _make_coordinator()
    coordinator._last_accepted_time_type2 = 1000

    for _ in range(10):
        coordinator.handle_location_item(_type_2(time=500, mp=1))
    assert coordinator._type2_drop_streak == 10

    # A fresh packet: streak resets to 0.
    coordinator.handle_location_item(_type_2(time=2000, mp=2, subtotal="20.0"))
    assert coordinator._type2_drop_streak == 0

    coordinator.handle_location_item(_type_2(time=500, mp=1))
    assert coordinator._type2_drop_streak == 1  # not 11


def test_streak_warning_fires_at_threshold_type_1(caplog) -> None:
    from custom_components.navimow.const import STALE_DROP_STREAK_TO_WARN

    coordinator = _make_coordinator()
    coordinator._last_accepted_time_type1 = 1000

    with (
        patch("custom_components.navimow.coordinator.async_dispatcher_send"),
        caplog.at_level("WARNING", logger="custom_components.navimow.coordinator"),
    ):
        for _ in range(STALE_DROP_STREAK_TO_WARN):
            coordinator.handle_location_item(_type_1(time=500))

    warns = [
        r
        for r in caplog.records
        if r.levelname == "WARNING" and "type-1" in r.getMessage()
    ]
    assert len(warns) == 1
    assert coordinator._type1_drop_streak == STALE_DROP_STREAK_TO_WARN


def test_streak_counters_are_independent_across_streams() -> None:
    """Draining type-2 does not zero the type-1 counter and vice versa."""
    coordinator = _make_coordinator()
    coordinator._last_accepted_time_type1 = 1000
    coordinator._last_accepted_time_type2 = 1000

    with patch("custom_components.navimow.coordinator.async_dispatcher_send"):
        # 5 drops on type-1
        for _ in range(5):
            coordinator.handle_location_item(_type_1(time=500))
        # 3 drops on type-2
        for _ in range(3):
            coordinator.handle_location_item(_type_2(time=500, mp=1))

        # Accepting a type-1 must not zero the type-2 counter.
        coordinator.handle_location_item(_type_1(time=2000))

    assert coordinator._type1_drop_streak == 0
    assert coordinator._type2_drop_streak == 3
