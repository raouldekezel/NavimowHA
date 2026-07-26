"""Pure, HA-agnostic per-zone registry.

Folds run_tracker ``run_finished`` payloads into per-zone records. Holds no
persisted state of its own: the coordinator rebuilds it from ``history`` at
startup. See docs/design/FEAT-04-zone-registry.md.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass

# A pass whose cmp_max reaches this counts as a "complete" pass of the zone
# and refreshes size_estimate_m2. All 8 completions in the 763-packet archive
# reach cmp = 10000; the threshold sits below 10000 to tolerate a peak packet
# missed by a lossy stream, and far above the ~7000 partial ceiling.
COMPLETE_PASS_CMP = 9900


@dataclass
class ZoneRecord:
    """Per-boundary aggregate held in memory."""

    boundary_id: int
    last_mowed_ms: int | None = None
    last_surface_m2: float | None = None
    last_duration_s: int | None = None
    last_cmp_max: int = 0
    size_estimate_m2: float | None = None
    # Start-time of the visit that most recently refreshed
    # `size_estimate_m2`. Paired with the estimate so the operator can tell
    # when the zone-size figure was last calibrated — useful after an
    # app-side reshape, where the estimate auto-corrects on the next complete
    # pass and this stamp says which pass won. Start of the visit, not its
    # exit, matching `last_mowed_ms`.
    size_estimate_updated_ms: int | None = None
    last_result: str | None = None
    bbox: dict[str, float] | None = None  # reserved for posture-bbox, unused today


class ZoneRegistry:
    """Per-boundary registry, projected from run_finished payloads."""

    def __init__(self) -> None:
        self.zones: dict[int, ZoneRecord] = {}

    def ingest_run(self, rf: dict) -> list[int]:
        """Fold one run_finished payload.

        Returns the boundary ids seen for the first time, for lazy entity
        creation.
        """
        result = rf.get("result")
        segments = rf.get("zones") or []

        by_boundary: dict[int, list[dict]] = defaultdict(list)
        for seg in segments:
            bid = seg.get("boundary_id")
            if bid:  # None / 0 (the session-init sentinel) excluded
                by_boundary[bid].append(seg)

        newly_seen: list[int] = []
        for bid, segs in by_boundary.items():
            # Segments missing sub_entry/sub_exit are skipped defensively; in
            # practice every tracker-emitted segment carries them. When every
            # segment lacks sub the surface collapses to 0.0 rather than None
            # (harmless: real payloads never trigger it, and downstream steps
            # treat 0.0 and None the same for "nothing mowed").
            surface = sum(
                s["sub_exit"] - s["sub_entry"]
                for s in segs
                if s.get("sub_exit") is not None and s.get("sub_entry") is not None
            )
            # In-zone mowing time = sum of per-segment spans. An intra-zone
            # recharge does not split the segment, so its pause is included;
            # time spent in OTHER zones between two segments is excluded.
            duration_ms = sum(
                s["last_time"] - s["first_time"]
                for s in segs
                if s.get("last_time") is not None and s.get("first_time") is not None
            )
            cmp_max = max((s.get("cmp_max") or 0) for s in segs)

            # "Last mowed" is the **start** of this boundary's last visit
            # (min(first_time)), not its exit. It lines up with the other
            # session-start timestamps the operator sees. Taking the run's
            # `end_time` instead would stamp zone 1 with the moment zone 3
            # finished on a 1→3 run; `first_time` is zone-local, so the
            # invariant `last_mowed_ms != end_time` holds.
            seg_first_times = [
                s["first_time"] for s in segs if s.get("first_time") is not None
            ]

            # Preserve a prior ZoneRecord on a fully-degenerate payload: if not
            # one segment carries usable data (no timing, no sub delta, no cmp
            # progress), skip the boundary — never materialise a new record,
            # never wipe an existing one. The tracker never emits such a
            # payload; this hardens the pure module against an out-of-band
            # caller.
            has_area = any(
                s.get("sub_exit") is not None and s.get("sub_entry") is not None
                for s in segs
            )
            if not seg_first_times and not has_area and cmp_max == 0:
                continue

            if bid not in self.zones:
                self.zones[bid] = ZoneRecord(boundary_id=bid)
                newly_seen.append(bid)
            rec = self.zones[bid]

            rec.last_mowed_ms = min(seg_first_times) if seg_first_times else None
            rec.last_surface_m2 = round(surface, 2)
            rec.last_duration_s = round(duration_ms / 1000)
            rec.last_cmp_max = cmp_max
            rec.last_result = result
            if cmp_max >= COMPLETE_PASS_CMP:
                rec.size_estimate_m2 = round(surface, 2)  # last complete wins
                # Stamp the complete pass. `min(seg_first_times)` matches
                # `last_mowed_ms` semantics (start of the visit), and `rebuild`
                # replays history oldest-to-newest, so this too ends on the
                # most recent complete pass.
                rec.size_estimate_updated_ms = (
                    min(seg_first_times) if seg_first_times else None
                )
        return newly_seen

    def rebuild(self, history: list[dict]) -> None:
        """Replay history oldest-to-newest.

        The last complete pass wins the size estimate, so it auto-corrects
        after an app-side zone reshape.
        """
        self.zones.clear()
        for rf in history:
            self.ingest_run(rf)

    def forget(self, boundary_id: int) -> bool:
        """Drop a zone's record."""
        return self.zones.pop(boundary_id, None) is not None
