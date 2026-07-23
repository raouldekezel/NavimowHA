"""BUG-12 — ``current_zone`` collapses to ``None`` once the tracker
closes the run, instead of freezing on the last-mowed zone.

Pre-BUG-12 the ``value_fn`` fell back to ``coordinator.stats["boundary"]``
whenever the tracker had no open run. But ``stats`` is never cleared:
the cloud stops emitting type-2 packets at dock (design MQTT §5), so
``stats["boundary"]`` freezes on the last-mowed value and would render
the last zone until the next mow starts.

Fix: no stats fallback. The tracker is the sole source of truth.
"""

from __future__ import annotations

from unittest.mock import MagicMock

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


def _make_coord(*, tracker_state, tracker_zones=None, stats_boundary=None):
    coord = MagicMock()
    coord.run_tracker = MagicMock()
    coord.run_tracker.state = tracker_state
    coord.run_tracker.current_run = (
        {"zones": tracker_zones or []} if tracker_zones is not None else None
    )
    coord.stats = {"boundary": stats_boundary} if stats_boundary is not None else None
    del coord.config_entry
    return coord


# --------------------------------------------------------------------- #
# post-run tracker states → sensor must clear                           #
# --------------------------------------------------------------------- #


def test_completed_run_clears_current_zone_even_with_stale_stats() -> None:
    """Run just closed with ``STATE_COMPLETED``. Cloud has stopped
    emitting type-2 so stats still carries the last zone. Sensor must
    render ``None``, not the frozen stats value."""
    coord = _make_coord(
        tracker_state=STATE_IDLE,
        tracker_zones=None,
        stats_boundary=1,  # frozen from the mow that just closed
    )
    assert _current_boundary(coord) is None
    assert _current_zone_display(coord) is None


def test_interrupted_run_clears_current_zone_even_with_stale_stats() -> None:
    coord = _make_coord(
        tracker_state=STATE_IDLE,
        tracker_zones=None,
        stats_boundary=1,
    )
    assert _current_boundary(coord) is None
    assert _current_zone_display(coord) is None


def test_idle_tracker_clears_current_zone_even_with_stale_stats() -> None:
    coord = _make_coord(
        tracker_state=STATE_IDLE,
        tracker_zones=None,
        stats_boundary=3,
    )
    assert _current_boundary(coord) is None
    assert _current_zone_display(coord) is None


# --------------------------------------------------------------------- #
# active tracker states → sensor still renders                          #
# --------------------------------------------------------------------- #


def test_running_tracker_still_renders_current_zone() -> None:
    """BUG-12 does not regress the happy path: an open run with a
    boundary still renders it."""
    coord = _make_coord(
        tracker_state=STATE_RUNNING,
        tracker_zones=[{"boundary_id": 1}],
    )
    assert _current_boundary(coord) == 1
    assert _current_zone_display(coord) == "#1"


def test_paused_docked_tracker_still_renders_current_zone() -> None:
    """Intra-run recharge pause is also an "active" state — the sensor
    must still show the current boundary (the mow will resume)."""
    coord = _make_coord(
        tracker_state=STATE_PAUSED_DOCKED,
        tracker_zones=[{"boundary_id": 3}],
    )
    assert _current_boundary(coord) == 3
    assert _current_zone_display(coord) == "#3"


# --------------------------------------------------------------------- #
# attrs mirror the same clearing                                        #
# --------------------------------------------------------------------- #


def test_attrs_none_when_run_closed_even_with_stale_stats() -> None:
    """The attribute path used to expose the raw stats boundary for
    debugging. Since the fix removed the stats consultation, the
    attribute also clears — a stale value in attributes would be as
    misleading as one in the state."""
    coord = _make_coord(
        tracker_state=STATE_IDLE,
        tracker_zones=None,
        stats_boundary=1,
    )
    attrs = _desc("current_zone").attrs_fn(coord)
    assert attrs is None
