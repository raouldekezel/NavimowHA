# HARD-18 post-deploy field validation — eager start + aborted start

## TL;DR

On `raoul.26` two manual starts confirm HARD-18 in production: the run
`duration` is now anchored on the **press** (essai 1: 78 s spanning
press → first task packet, i.e. the démarrage is counted, where pre-HARD-18
this run would have read ~0 s), and an **immediate return-to-dock**
commits the arbitrated **minimal aborted-start** entry (essai 2:
`result=interrupted`, `zones=[]`, `session_area=None`, `mow_start_type=None`,
`duration=54.2 s` of real wander), with `counters.aborted_starts_committed`
incrementing to `1`. The display legs of the §6 checklist are
operator-attested (see Findings) — the checklist is complete.

## Context

- **Date:** 2026-07-23, ~15:18–15:46 UTC (local offset `+02:00`).
- **Robot:** i210 LiDAR Pro (`razibus`).
- **Integration:** `NavimowHA-v1.1.0-raoul.26` (HARD-18, #119 squash-merged
  to `deploy` at `bb0180e`), installed via HACS.
- **HA:** 2026.1.3 (Docker `hass`).
- **Pre-state:** tracker restored to a terminal state from Store (the
  operator's dominant post-close entry path); both starts manual
  (`mow_start_type = 1` on the seeded one).
- Evidence: `01_store_after_two_starts.json` — the two `history[]` rows
  plus the tracker `counters` read from the run_tracker Store after both
  runs (serial redacted).

## Actions taken

1. **Essai 1** — pressed RUN, let the robot leave the dock and reach
   Prunier (boundary 1), then interrupted it shortly after the first task
   packet.
2. **Essai 2** — pressed RUN, then immediately sent the robot back to the
   base before any task packet was emitted.

(Both captured in `01_store_after_two_starts.json`.)

## Timeline

Times UTC; `+02:00` local.

**Essai 1 — seeded, then interrupted after a single type-2**

| UTC          | Event |
| ------------ | ----- |
| 15:18:06.536 | RUN pressed → `vs=4`; provisional run opened, `start_time = 15:18:06.536`. |
| 15:18:06 → 15:19:24 | Dock exit + navigation to Prunier (~78 s); **no type-2** yet (run stays provisional, `zones=[]`). |
| 15:19:24.497 | First — and only — task `type-2`: `boundary=1`, `sub=41.28`, `cmp=1701` (17.01 %). Seeds the run: `sub0 = 41.28`, `last_sub = 41.28`. |
| ~15:19:24+   | Interrupted → close `interrupted`. `end_time = 15:19:24.497` (last accepted type-2). `duration_ms = 77 961`, `session_area = 0.0`, `zones=[Prunier]`. |

**Essai 2 — aborted start (immediate return)**

| UTC          | Event |
| ------------ | ----- |
| 15:45:38.919 | RUN pressed → `vs=4`; provisional run opened, `start_time = 15:45:38.919`. |
| 15:45:38 → 15:46:33 | Wander then dock; **no type-2 ever** (run stays provisional, `zones=[]`). |
| 15:46:33.143 | Dock entry stamps `last_time`; sustained-dock timer fires → close `interrupted`. `duration_ms = 54 224`, `session_area = None`, `zones=[]`, `mow_start_type = None`. `counters.aborted_starts_committed → 1`. |

## Findings

- **Eager start anchors duration on the press (essai 1).** `duration_ms
  = 77 961` spans `start_time` (15:18:06, the `vs=4` press) → `end_time`
  (15:19:24, the first task packet). The 78 s **is** the démarrage (dock
  exit + navigation). Pre-HARD-18 this run would have anchored `start_time`
  on the first type-2 and read ~0 s. Confirms §1b/§1c anchoring in prod.
- **Display legs of the §6 checklist: operator-attested (2026-07-23).**
  The operator confirmed observing live, across the two essais:
  `etat_de_la_tonte` rendering **« Démarrage »** within ~2 s of the press,
  the flip to **« En tonte »** at the first task packet (essai 1), and
  **« Retour »** on the return leg. Attested observation, not wire/log
  evidence — recorded per the PR #121 Fable review to close the §6
  checklist; the Store rows in `01_store_after_two_starts.json` carry the
  hard evidence for the tracker side.
- **Single-packet `session_area = 0` is honest, not a regression (essai
  1).** `session_area = last_sub − sub0`; only one task packet arrived, so
  the anchor and the last value are the same `41.28` → `0.0`. The Prunier
  zone shows `sub_entry = sub_exit = 41.28` for the same reason. Measuring
  a *delta* needs ≥2 packets (FEAT-06 semantics, #54); the firmware's
  `cmp=17 %` is a zone-completion state, not an area delta. Interrupting
  before the 2nd packet yields no measurable area.
- **Aborted start commits the arbitrated minimal entry (essai 2).**
  `result=interrupted`, `zones=[]`, `session_area=None`, `mow_start_type=None`,
  `duration_ms=54 224` = real wander (press → dock via the type-1
  `last_time` stamp). Clean history row — no phantom run, no `cmp_max`
  poisoning. Exactly the §1e shape; `aborted_starts_committed` incremented
  to `1`.
- **Return-transit asymmetry is now observable in the field (#120 /
  HARD-19).** Essai 1's `end_time` is the last **type-2** (15:19:24), not
  the later dock arrival — the outbound transit is counted, the return is
  not. Essai 2 (aborted) *is* symmetric (`end_time` = the dock-entry
  type-1). The two rows side by side make the HARD-19 candidate concrete.
- **Counters match the raoul.26 predictions.** `aborted_starts_committed=1`,
  `strict_progress_rejections=0` (no terminal-path refusals yet),
  `invariant_deviations_observed=1` — flat since raoul.26, exactly as the
  §2 review note predicted (sessions now open at `vs=4` with `wk0=None`,
  so the cross-boundary drift shape is re-anchored, not observed).
- **Benign residue: `is_provisional` reads `True` on the closed aborted
  run.** After essai 2's close the Store shows `state=interrupted` with
  `current_run.provisional=True` (the flag is not cleared on close). Every
  consumer gates on `state` first (`_run_state_display`, the docked-ignore
  guard, `tick`, `_current_run_or_none`), so it leaks into no sensor and
  the next `vs=4` rebuilds `current_run` outright. Flagged only so a future
  unguarded read of `is_provisional` is not introduced.

## Open questions

- Should a single-packet interrupted run surface a non-zero area at all
  (the firmware moved `cmp` to 17 %), or is delta-honest `0` the right
  answer? Current view: `0` is honest — one absolute-accumulator reading
  cannot separate this session's contribution from prior tasks.
- HARD-19 (#120): move seeded-run `end_time` to the dock-entry type-1 for
  symmetry with the eager `start_time`? Essai 1 vs essai 2 quantify the
  gap.

## Refs

- #117 (HARD-18) — the feature this session validates; §6 post-deploy
  checklist.
- #119 — the merged PR (`bb0180e`); `NavimowHA-v1.1.0-raoul.26`.
- #120 (HARD-19) — return-transit `end_time` symmetry, made concrete here.
- #54 (FEAT-06) — session-scoped `session_area = last_sub − sub0`.
