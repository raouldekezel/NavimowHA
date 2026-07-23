# HARD-21 migration-retirement evidence — registry + Store reads

## TL;DR

The two spent shims removed by HARD-21 (#123) are gated on live artifacts,
not on "a restart happened": the entity registry carries **zero**
old-scheme unique_ids (26 navimow entities, all `current`/`last`/`total`),
and the run_tracker Store re-persisted **`state = "idle"`** after the first
post-raoul.27 mow closed — so `_async_migrate_unique_ids` (item 1) and the
`restore()` dead-vocabulary state mapping (item 2) are both dead code.

## Context

- **Date:** 2026-07-23 (post-raoul.27 upgrade, after one mow closed).
- **Robot:** i210 LiDAR Pro (`razibus`); serial redacted.
- **Integration:** `NavimowHA-v1.1.0-raoul.27` (HARD-20, #124) live via HACS;
  HA 2026.1.3 (Docker `hass`).
- Read-only reads of `/config/.storage/core.entity_registry` and
  `/config/.storage/navimow.<serial>.run_tracker` inside the container.
- Evidence files:
  - `01_entity_registry_navimow_unique_ids.json` — the 26 live unique_ids
    (item 1).
  - `02_run_tracker_store_state_idle.json` — the tracker Store head
    (item 2).

## Actions taken

1. Enumerated every `platform == "navimow"` unique_id in the live entity
   registry and classified each against the five FEAT-08 old-scheme
   patterns (`_run_state`, `_run_progress`, `_zone_progress`, `_zone_<id>`,
   `_zone_<id>_duration`).
2. Read the run_tracker Store head (`state`, counters, current_run
   reference).

## Timeline

The Store `current_run` reference is the last closed run — the mow that
re-persisted the new vocabulary:

| epoch-ms (UTC)          | Event |
| ----------------------- | ----- |
| 1784832111304 (18:41:51) | press → run start_time. |
| 1784832134137 (18:42:14) | first task type-2 (Prunier) seeds the run. |
| 1784832186141 (18:43:06) | last type-2; run closes → **Store saved with `state = "idle"`** (the item-2 evidence). |

## Findings

- **Item 1 — the FEAT-08 unique_id migration is spent.** 26 navimow
  unique_ids, **0** match any old-scheme pattern; every run/zone entity is
  on the `current_*` / `last_*` / `total_*` scheme
  (`01_entity_registry_navimow_unique_ids.json`). `_async_migrate_unique_ids`
  has been an idempotent no-op for dozens of restarts — removed.
- **Item 2 — the HARD-20 restore() state mapping is spent.** The on-disk
  `tracker.state` is **`"idle"`** (`02_run_tracker_store_state_idle.json`),
  the current vocabulary — no `"completed"`/`"interrupted"` string remains
  on disk. The raoul.27 dead-vocabulary shim (which translated those
  strings in memory on restore) had nothing left to translate; removed.
  Counters carried across intact (`aborted_starts_committed = 1`,
  `wk_regressions_observed = 2`, `invariant_deviations_observed = 1`,
  `strict_progress_rejections = 0`), confirming the item-4 tolerant reads
  are robustness, not migration.
- **Retirement criterion honoured.** The item-2 evidence is exactly Sol's
  corrected condition (#122 review): a post-upgrade Store save **verified
  on disk** in the current vocabulary — not merely "a restart occurred"
  (`restore()` translates in memory only; saves fire on heartbeat/close,
  never at rest).
- **The greppable invariant holds.** After the removals,
  `grep "MIGRATION("` over the source returns zero — every migration is
  now either gone or, going forward, tagged at birth.

## Open questions

- Item 5 (auth-token `entry.data.get("token")` fallback) is deferred — a
  HA-core compat ladder whose reachability on 2026.1.3 is unverified. A
  one-restart DEBUG capture of the token path would settle whether the
  modern branch always serves it.

## Refs

- #123 (HARD-21) — the sweep this evidence gates; item 1 / item 2 removals.
- #88 (FEAT-08) — origin of the unique_id migration (item 1).
- #122 (HARD-20) / #124 — origin of the restore() state migration (item 2);
  Sol's on-disk-re-persist correction.
- #121 — the 2026-07-23 Store dump (item-4 counter evidence).
