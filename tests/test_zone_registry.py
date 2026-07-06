"""Unit tests for the pure zone registry (FEAT-04, PR 1)."""

from __future__ import annotations

import pytest

from custom_components.navimow.zone_registry import COMPLETE_PASS_CMP, ZoneRegistry


def _seg(boundary_id, first_time, last_time, cmp_max, sub_entry, sub_exit):
    return {
        "boundary_id": boundary_id,
        "first_time": first_time,
        "last_time": last_time,
        "cmp_max": cmp_max,
        "sub_entry": sub_entry,
        "sub_exit": sub_exit,
    }


def _run(zones, *, result="completed", start_time=1_000, end_time=None):
    if end_time is None:
        last = max((s["last_time"] for s in zones), default=start_time)
        end_time = last + 60_000  # dock / return after the last zone exit
    return {
        "result": result,
        "start_time": start_time,
        "end_time": end_time,
        "duration_ms": end_time - start_time,
        "session_area": None,
        "mow_start_type": None,
        "zones": zones,
    }


def test_single_run_two_boundaries():
    reg = ZoneRegistry()
    seen = reg.ingest_run(
        _run(
            [
                _seg(1, 0, 10_000, 10_000, 0.0, 228.0),
                _seg(3, 10_000, 18_000, 10_000, 228.0, 352.0),
            ]
        )
    )
    assert set(seen) == {1, 3}
    assert reg.zones[1].last_surface_m2 == pytest.approx(228.0)
    assert reg.zones[3].last_surface_m2 == pytest.approx(124.0)


def test_interleaved_segments_sum_and_last_mowed():
    reg = ZoneRegistry()
    run = _run(
        [
            _seg(1, 0, 5_000, 4_000, 0.0, 90.0),
            _seg(3, 5_000, 9_000, 10_000, 90.0, 214.0),
            _seg(1, 9_000, 14_000, 10_000, 214.0, 352.0),
        ],
        end_time=200_000,
    )
    reg.ingest_run(run)
    z1, z3 = reg.zones[1], reg.zones[3]
    # surface = (90 - 0) + (352 - 214) = 228
    assert z1.last_surface_m2 == pytest.approx(228.0)
    # duration = (5000 - 0) + (14000 - 9000) = 10 s, excludes the zone-3 gap
    assert z1.last_duration_s == 10
    # last_mowed = max last_time of this zone's own segments
    assert z1.last_mowed_ms == 14_000
    assert z3.last_mowed_ms == 9_000
    # durable invariant: neither equals the run's end_time
    assert z1.last_mowed_ms != run["end_time"]
    assert z3.last_mowed_ms != run["end_time"]


def test_plain_sequential_run_last_mowed_ordering():
    reg = ZoneRegistry()
    run = _run(
        [
            _seg(1, 0, 6_000, 10_000, 0.0, 228.0),
            _seg(3, 6_000, 12_000, 10_000, 228.0, 352.0),
        ],
        end_time=200_000,
    )
    reg.ingest_run(run)
    # zone 1 mowed strictly before zone 3, no return
    assert reg.zones[1].last_mowed_ms < reg.zones[3].last_mowed_ms
    assert reg.zones[3].last_mowed_ms != run["end_time"]


def test_intra_zone_recharge_duration_includes_pause():
    reg = ZoneRegistry()
    # single segment spanning a dock: 40 min wall-clock, recharge inside
    reg.ingest_run(_run([_seg(1, 0, 2_400_000, 10_000, 0.0, 228.0)]))
    assert reg.zones[1].last_duration_s == 2_400


def test_interrupted_pass_keeps_prior_size_estimate():
    reg = ZoneRegistry()
    reg.ingest_run(_run([_seg(1, 0, 10_000, 10_000, 0.0, 228.0)]))
    reg.ingest_run(_run([_seg(1, 0, 6_000, 6_000, 0.0, 140.0)], result="interrupted"))
    z1 = reg.zones[1]
    assert z1.size_estimate_m2 == pytest.approx(228.0)  # unchanged
    assert z1.last_surface_m2 == pytest.approx(140.0)  # partial
    assert z1.last_cmp_max == 6_000


def test_resize_auto_correction_last_wins():
    reg = ZoneRegistry()
    reg.ingest_run(_run([_seg(1, 0, 10_000, 10_000, 0.0, 200.0)]))
    reg.ingest_run(_run([_seg(1, 0, 10_000, 10_000, 0.0, 150.0)]))
    assert reg.zones[1].size_estimate_m2 == pytest.approx(150.0)  # not max


@pytest.mark.parametrize(
    "cmp_max, updated",
    [(9_850, False), (COMPLETE_PASS_CMP, True), (10_000, True)],
)
def test_threshold_gate_9900(cmp_max, updated):
    reg = ZoneRegistry()
    reg.ingest_run(_run([_seg(1, 0, 10_000, cmp_max, 0.0, 180.0)]))
    if updated:
        assert reg.zones[1].size_estimate_m2 == pytest.approx(180.0)
    else:
        assert reg.zones[1].size_estimate_m2 is None


def test_boundary_zero_and_missing_excluded():
    reg = ZoneRegistry()
    seen = reg.ingest_run(
        _run(
            [
                _seg(0, 0, 1_000, 0, 0.0, 0.0),  # sentinel
                _seg(1, 1_000, 10_000, 10_000, 0.0, 228.0),
            ]
        )
    )
    assert seen == [1]
    assert 0 not in reg.zones


def test_rebuild_matches_sequential_ingest():
    history = [
        _run([_seg(1, 0, 10_000, 10_000, 0.0, 200.0)]),
        _run([_seg(1, 0, 10_000, 10_000, 0.0, 227.0)]),  # last complete
        _run([_seg(3, 0, 9_000, 10_000, 0.0, 124.0)]),
    ]
    seq = ZoneRegistry()
    for rf in history:
        seq.ingest_run(rf)
    reb = ZoneRegistry()
    reb.rebuild(history)
    assert reb.zones.keys() == seq.zones.keys()
    assert reb.zones[1].size_estimate_m2 == pytest.approx(227.0)  # last, not 200
    assert reb.zones[3].size_estimate_m2 == pytest.approx(124.0)


def test_forget():
    reg = ZoneRegistry()
    reg.ingest_run(_run([_seg(1, 0, 10_000, 10_000, 0.0, 228.0)]))
    assert reg.forget(1) is True
    assert 1 not in reg.zones
    assert reg.forget(99) is False
