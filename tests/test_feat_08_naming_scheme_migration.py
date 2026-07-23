"""FEAT-08 (#88 comment naming) — area_precise uniformity + last_run_area
promotion.

Companion to ``test_feat_08_zone_surface_entities.py`` (which covers the
two new area entities). This file locks the naming contract that came out
of the issue's follow-up comments:

- ``area_precise`` attribute is present on every m² sensor.
- ``last_run_area`` was promoted out of ``last_run_result.session_area``.

The FEAT-08 ``entity_registry`` unique_id migration and its six tests were
retired with the shim by HARD-21 (#123): the registry has carried the
`current` / `last` scheme for dozens of restarts (zero old-scheme
unique_ids in the live registry, verified 2026-07-23), so the one-time
rename is spent dead code. See #123 for the removal evidence.
"""

from __future__ import annotations

from unittest.mock import MagicMock

from custom_components.navimow.sensor import SENSOR_DESCRIPTIONS


def _desc(key: str):
    return next((d for d in SENSOR_DESCRIPTIONS if d.key == key), None)


def test_weekly_area_exposes_area_precise_attr() -> None:
    d = _desc("weekly_area")
    coord = MagicMock()
    coord.stats = {"area_week": 227.82}
    assert d.value_fn(coord) == 228  # ceil
    assert d.attrs_fn(coord) == {"area_precise": 227.82}


def test_weekly_area_attrs_none_when_stat_missing() -> None:
    """First boot before any type-2 has arrived — the attrs render as
    `None` rather than `{"area_precise": None}` so HA hides the row
    entirely on the developer-tools panel."""
    d = _desc("weekly_area")
    coord = MagicMock()
    coord.stats = {}
    assert d.value_fn(coord) is None
    assert d.attrs_fn(coord) is None


def test_last_run_area_exposes_area_precise_attr() -> None:
    d = _desc("last_run_area")
    coord = MagicMock()
    coord.last_finished_run = {"session_area": 353.55, "result": "completed"}
    assert d.value_fn(coord) == 354  # ceil(353.55)
    assert d.attrs_fn(coord) == {"area_precise": 353.55}
