"""FEAT-08 (#88 comment naming) — unique_id migration + area_precise
uniformity + last_run_area promotion.

Companion to ``test_feat_08_zone_surface_entities.py`` (which covers
the two new area entities). This file locks the migration and naming
contract that came out of the issue's follow-up comments:

- ``entity_registry`` unique_id renames on setup — historic entities
  keep their history rather than orphaning when the keys shift.
- ``area_precise`` attribute is present on every m² sensor.
- ``last_run_area`` was promoted out of ``last_run_result.session_area``.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from custom_components.navimow import _async_migrate_unique_ids
from custom_components.navimow.const import DOMAIN
from custom_components.navimow.sensor import SENSOR_DESCRIPTIONS

# --------------------------------------------------------------------- #
# 1. entity_registry unique_id migration                                #
# --------------------------------------------------------------------- #


def _make_entry():
    entry = MagicMock()
    entry.entry_id = "test-entry"
    return entry


def _fake_entity(unique_id: str, platform: str = DOMAIN):
    e = MagicMock()
    e.entity_id = f"sensor.{unique_id}"
    e.unique_id = unique_id
    e.config_entry_id = "test-entry"
    e.platform = platform
    return e


async def test_migration_renames_the_three_static_keys() -> None:
    ent_reg = MagicMock()
    entities = {
        "run_state": _fake_entity(f"{DOMAIN}_DEV_run_state"),
        "run_progress": _fake_entity(f"{DOMAIN}_DEV_run_progress"),
        "zone_progress": _fake_entity(f"{DOMAIN}_DEV_zone_progress"),
    }
    ent_reg.entities = MagicMock()
    ent_reg.entities.values.return_value = list(entities.values())

    with patch("homeassistant.helpers.entity_registry.async_get", return_value=ent_reg):
        await _async_migrate_unique_ids(MagicMock(), _make_entry())

    # Verify each unique_id was renamed to its `current_*` counterpart.
    calls = {
        c.kwargs["new_unique_id"] for c in ent_reg.async_update_entity.call_args_list
    }
    assert f"{DOMAIN}_DEV_current_run_state" in calls
    assert f"{DOMAIN}_DEV_current_run_progress" in calls
    assert f"{DOMAIN}_DEV_current_zone_progress" in calls


async def test_migration_renames_per_zone_bare_and_duration_ids() -> None:
    ent_reg = MagicMock()
    zone_ids = [
        f"{DOMAIN}_DEV_zone_1",  # → _last_area
        f"{DOMAIN}_DEV_zone_1_duration",  # → _last_duration
        f"{DOMAIN}_DEV_zone_3",  # → _last_area
        f"{DOMAIN}_DEV_zone_3_duration",  # → _last_duration
    ]
    ent_reg.entities = MagicMock()
    ent_reg.entities.values.return_value = [_fake_entity(u) for u in zone_ids]

    with patch("homeassistant.helpers.entity_registry.async_get", return_value=ent_reg):
        await _async_migrate_unique_ids(MagicMock(), _make_entry())

    calls = {
        c.kwargs["new_unique_id"] for c in ent_reg.async_update_entity.call_args_list
    }
    assert f"{DOMAIN}_DEV_zone_1_last_area" in calls
    assert f"{DOMAIN}_DEV_zone_1_last_duration" in calls
    assert f"{DOMAIN}_DEV_zone_3_last_area" in calls
    assert f"{DOMAIN}_DEV_zone_3_last_duration" in calls


async def test_migration_renames_surface_placeholder_to_total_area() -> None:
    """Belt-and-braces (review round 2): the first-commit placeholder
    key `_surface` never shipped in a merged prerelease, but a
    developer who installed the branch head before the naming pass
    would carry those unique_ids. Migrate them rather than orphan."""
    ent_reg = MagicMock()
    ent_reg.entities = MagicMock()
    ent_reg.entities.values.return_value = [
        _fake_entity(f"{DOMAIN}_DEV_zone_1_surface"),
        _fake_entity(f"{DOMAIN}_DEV_zone_3_surface"),
    ]

    with patch("homeassistant.helpers.entity_registry.async_get", return_value=ent_reg):
        await _async_migrate_unique_ids(MagicMock(), _make_entry())

    calls = {
        c.kwargs["new_unique_id"] for c in ent_reg.async_update_entity.call_args_list
    }
    assert f"{DOMAIN}_DEV_zone_1_total_area" in calls
    assert f"{DOMAIN}_DEV_zone_3_total_area" in calls


async def test_migration_leaves_already_migrated_ids_alone() -> None:
    """Idempotent: the second setup after migration must not rename
    anything (no matcher would fire), and must not touch already-new
    ids like ``_last_mowed`` / ``_total_area`` / ``current_run_started``."""
    ent_reg = MagicMock()
    stable = [
        f"{DOMAIN}_DEV_zone_1_last_area",
        f"{DOMAIN}_DEV_zone_1_last_duration",
        f"{DOMAIN}_DEV_zone_1_last_mowed",
        f"{DOMAIN}_DEV_zone_1_total_area",
        f"{DOMAIN}_DEV_current_run_state",
        f"{DOMAIN}_DEV_current_run_progress",
        f"{DOMAIN}_DEV_current_run_started",  # already `current_*`, unchanged key
        f"{DOMAIN}_DEV_zones_total_area",
        f"{DOMAIN}_DEV_last_run_area",
        f"{DOMAIN}_DEV_battery",
        f"{DOMAIN}_DEV_weekly_area",
    ]
    ent_reg.entities = MagicMock()
    ent_reg.entities.values.return_value = [_fake_entity(u) for u in stable]

    with patch("homeassistant.helpers.entity_registry.async_get", return_value=ent_reg):
        await _async_migrate_unique_ids(MagicMock(), _make_entry())

    ent_reg.async_update_entity.assert_not_called()


async def test_migration_ignores_other_platforms_and_other_entries() -> None:
    ent_reg = MagicMock()
    entities = [
        # Wrong platform — untouched.
        _fake_entity(f"{DOMAIN}_DEV_run_state", platform="binary_sensor"),
        # Wrong config entry — untouched.
        _fake_entity(f"{DOMAIN}_DEV_run_progress"),
    ]
    entities[1].config_entry_id = "some-other-entry"
    ent_reg.entities = MagicMock()
    ent_reg.entities.values.return_value = entities

    with patch("homeassistant.helpers.entity_registry.async_get", return_value=ent_reg):
        await _async_migrate_unique_ids(MagicMock(), _make_entry())

    ent_reg.async_update_entity.assert_not_called()


async def test_migration_last_mowed_zone_id_not_matched_by_bare_pattern() -> None:
    """Guard against the classic false-positive: the `zone_<id>` regex
    must not swallow `zone_<id>_last_mowed` (which already ends on a
    non-digit suffix). Without the `$` anchor, `zone_1` would match
    the prefix of `zone_1_last_mowed` and the migration would corrupt
    it."""
    ent_reg = MagicMock()
    ent_reg.entities = MagicMock()
    ent_reg.entities.values.return_value = [
        _fake_entity(f"{DOMAIN}_DEV_zone_1_last_mowed"),
    ]

    with patch("homeassistant.helpers.entity_registry.async_get", return_value=ent_reg):
        await _async_migrate_unique_ids(MagicMock(), _make_entry())

    ent_reg.async_update_entity.assert_not_called()


# --------------------------------------------------------------------- #
# 2. area_precise uniformity                                            #
# --------------------------------------------------------------------- #


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
