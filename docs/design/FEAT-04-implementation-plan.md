# FEAT-04 — Implementation plan (PR breakdown) + PR-1 brief

Companion to [`FEAT-04-zone-registry.md`](./FEAT-04-zone-registry.md). Splits FEAT-04a into small, independently testable PRs, each green on CI (pytest + Black 26.3.1) on its own and behaviour-safe against the existing integration. Fine granularity is deliberate — it keeps each Fable review on a narrow surface.

---

## 1. PR breakdown

| PR | Scope | User-visible? | Depends on | Risk |
| --- | --- | --- | --- | --- |
| 1 | `zone_registry.py` pure class + unit tests | no | — | none |
| 2 | coordinator wiring (fed + rebuilt + discovery signal) | no | 1 | low |
| 3 | entity layer (per-zone + aggregate) + i18n | **yes** | 2 | medium |
| 4 | options flow: naming + removal | yes | 3 | medium |
| 5 | FEAT-04b: posture bbox + `reset_posture_extents` | yes | 3 | tbd |

### PR 1 — `zone_registry.py`: pure class + unit tests
The logic core, zero HA dependency: `ZoneRecord`, `ZoneRegistry`, `ingest_run`, `rebuild`, `forget`, `COMPLETE_PASS_CMP = 9900`, plus `tests/test_zone_registry.py` (design tests 1–9). Nothing in the integration references the file yet → **no behaviour change, no risk**. Ideal opener: Fable reviews the fold in isolation, CI trivially green. Full brief in §3.

### PR 2 — coordinator wiring
Instantiate the registry in the coordinator; call `registry.rebuild(history)` after `_async_restore_store`; call `registry.ingest_run(payload)` in `_forward_run_events` on the `run_finished` branch; dispatch `navimow_zone_discovered_<device_id>` for newly-seen boundaries. Tests: after a `run_finished`, `coordinator.zone_registry.zones` is populated; startup rebuild from a supplied `history`; the signal fires. Still **no entities** → nothing user-visible, but the registry is live and inspectable. A dispatch with no listener is a HA no-op, so safe.

### PR 3 — entity layer + i18n
Per-zone sensors (`_zone_<id>` with `ceil` state + `last_surface_precise` attr, `_duree`, `_derniere_tonte`) and the aggregate `_zones`. Eager creation at `async_setup_entry` for known zones; lazy add via the dispatcher listener. **Must land the aggregate's `translation_key`s here** (`strings.json` + `en.json` + `fr.json`) — the static entity would otherwise ship nameless (PR #50 lesson). Per-zone entities have no name map yet → `#<id>` fallback (correct, temporary). Tests: startup values restored (design test 10), ceil display (test 11), lazy add (test 12). First **visible** PR.

### PR 4 — options flow (naming + removal)
Options flow with the `zones: {boundary_id: {name}}` map + "forget" checkbox. Per-zone `_attr_name` from the map with `#<id>` fallback and an options-update listener to refresh names live. "Forget" → `registry.forget` + `entity_registry.async_remove`. Tests: friendly name resolves; "forget" removes entities (design test 13).

### PR 5 — FEAT-04b (deferred, separate track)
Posture `bbox` + `reset_posture_extents`, with the type-1 ↔ boundary join. Out of the 4a sequence; own cycle later.

### Branch strategy
PRs 1–4 stacked from `deploy`, each merged before the next opens (keeps diffs clean and reviews linear). The design branch (`feat-04-zone-registry-design`, carrying this plan and the design doc) merges first as the reference.

### Variant (operator's call)
PR 2 + PR 3 can be **merged into one** if you prefer every PR to ship something visible, at the cost of a diff mixing coordinator and platform. The fine split above is the recommendation for review clarity.

---

## 2. Conventions for every PR

- Target branch `deploy`; CI must pass (pytest + pre-commit, Black 26.3.1).
- English artifacts; French chat.
- No change outside the PR's stated scope. In particular, `run_tracker.py`, `location.py` and the guard layers are untouched across all of FEAT-04a.
- Type hints throughout; `from __future__ import annotations`.
- Reference `#10` in each PR body; link back to the design doc.

---

## 3. PR-1 implementation brief (for Opus)

**Goal.** Add the pure registry and its unit tests. No integration wiring, no entities, no coordinator change. Two new files only.

**Files.**
- `custom_components/navimow/zone_registry.py` (new)
- `tests/test_zone_registry.py` (new)

**Acceptance criteria.**
1. `tests/test_zone_registry.py` — the 9 tests below — all green.
2. Black 26.3.1 clean; `from __future__ import annotations`; no HA import anywhere in `zone_registry.py`.
3. No change to any other file.
4. `ingest_run` returns first-seen boundary ids (for the later lazy-add wiring); registry stores **precise** surfaces (`round(..., 2)`) — the `math.ceil` presentation lands in PR 3, not here.

