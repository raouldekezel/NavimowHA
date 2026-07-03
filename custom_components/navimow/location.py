"""Parser for the `/downlink/vehicle/<id>/realtimeDate/location` MQTT topic.

This channel is not subscribed by `mower_sdk` upstream. It carries an
array of items discriminated by `type`:

- **type 1** — vehicle pose (x, y, theta) and `vehicleState` (charging /
  idle / mowing / paused / …) at ~2 s cadence during mowing.
- **type 2** — mowing stats (progress, current boundary, week area) at
  ~30-90 s cadence during mowing.
- **type 3/4** — heartbeat / task delay, ignored here.

FEAT-01 handles type 1 only. FEAT-02 extends the module with type 2.

All parsing lives here as pure functions so the coordinator can call
them without spinning up an MQTT client, and tests can exercise them
without HA.
"""

from __future__ import annotations

import math
from typing import Any


def _to_float(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _to_int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def parse_location_type_1(item: dict[str, Any]) -> dict[str, Any] | None:
    """Parse a location item of `type == 1` (vehicle pose).

    Returns a dict with:
    - `x` (float, meters, station-relative)
    - `y` (float, meters, station-relative)
    - `theta` (float, radians, -π..π; None if firmware omitted it)
    - `vehicle_state` (int; None if firmware omitted it)
    - `distance` (float, meters, √(x²+y²))
    - `time` (int, firmware epoch ms; None if firmware omitted it)

    Returns `None` when the payload lacks the mandatory `postureX` /
    `postureY` fields (a defensive drop, not a crash — the cloud has
    been observed sending sparser variants of the same envelope).
    """
    x = _to_float(item.get("postureX"))
    y = _to_float(item.get("postureY"))
    if x is None or y is None:
        return None

    return {
        "x": x,
        "y": y,
        "theta": _to_float(item.get("postureTheta")),
        "vehicle_state": _to_int(item.get("vehicleState")),
        "distance": round(math.hypot(x, y), 2),
        "time": _to_int(item.get("time")),
    }


def parse_location_type_2(item: dict[str, Any]) -> dict[str, Any] | None:
    """Parse a location item of `type == 2` (mowing statistics).

    Returns a dict with:
    - `mowing_percentage` (int; overall run progression, 0..100)
    - `current_mow_progress` (int; current zone progression, 0..10000)
    - `area_session` (float; `subtotalArea`, m² — cumulative across the run)
    - `area_week` (float; `mowingWeekArea`, m² — since ISO week start)
    - `boundary` (int; `currentMowBoundary`, internal id, not sequential)
    - `action` (int; sub-state code observed 5/8/-1, semantics TBD)
    - `time` (int, firmware epoch ms; None if firmware omitted it)
    - `mow_start_type` (int; 1 = manual (two controlled observations,
      2026-05-25 afternoon + 2026-07-03); 0 presumed scheduled but
      unconfirmed — single uncontrolled morning-run observation)
    - `sub_action` (int; only present when `action == 8`, value 6 in every
      committed sample — see the FEAT-05 SPIKE record on #43)

    Returns `None` only when the item is not a mapping. The individual
    fields fall back to `None` on parse errors — the operator's 2026-05-25
    multizone run (diag #20) captured only full type-2 packets, but the
    parser is defensively tolerant of sparse variants should another
    Navimow firmware emit them.
    """
    if not isinstance(item, dict):
        return None
    return {
        "mowing_percentage": _to_int(item.get("mowingPercentage")),
        "current_mow_progress": _to_int(item.get("currentMowProgress")),
        "area_session": _to_float(item.get("subtotalArea")),
        "area_week": _to_float(item.get("mowingWeekArea")),
        "boundary": _to_int(item.get("currentMowBoundary")),
        "action": _to_int(item.get("action")),
        "time": _to_int(item.get("time")),
        "mow_start_type": _to_int(item.get("mowStartType")),
        "sub_action": _to_int(item.get("subAction")),
    }


def parse_location_payload(payload: bytes) -> list[dict[str, Any]] | None:
    """Decode a raw MQTT payload from the /location topic.

    Returns the decoded array on success, `None` when the payload is
    malformed. The caller iterates the array and dispatches by `type`
    (see `parse_location_type_1`).
    """
    import json

    try:
        items = json.loads((payload or b"").decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return None
    if not isinstance(items, list):
        return None
    return items
