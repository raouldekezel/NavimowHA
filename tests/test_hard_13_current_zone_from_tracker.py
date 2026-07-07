"""HARD-13 — current_zone survives an HA restart mid-mow.

The tracker snapshot is restored from the Store at
``_async_restore_store`` (FEAT-05 c), so ``run_tracker.current_run``
is available immediately after a restart. ``coordinator.stats`` is
not persisted (FEAT-02 design), so ``current_zone`` relying on
``stats["boundary"]`` alone would go ``unknown`` until the next
type-2 packet.

Fix: prefer the tracker's current-run boundary, fall back to stats.
"""

from __future__ import annotations

from unittest.mock import MagicMock

from custom_components.navimow.const import OPTIONS_KEY_ZONES
from custom_components.navimow.run_tracker import (
    STATE_IDLE,
    STATE_PAUSED_DOCKED,
    STATE_RUNNING,
)
from custom_components.navimow.sensor import (
    SENSOR_DESCRIPTIONS,
    _current_boundary,
    _current_zone_display,
)


def _desc(key: str):
    return next(d for d in SENSOR_DESCRIPTIONS if d.key == key)


def _make_coord(
    *,
    tracker_state=STATE_IDLE,
    tracker_zones=None,
    stats=None,
    options=None,
):
    coord = MagicMock()
    coord.run_tracker = MagicMock()
    coord.run_tracker.state = tracker_state
    coord.run_tracker.current_run = (
        {"zones": tracker_zones or []} if tracker_zones is not None else None
    )
    coord.stats = stats
    if options is not None:
        entry = MagicMock()
        entry.options = options
        coord.config_entry = entry
    else:
        del coord.config_entry
    return coord


# --------------------------------------------------------------------- #
# 1. Post-restart mid-mow: tracker source of truth                      #
# --------------------------------------------------------------------- #


def test_current_boundary_reads_from_tracker_when_stats_empty() -> None:
    """Restart scenario: coordinator.stats reset to None, tracker
    restored with an open run on boundary 1."""
    coord = _make_coord(
        tracker_state=STATE_RUNNING,
        tracker_zones=[{"boundary_id": 1}],
        stats=None,
    )
    assert _current_boundary(coord) == 1


def test_current_zone_renders_correctly_after_restart_without_stats() -> None:
    coord = _make_coord(
        tracker_state=STATE_RUNNING,
        tracker_zones=[{"boundary_id": 1}],
        stats=None,
        options={OPTIONS_KEY_ZONES: {"1": {"name": "Prunier"}}},
    )
    assert _current_zone_display(coord) == "Prunier"


def test_current_zone_renders_short_id_after_restart_unmapped() -> None:
    coord = _make_coord(
        tracker_state=STATE_RUNNING,
        tracker_zones=[{"boundary_id": 2}],
        stats=None,
        options={OPTIONS_KEY_ZONES: {"1": {"name": "Prunier"}}},
    )
    assert _current_zone_display(coord) == "#2"


def test_current_boundary_prefers_last_segment_on_interleaved_run() -> None:
    """After a 1→3 transition, tracker.current_run.zones ends on the
    latest segment. That is the boundary the robot is currently on."""
    coord = _make_coord(
        tracker_state=STATE_RUNNING,
        tracker_zones=[{"boundary_id": 1}, {"boundary_id": 3}],
        stats=None,
    )
    assert _current_boundary(coord) == 3


def test_current_boundary_reads_from_tracker_when_paused_docked() -> None:
    """Fable review pin: the fix also covers a restart during an
    intra-run recharge pause (``STATE_PAUSED_DOCKED``), because
    ``_current_run_or_none`` returns the run for both ``RUNNING`` and
    ``PAUSED_DOCKED``. Explicit test so a future change to that
    predicate can't silently regress the recharge-pause path."""
    coord = _make_coord(
        tracker_state=STATE_PAUSED_DOCKED,
        tracker_zones=[{"boundary_id": 1}],
        stats=None,
        options={OPTIONS_KEY_ZONES: {"1": {"name": "Prunier"}}},
    )
    assert _current_boundary(coord) == 1
    assert _current_zone_display(coord) == "Prunier"


# --------------------------------------------------------------------- #
# 2. Legacy path: tracker idle, stats live                              #
# --------------------------------------------------------------------- #


def test_current_boundary_falls_back_to_stats_when_tracker_idle() -> None:
    """No open run → sensor reads live type-2 stats. Matches the
    pre-HARD-13 behaviour when the tracker hasn't yet moved to
    RUNNING (edge case at the very start of a session, before the
    first accepted type-2)."""
    coord = _make_coord(
        tracker_state=STATE_IDLE,
        tracker_zones=None,
        stats={"boundary": 3},
    )
    assert _current_boundary(coord) == 3


def test_current_boundary_falls_back_to_stats_when_run_has_no_zones() -> None:
    coord = _make_coord(
        tracker_state=STATE_RUNNING,
        tracker_zones=[],  # open run but zero segments yet
        stats={"boundary": 3},
    )
    assert _current_boundary(coord) == 3


def test_current_boundary_returns_none_when_nothing_known() -> None:
    coord = _make_coord(tracker_state=STATE_IDLE, tracker_zones=None, stats=None)
    assert _current_boundary(coord) is None


# --------------------------------------------------------------------- #
# 3. BUG-06 sentinel intact                                             #
# --------------------------------------------------------------------- #


def test_bug_06_sentinel_still_filtered_by_display() -> None:
    """boundary=0 (session-init) still collapses to ``None`` on
    ``value_fn`` — the raw sentinel does not leak to the UI."""
    coord = _make_coord(
        tracker_state=STATE_IDLE,
        tracker_zones=None,
        stats={"boundary": 0},
    )
    assert _current_zone_display(coord) is None


def test_bug_06_sentinel_still_exposed_in_attrs_for_debug() -> None:
    """The raw ``0`` still shows in attributes so the sentinel is
    visible in developer-tools even after HARD-13."""
    coord = _make_coord(
        tracker_state=STATE_IDLE,
        tracker_zones=None,
        stats={"boundary": 0},
    )
    attrs = _desc("current_zone").attrs_fn(coord)
    assert attrs == {"boundary_id": 0}