### 3.1 `zone_registry.py`

```python
"""Pure, HA-agnostic per-zone registry for FEAT-04.

Folds run_tracker `run_finished` payloads into per-zone records. Holds no
persisted state of its own: the coordinator rebuilds it from `history` at
startup (PR 2). See docs/design/FEAT-04-zone-registry.md.
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass

# A pass whose cmp_max reaches this counts as a "complete" pass of the zone
# and refreshes size_estimate_m2. All 8 completions in the 763-packet
# archive reach cmp = 10000; the threshold sits below 10000 to tolerate a
# peak packet missed by a lossy stream, and far above the ~7000 partial
# ceiling. See design §4 / D6.
COMPLETE_PASS_CMP = 9900


@dataclass
class ZoneRecord:
    boundary_id: int
    last_mowed_ms: int | None = None       # this zone's own last exit time
    last_surface_m2: float | None = None   # Σ(sub_exit − sub_entry), precise
    last_duration_s: int | None = None     # in-zone mowing time, recharge incl.
    last_cmp_max: int = 0                   # completeness of last pass, 0..10000
    size_estimate_m2: float | None = None   # LAST complete pass, precise (m²)
    last_result: str | None = None          # "completed" / "interrupted"
    bbox: dict[str, float] | None = None    # FEAT-04b, unused here


class ZoneRegistry:
    """Per-boundary aggregate, projected from run_finished payloads."""

    def __init__(self) -> None:
        self.zones: dict[int, ZoneRecord] = {}

    def ingest_run(self, rf: dict) -> list[int]:
        """Fold one run_finished payload. Return boundary ids seen for the
        first time (for lazy entity creation in PR 2/3)."""
        result = rf.get("result")
        segments = rf.get("zones") or []

        by_boundary: dict[int, list[dict]] = defaultdict(list)
        for seg in segments:
            bid = seg.get("boundary_id")
            if bid:  # None / 0 (BUG-06 sentinel) excluded
                by_boundary[bid].append(seg)

        newly_seen: list[int] = []
        for bid, segs in by_boundary.items():
            if bid not in self.zones:
                self.zones[bid] = ZoneRecord(boundary_id=bid)
                newly_seen.append(bid)
            rec = self.zones[bid]

            surface = sum(
                s["sub_exit"] - s["sub_entry"]
                for s in segs
                if s.get("sub_exit") is not None and s.get("sub_entry") is not None
            )
            # In-zone mowing time = Σ per-segment spans. An intra-zone
            # recharge does not split the segment, so its pause is included;
            # time spent in OTHER zones between two segments is excluded.
            duration_ms = sum(
                s["last_time"] - s["first_time"]
                for s in segs
                if s.get("last_time") is not None and s.get("first_time") is not None
            )
            cmp_max = max((s.get("cmp_max") or 0) for s in segs)

            # "last mowed" = this boundary's own last exit, NOT the run end.
            seg_last_times = [
                s["last_time"] for s in segs if s.get("last_time") is not None
            ]
            rec.last_mowed_ms = max(seg_last_times) if seg_last_times else None
            rec.last_surface_m2 = round(surface, 2)
            rec.last_duration_s = round(duration_ms / 1000)
            rec.last_cmp_max = cmp_max
            rec.last_result = result
            if cmp_max >= COMPLETE_PASS_CMP:
                rec.size_estimate_m2 = round(surface, 2)  # last complete wins
        return newly_seen

    def rebuild(self, history: list[dict]) -> None:
        """Replay history oldest→newest; last complete pass wins the size
        estimate, so it auto-corrects after an app-side zone reshape."""
        self.zones.clear()
        for rf in history:
            self.ingest_run(rf)

    def forget(self, boundary_id: int) -> bool:
        """Drop a zone's record (options-flow removal, PR 4)."""
        return self.zones.pop(boundary_id, None) is not None
```

### 3.2 `tests/test_zone_registry.py`

Helpers, then the 9 cases. Use `pytest.approx` for surfaces.

