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

from custom_components.navimow.run_tracker import STATE_IDLE, STATE_RUNNING


def _get_desc(key: str):
    from custom_components.navimow.sensor import SENSOR_DESCRIPTIONS

    return next(d for d in SENSOR_DESCRIPTIONS if d.key == key)


def _make_coordinator(*, tracker_boundary=None, stats_boundary=None):
    """Coordinator mock.

    BUG-12: ``current_zone`` reads exclusively from the tracker; the
    ``stats`` fallback is retired. The ``stats_boundary`` kwarg still
    exists for BUG-06 sentinel-in-attrs regression checks — since the
    sentinel arrives on ``coordinator.stats`` from the first type-2
    packet, its BUG-06 filter belonged to the tracker's own
    ``_update_zone`` (never appends ``boundary=0`` to
    ``current_run.zones``), and any user-facing filter now happens
    naturally because ``tracker_boundary=None`` yields ``None`` on
    ``value_fn``.
    """
    coordinator = MagicMock()
    coordinator.run_tracker = MagicMock()
    if tracker_boundary is None:
        coordinator.run_tracker.state = STATE_IDLE
        coordinator.run_tracker.current_run = None
    else:
        coordinator.run_tracker.state = STATE_RUNNING
        coordinator.run_tracker.current_run = {
            "zones": [{"boundary_id": tracker_boundary}]
        }
    coordinator.stats = (
        {"boundary": stats_boundary} if stats_boundary is not None else None
    )
    # HARD-11: current_zone reads config_entry.options for names. Clear
    # the auto-generated MagicMock attribute so the helper falls back
    # to the raw `#<id>` path this file is pinning.
    del coordinator.config_entry
    return coordinator


# --------------------------------------------------------------------- #
# value_fn                                                              #
# --------------------------------------------------------------------- #


def test_current_zone_real_boundary_renders() -> None:
    """Regression guard: a real positive boundary must render as `#N`."""
    desc = _get_desc("current_zone")
    coordinator = _make_coordinator(tracker_boundary=3)

    assert desc.value_fn(coordinator) == "#3"


def test_current_zone_zero_boundary_is_filtered() -> None:
    """The fix: `boundary=0` is the session-init sentinel — the sensor
    must report `None` (rendered as HA `unknown`), not `#0`.

    BUG-12: the tracker's own ``_update_zone`` never appends
    ``boundary=0`` to ``current_run.zones``, so simulating "sentinel
    seen" means "no zone in the tracker's current run" here — same
    effect on ``value_fn``.
    """
    desc = _get_desc("current_zone")
    coordinator = _make_coordinator(tracker_boundary=None)

    assert desc.value_fn(coordinator) is None


def test_current_zone_no_source_stays_none() -> None:
    """Idle tracker → ``None``. BUG-12: no stats fallback."""
    desc = _get_desc("current_zone")
    coordinator = _make_coordinator()

    assert desc.value_fn(coordinator) is None


def test_current_zone_boundary_one_still_renders() -> None:
    """Regression guard: `#1` (Zone prunier in the operator's map) must
    survive the falsy filter — `bool(1) is True`.
    """
    desc = _get_desc("current_zone")
    coordinator = _make_coordinator(tracker_boundary=1)

    assert desc.value_fn(coordinator) == "#1"


# --------------------------------------------------------------------- #
# attrs_fn                                                              #
# --------------------------------------------------------------------- #


def test_current_zone_attrs_expose_boundary_from_tracker() -> None:
    """``attrs_fn`` mirrors ``value_fn``: same boundary, exposed as
    ``boundary_id`` for developer-tools inspection."""
    desc = _get_desc("current_zone")
    coordinator = _make_coordinator(tracker_boundary=3)

    assert desc.attrs_fn(coordinator) == {"boundary_id": 3}


def test_current_zone_attrs_none_when_no_source() -> None:
    """Idle tracker + no stats → no attrs. BUG-12: no stats fallback."""
    desc = _get_desc("current_zone")
    coordinator = _make_coordinator()

    assert desc.attrs_fn(coordinator) is None
