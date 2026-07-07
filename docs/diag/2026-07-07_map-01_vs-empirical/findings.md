# MAP-01 empirical correction — `vehicleState` catalog

## TL;DR

Real user pause emits `vs = 3` (`/state = isPaused`), not `vs = 6`. `vs = 6`
is the post-mow map-consolidation phase (`isMapping`). The current catalog
labels `vs = 6 = VS_PAUSED = "explicit user pause"` — empirically wrong.
`VS_PAUSED` renamed to `VS_MAPPING`; the `VS_DOCKED_UNPOWERED = 3` symbol
kept but reframed as a catch-all "stopped" with three observed sub-cases.
Latent BUG-09 concern noted, not fixed here.

## Context

- **Date**: 2026-07-07 14:43 → 14:53 UTC (~10 min)
- **Model**: Navimow i210 LiDAR Pro
- **Integration tag**: `NavimowHA-v1.1.0-raoul.13` (installed via HACS on the
  operator's HA at commit `640b173`)
- **Pre-experiment state**: robot docked, battery 100 %, no charge in progress
- **DEBUG logging**: enabled live via `logger.set_level` on
  `custom_components.navimow` + `mower_sdk` (no persistent
  `configuration.yaml` change); reset to `warning` post-experiment
- **Weather / physical**: no rain, no obstacle

## Actions taken

1. Baseline snapshot of HA entities.
2. Start mow from HA UI (`lawn_mower.start_mowing`).
3. **User pause** from HA UI (bouton pause, mid-mow, off-dock).
4. Resume from HA UI.
5. Return-to-base from HA UI.

## Timeline

Extracted from `docker logs hass --since 15m`. Full slice in
`01_scripted-lifecycle.mqtt.log`.

| Time (UTC)    | `vs` | `/state`       | posture (x, y, θ)      | Event                        |
| ------------- | ---- | -------------- | ---------------------- | ---------------------------- |
| 14:43:19.458  | **1** | —              | (−0.06, 0.04, 3.01)    | Baseline: docked idle, 100 % |
| 14:44:43.178  | **4** | `isRunning`    | (−0.03, 0.05, 3.01)    | ▶ Start mow                  |
| 14:46:39.744  | **3** | **`isPaused`** | (10.98, 22.41, 1.22)   | ⏸ User pause, **off-dock**   |
| 14:49:59.319  | **4** | `isRunning`    | (10.95, 22.43, 1.22)   | ▶ Resume                     |
| 14:50:57.503  | **5** | `isDocking`    | (5.72, 25.75, −1.97)   | 🏠 Return-to-base            |
| 14:52:53.271  | **2** | `isDocked`     | (−0.13, 0.04, 3.03)    | 🔌 Dock arrival, charging    |
| 14:52:55.509  | **3** | `isIdel` (sic) | (−0.12, 0.04, 3.03)    | Transient flip (2.2 s)       |
| 14:52:56.099  | **2** | `isDocked`     | (−0.12, 0.04, 3.03)    | Charging stable              |

Battery held 100 % throughout — the pack was already full, this run drew no
meaningful energy. No `vs = 6` phase entered (would require a real `mp = 99`
task completion followed by post-mow mapping).

## Findings

### F1 — `vs = 6` is `isMapping`, not user pause

The existing catalog labels `VS_PAUSED = 6` with the comment
> vs = 6 (explicit user pause) is excluded so a manual pause still holds the run

Empirically wrong. This capture shows a genuine user pause at 14:46:39.744
emitting `vs = 3` + `/state = isPaused` with posture off-dock (x = 10.98,
y = 22.41). `vs = 6` was **not** produced by any user action.

Prior observation on 2026-05-23 (`it-documentation/Home Assistant - Navimow
- MQTT.md §5` — operator's own note) correlates `vs = 6` with
`/state = isMapping`: post-mow map consolidation, robot at-dock and
immobile. That is a firmware phase, not a user-facing pause.

`vs = 6` was **not observed today** (short run, battery already full → no
`mp = 99` completion → no isMapping phase entered). The `vs = 6` label
leans on the 2026-05-23 correlation for indirect evidence; a dedicated
capture of a real end-of-task dock is a follow-up.

### F2 — `vs = 3` is a catch-all, not "docked unpowered"

Three observed sub-cases:

| Sub-case                          | Posture            | `/state`   | Origin                          |
| --------------------------------- | ------------------ | ---------- | ------------------------------- |
| **User pause off-dock** (new)     | (10.98, 22.41)     | `isPaused` | 14:46:39 today, mid-mow         |
| Transient at-dock idle flip       | (−0.12, 0.04)      | `isIdel`   | 14:52:55 today, between two `vs = 2` samples |
| Docked, base unpowered            | (~0, ~0)           | `isDocked` | 2026-05-23 journal              |

Common denominator: "not mowing, not returning, not charging, not mapping".
The `/state` channel discriminates sub-cases; posture confirms on-dock vs
off-dock.

### F3 — Confirmations (no change needed)

| `vs` | Semantic | Status |
| ---- | -------- | ------ |
| 1    | docked idle, battery full, no charge | Confirmed (baseline) |
| 2    | docked, charging | Confirmed (dock arrival) |
| 4    | mowing | Confirmed (2 occurrences) |
| 5    | returning / docking | Confirmed |
| 8    | firmware-reset transient (posture all-zero) | Prior evidence, unchanged |

## Open questions

1. **Empirical capture of `vs = 6`**: today's run ended with battery full so
   no real completion phase happened. A future end-of-task dock (task
   reaches `mp ≥ 99`, robot returns, sits immobile at dock for a few minutes)
   would seal the `vs = 6 = isMapping` correlation directly rather than
   through the 2026-05-23 journal correlation.

2. **BUG-09 latent concern** — `DOCKED_NOT_USER_PAUSED = {1, 2, 3}` in
   `run_tracker.py` gates completion. Because `vs = 3` includes user pause
   off-dock, a user pause at `mp = 99` far from the dock would trigger a
   `completed` close. Not observed in the wild (narrow window), but the
   guard's stated reasoning ("vs = 6 is explicit user pause, so we exclude
   it") is based on a false premise. Candidate refinement: tighten the
   check to "on-dock" (posture near origin) rather than "vs ∈ {1, 2, 3}".
   **Deferred** — needs operator triage before opening a follow-up issue.

3. **Uncatalogued transient flip vs = 2 → 3 → 2 in 2.2 s** at 14:52:55.
   Correlates with `/state = isIdel`. Ignorable by tracker (already
   handled by `DOCKED_STATES` inclusion), but the mechanism is unexplained
   — possibly a firmware momentary charge-current dropout during initial
   dock contact. Not worth chasing unless it recurs.

## Refs

- Issue: [#25 MAP-01](https://github.com/raouldekezel/NavimowHA/issues/25)
- Prior sessions:
  - [2026-05-23_map-01_vehiclestate-catalog](../2026-05-23_map-01_vehiclestate-catalog/findings.md)
  - [2026-05-23_spike-01_errors-invisible-mqtt](../2026-05-23_spike-01_errors-invisible-mqtt/findings.md)
- Downstream doc (operator's private notes, out of repo):
  `it-documentation/Home Assistant - Navimow - MQTT.md §5`
