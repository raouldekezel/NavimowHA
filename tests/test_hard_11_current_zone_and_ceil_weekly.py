"""HARD-11 — current_zone name resolution + weekly_area ceil.

- ``sensor.<slug>_current_zone`` (BUG-06 sensor) resolves the operator's
  chosen name via ``config_entry.options["zones"]`` (falls back to
  ``#<id>``), so a mow reports "Prunier" instead of "#1" on the
  operator's install.
- ``sensor.<slug>_weekly_area`` ``ceil``'s the cumulative surface for
  parity with the FEAT-04 rounding convention.
"""

from __future__ import annotations

from unittest.mock import MagicMock

from custom_components.navimow.const import OPTIONS_KEY_ZONES
from custom_components.navimow.run_tracker import STATE_IDLE, STATE_RUNNING
from custom_components.navimow.sensor import SENSOR_DESCRIPTIONS, _current_zone_display


def _desc(key: str):
    return next(d for d in SENSOR_DESCRIPTIONS if d.key == key)


def _make_coord(*, boundary=None, area_week=None, options=None):
    """Coordinator mock.

    ``boundary`` seeds the tracker's current run (BUG-12: no more stats
    fallback — the tracker is the sole source of ``current_zone``).
    ``area_week`` seeds ``coordinator.stats`` for the ``weekly_area``
    branch, which still legitimately reads from stats (HARD-02
    restored counter).
    """
    coord = MagicMock()
    coord.run_tracker = MagicMock()
    if boundary is None:
        coord.run_tracker.state = STATE_IDLE
        coord.run_tracker.current_run = None
    else:
        coord.run_tracker.state = STATE_RUNNING
        coord.run_tracker.current_run = {"zones": [{"boundary_id": boundary}]}
    if area_week is None:
        coord.stats = None
    else:
        coord.stats = {"area_week": area_week}
    if options is not None:
        entry = MagicMock()
        entry.options = options
        coord.config_entry = entry
    else:
        # Simulate a coordinator without the attribute (pre-setup or test seam).
        del coord.config_entry
    return coord


# --------------------------------------------------------------------- #
# 1. current_zone — name resolution                                     #
# --------------------------------------------------------------------- #


def test_current_zone_resolves_operator_name() -> None:
    coord = _make_coord(
        boundary=1,
        options={OPTIONS_KEY_ZONES: {"1": {"name": "Prunier"}}},
    )
    assert _current_zone_display(coord) == "Prunier"


def test_current_zone_falls_back_to_hash_id_for_unmapped_boundary() -> None:
    """Boundary #2 (transit corridor) has no operator rename → stays
    ``#2``. This is the same fallback the per-zone entities use."""
    coord = _make_coord(
        boundary=2,
        options={OPTIONS_KEY_ZONES: {"1": {"name": "Prunier"}}},
    )
    assert _current_zone_display(coord) == "#2"


def test_current_zone_falls_back_to_hash_id_when_no_options_at_all() -> None:
    coord = _make_coord(boundary=1, options={})
    assert _current_zone_display(coord) == "#1"


def test_current_zone_returns_none_for_sentinel_boundary_zero() -> None:
    """BUG-06 session-init sentinel: ``boundary=0`` → ``unknown``."""
    coord = _make_coord(
        boundary=0,
        options={OPTIONS_KEY_ZONES: {"1": {"name": "Prunier"}}},
    )
    assert _current_zone_display(coord) is None


def test_current_zone_returns_none_when_stats_is_none() -> None:
    coord = _make_coord(options={})
    assert _current_zone_display(coord) is None


def test_current_zone_survives_missing_config_entry_attr() -> None:
    """Test seam: a coordinator built without HA setup lacks
    ``config_entry`` — the entity must still render (fall back to
    ``#<id>``), not raise."""
    coord = _make_coord(boundary=3)  # no options → no config_entry stashed
    assert _current_zone_display(coord) == "#3"


def test_current_zone_description_value_fn_uses_helper() -> None:
    """Pin the entity description contract: the raw ``value_fn`` on
    ``current_zone`` delegates to ``_current_zone_display``."""
    coord = _make_coord(
        boundary=1,
        options={OPTIONS_KEY_ZONES: {"1": {"name": "Prunier"}}},
    )
    assert _desc("current_zone").value_fn(coord) == "Prunier"


# --------------------------------------------------------------------- #
# 2. weekly_area — ceil                                                 #
# --------------------------------------------------------------------- #


def test_weekly_area_ceils_precise_value() -> None:
    coord = _make_coord(area_week=477.31)
    assert _desc("weekly_area").value_fn(coord) == 478


def test_weekly_area_ceils_integer_value_unchanged() -> None:
    coord = _make_coord(area_week=200.0)
    assert _desc("weekly_area").value_fn(coord) == 200


def test_weekly_area_ceils_tiny_fraction() -> None:
    """0.01 must ceil to 1 — never truncate downward."""
    coord = _make_coord(area_week=0.01)
    assert _desc("weekly_area").value_fn(coord) == 1


def test_weekly_area_none_passes_through() -> None:
    """No type-2 yet → ``None`` (renders as HA `unknown`), not 0."""
    coord = _make_coord(area_week=None)
    coord.stats = {"area_week": None}  # explicit None, not missing
    assert _desc("weekly_area").value_fn(coord) is None


def test_weekly_area_missing_key_passes_through() -> None:
    coord = MagicMock()
    coord.stats = {}  # no area_week key at all
    assert _desc("weekly_area").value_fn(coord) is None


def test_weekly_area_stats_none_passes_through() -> None:
    coord = MagicMock()
    coord.stats = None
    assert _desc("weekly_area").value_fn(coord) is None