```python
from __future__ import annotations

import pytest

from custom_components.navimow.zone_registry import (
    COMPLETE_PASS_CMP,
    ZoneRegistry,
)


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
        end_time = last + 60_000  # dock/return after last zone exit
    return {
        "result": result,
        "start_time": start_time,
        "end_time": end_time,
        "duration_ms": end_time - start_time,
        "session_area": None,
        "mow_start_type": None,
        "zones": zones,
    }


# 1
def test_single_run_two_boundaries():
    reg = ZoneRegistry()
    seen = reg.ingest_run(
        _run([
            _seg(1, 0, 10_000, 10_000, 0.0, 228.0),
            _seg(3, 10_000, 18_000, 10_000, 228.0, 352.0),
        ])
    )
    assert set(seen) == {1, 3}
    assert reg.zones[1].last_surface_m2 == pytest.approx(228.0)
    assert reg.zones[3].last_surface_m2 == pytest.approx(124.0)


# 2 — interleaved [1, 3, 1]: zone 1 sums both its segments; last_mowed pins
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
    # surface = (90-0) + (352-214) = 228
    assert z1.last_surface_m2 == pytest.approx(228.0)
    # duration = (5000-0) + (14000-9000) = 10 s, excludes the zone-3 gap
    assert z1.last_duration_s == 10
    # last_mowed = max last_time of zone 1's segments = 14000, < zone 3's? no:
    # zone 3 exits at 9000, zone 1's final exit 14000 → z1 > z3 here; the
    # invariant is only that neither equals end_time (200000).
    assert z1.last_mowed_ms == 14_000
    assert z3.last_mowed_ms == 9_000
    assert z1.last_mowed_ms != run["end_time"]
    assert z3.last_mowed_ms != run["end_time"]


# 3
def test_intra_zone_recharge_duration_includes_pause():
    reg = ZoneRegistry()
    # single segment spanning a dock: 40 min wall-clock, recharge inside
    reg.ingest_run(_run([_seg(1, 0, 2_400_000, 10_000, 0.0, 228.0)]))
    assert reg.zones[1].last_duration_s == 2_400


# 4
def test_interrupted_pass_keeps_prior_size_estimate():
    reg = ZoneRegistry()
    reg.ingest_run(_run([_seg(1, 0, 10_000, 10_000, 0.0, 228.0)]))
    reg.ingest_run(_run([_seg(1, 0, 6_000, 6_000, 0.0, 140.0)], result="interrupted"))
    z1 = reg.zones[1]
    assert z1.size_estimate_m2 == pytest.approx(228.0)  # unchanged
    assert z1.last_surface_m2 == pytest.approx(140.0)   # partial
    assert z1.last_cmp_max == 6_000


# 5
def test_resize_auto_correction_last_wins():
    reg = ZoneRegistry()
    reg.ingest_run(_run([_seg(1, 0, 10_000, 10_000, 0.0, 200.0)]))
    reg.ingest_run(_run([_seg(1, 0, 10_000, 10_000, 0.0, 150.0)]))
    assert reg.zones[1].size_estimate_m2 == pytest.approx(150.0)  # not max


# 6
@pytest.mark.parametrize(
    "cmp_max, updated", [(9_850, False), (COMPLETE_PASS_CMP, True), (10_000, True)]
)
def test_threshold_gate_9900(cmp_max, updated):
    reg = ZoneRegistry()
    reg.ingest_run(_run([_seg(1, 0, 10_000, cmp_max, 0.0, 180.0)]))
    if updated:
        assert reg.zones[1].size_estimate_m2 == pytest.approx(180.0)
    else:
        assert reg.zones[1].size_estimate_m2 is None


# 7
def test_boundary_zero_and_missing_excluded():
    reg = ZoneRegistry()
    seen = reg.ingest_run(
        _run([
            _seg(0, 0, 1_000, 0, 0.0, 0.0),          # sentinel
            _seg(1, 1_000, 10_000, 10_000, 0.0, 228.0),
        ])
    )
    assert seen == [1]
    assert 0 not in reg.zones


# 8
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


# 9
def test_forget():
    reg = ZoneRegistry()
    reg.ingest_run(_run([_seg(1, 0, 10_000, 10_000, 0.0, 228.0)]))
    assert reg.forget(1) is True
    assert 1 not in reg.zones
    assert reg.forget(99) is False
```

**Note for the coder.** Test 2's comment corrects a stale assumption from the design's test list: in a `[1, 3, 1]` interleave, zone 1's last exit (14000) is *later* than zone 3's (9000) — the durable invariant is that neither zone's `last_mowed_ms` equals the run's `end_time`. The plain non-interleaved `1 → 3` case (zone 1 before zone 3, no return) is the one where `z1.last_mowed < z3.last_mowed`; add it as a second assertion block if you want both covered.

---

## 4. Refs

- Design: `docs/design/FEAT-04-zone-registry.md` (data model §4, fold §5, entities §6, tests §13, decisions §12).
- Tracker contract: `run_tracker.py` (`run_finished` payload, `_update_zone`, `_close_run`).
- CI: `pyproject.toml`, `.pre-commit-config.yaml` (Black 26.3.1), `requirements-test.txt`.
