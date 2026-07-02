# FEAT-02 — real multizone run, /location type 2 semantics

## TL;DR

`currentMowProgress / 100` is the **current-zone** progression, not the
run's. `mowingPercentage` is the run's progression. `subtotalArea` is
**cumulative across the whole run**, so per-zone area must be computed
as the delta of `subtotalArea` between boundary changes. `boundary_id`
is not sequential (this jardin observes `1 = zone 1`, `2 = tunnel/transit
never mowed`, `3 = zone 2`).

## Context

- Date: 2026-05-25 15:00–15:50 CEST
- Robot: Segway Navimow i210 LiDAR Pro (`REDACTED-ROBOT-SERIAL`)
- Integration: local build of `NavimowHA v1.1.0 + BUG-01/02/03/04 +
  FEAT-01`. Type-2 payloads had never been observed in a real mowing
  run before this session.
- Session: full multizone mow (zone 1 → tunnel transit → zone 2),
  including an intermediate dock-and-charge (~20 min) inside zone 1.

## Actions taken

1. `01_multizone-run-type-2-payloads.mqtt.log` — 30-line slice of the
   type-2 packets received during the run.

## Timeline (approximate, local CEST)

| Time  | boundary | mowing% | current% | subtotalArea | weekArea |
| ----- | -------- | ------- | -------- | ------------ | -------- |
| 15:01 |        1 | 39 %    | 60.12 %  | 140.41 m²    | 264.18 m² |
| 15:22 |        1 | 50 %    | (?)      | 180 m²       | 305 m²   |
| 15:38 |        1 | 60 %    | (?)      | 214 m²       | 338 m²   |
| 15:44 |        3 | 65 %    | 20 %     | 250 m²       | 375 m²   |

(Values approximate, from the deploy-time log slice; the exact
progression per tick is in `01_multizone-run-type-2-payloads.mqtt.log`.)

## Findings

- **`mowingPercentage` = run progression** — monotonically increasing
  end-to-end (39 → 50 → 60 → 65 → … → 100 by session end).
- **`currentMowProgress / 100` = zone-scoped progression** — resets on
  boundary change. In the committed log, at 15:48:37 UTC (last payload
  before crossing) `boundary=1, currentMowProgress=9901` (99.01 % of
  zone 1). Next type-2 at 15:51:40 UTC: `boundary=3, currentMowProgress=0`
  (fresh start on zone 2). By 15:56:10 the zone-2 progress climbed to
  `cmp=127` (1.27 %), monotonically. Confirms the interpretation that
  led to the design revision documented in the local Journal.md.
- **`subtotalArea` = run total surface**, NOT per-zone. Zone 1 area at
  the boundary crossing = 250 − 140 = ~110 m² if we compute the delta
  between the first and last `subtotalArea` for boundary=1. Aggregate
  weekly = `mowingWeekArea` is a distinct field, tracked over the ISO
  week and increments across the run.
- **Boundary numbering**: id `1 = zone 1` (prunier, first zone mowed),
  id `2 = tunnel` (transit, never appears in a type-2 payload with
  positive progression), id `3 = zone 2` (figuier). Sequential in
  creation-order, not in "physical" zone order.
- **Idle transition artifact**: at 13:38:59 UTC during a mid-run dock-
  and-charge, one transient type-2 packet arrived with
  `mowingPercentage=0`, `currentMowProgress=16`, `subtotalArea=0.39`,
  `action=8`. Neighboring packets in the same window (13:37:25 UTC
  before, 15:35:15 UTC after resumption) hold `mowingPercentage=58`
  and `subtotalArea≈209 m²` — the artefact is a single-packet dip,
  not a real restart. FEAT-02 exposes the raw fields as-is; the
  deferred FEAT-04 zone registry must reject this shape.
- **Post-run persistence**: after the run ended (dock, in charge, no
  more type-2 for ~30 min), the last-seen values remained in
  `coordinator.stats`. Confirms the design decision to cache stats
  across ticks: dropping them would show "unknown" in HA during
  charge, which is neither useful nor accurate.

## Open questions

- **Sub-state `action` field**: observed values `-1`, `5`, `8` during
  mowing; `-1` at idle. Semantic mapping not established. Kept as an
  attribute of the `progression` sensor for downstream investigation.
- **Zone-scoped area computation**: FEAT-02 does NOT expose per-zone
  area — deferred to FEAT-04 (zone registry) so that the filtering
  logic for idle transients can be paired with the persistence design.
- **`mapWorkPosition`**: a 128-char hex blob observed in every type-2
  packet, apparently a bitmap of mown tiles. Not decoded. Would enable
  a coverage overlay on the Lovelace map — deferred.

## Refs

- Local doc: `Home Assistant - Navimow - Journal.md` § 2026-05-25 —
  "Tonte COMPLÈTE multizone (Phase 2.5) : analyse logs"
- Local doc: `Home Assistant - Navimow - Design.md` § 2.3 Phase 2 —
  motivation for the FEAT-04 zone registry (still deferred).
- Related sibling FEAT-01 session for the type-1 half of the same
  channel: [2026-05-23_feat-01_phase1-deployment](../2026-05-23_feat-01_phase1-deployment/findings.md).
