"""BUG-06 — filter `boundary=0` as the session-init sentinel.

The cloud publishes an all-zero type-2 payload at the very start of a
mow (`currentMowBoundary=0`, `currentMowProgress=0`,
`mowingPercentage=0`, `action=-1`) before the real boundary lands ~60 s
later. The pre-fork local sensor.py explicitly filtered this
(see `raoul/home-assistant@89a6193`); the FEAT-02 port to github lost
the gate. This test file guards the fix and the regression it prevents.

Empirical evidence — same behaviour on both:
- 2026-05-25 07:30:41 UTC (FEAT-02 diag payload, epoch ms `1779694241252`)
- 2026-07-03 07:30:44 UTC (live HA recorder capture on raoul.4)
"""

from __future__ import annotations

from unittest.mock import MagicMock


def _get_desc(key: str):
    from custom_components.navimow.sensor import SENSOR_DESCRIPTIONS

    return next(d for d in SENSOR_DESCRIPTIONS if d.key == key)


def _make_coordinator(stats):
    coordinator = MagicMock()
    coordinator.stats = stats
    # HARD-11: current_zone now reads coordinator.config_entry.options
    # when a name mapping exists. Clear the auto-generated MagicMock
    # attribute so the helper falls back to the raw `#<id>` path this
    # file is pinning.
    del coordinator.config_entry
    return coordinator


# --------------------------------------------------------------------- #
# value_fn                                                              #
# --------------------------------------------------------------------- #


def test_current_zone_real_boundary_renders() -> None:
    """Regression guard: a real positive boundary must render as `#N`."""
    desc = _get_desc("current_zone")
    coordinator = _make_coordinator({"boundary": 3})

    assert desc.value_fn(coordinator) == "#3"


def test_current_zone_zero_boundary_is_filtered() -> None:
    """The fix: `boundary=0` is the session-init sentinel — the sensor
    must report `None` (rendered as HA `unknown`), not `#0`.
    """
    desc = _get_desc("current_zone")
    coordinator = _make_coordinator({"boundary": 0})

    assert desc.value_fn(coordinator) is None


def test_current_zone_none_boundary_stays_none() -> None:
    """Unchanged behaviour: missing boundary still maps to `None`."""
    desc = _get_desc("current_zone")
    coordinator = _make_coordinator({"boundary": None})

    assert desc.value_fn(coordinator) is None


def test_current_zone_no_stats_stays_none() -> None:
    """Unchanged behaviour: no stats at all still maps to `None`."""
    desc = _get_desc("current_zone")
    coordinator = _make_coordinator(None)

    assert desc.value_fn(coordinator) is None


def test_current_zone_boundary_one_still_renders() -> None:
    """Regression guard: `#1` (Zone prunier in the operator's map) must
    survive the falsy filter — `bool(1) is True`.
    """
    desc = _get_desc("current_zone")
    coordinator = _make_coordinator({"boundary": 1})

    assert desc.value_fn(coordinator) == "#1"


# --------------------------------------------------------------------- #
# attrs_fn — unchanged                                                  #
# --------------------------------------------------------------------- #


def test_current_zone_attrs_expose_raw_zero_for_debugging() -> None:
    """`attrs_fn` must keep exposing the raw numeric boundary even when
    value_fn filters it — the developer-tools view remains useful for
    diagnosing the session-init sentinel.
    """
    desc = _get_desc("current_zone")
    coordinator = _make_coordinator({"boundary": 0})

    assert desc.attrs_fn(coordinator) == {"boundary_id": 0}


def test_current_zone_attrs_none_when_no_stats() -> None:
    """Unchanged: no stats → no attrs."""
    desc = _get_desc("current_zone")
    coordinator = _make_coordinator(None)

    assert desc.attrs_fn(coordinator) is None
