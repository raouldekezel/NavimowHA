"""HARD-15 — expose the operator's raw zone name as attribute
``zone_name`` on the three per-zone sensors, so template consumers
resolve ``boundary_id → operator name`` without regex-stripping the
device prefix off ``friendly_name`` and without picking the surface
sub-class to dodge the ``_name_suffix`` leakage.

Every test exercises one seam:

- ``_zone_raw_name`` pure lookup: named, unmapped, empty-string reset.
- ``_zone_display_name`` regression: byte-identical output post-refactor
  (named + fallback, with and without suffix).
- ``_current_zone_display`` regression: routes through ``_zone_raw_name``
  but keeps its short ``#<id>`` fallback (intentional cosmetic
  divergence with the entities' ``Zone #<id>`` fallback).
- Attribute contract: ``zone_name`` + ``boundary_id`` present on all
  three sub-classes, identical string for a given ``boundary_id``.
- Named / fallback cases produce the right attribute.
- Value contract on options mutation: ``extra_state_attributes`` is a
  property (re-read each call), so a rename via the options flow is
  visible on the next read. Dispatcher-driven ``async_write_ha_state``
  is already covered by FEAT-04 PR4's listener test.
"""

from __future__ import annotations

from unittest.mock import MagicMock

from custom_components.navimow.const import OPTIONS_KEY_ZONES
from custom_components.navimow.run_tracker import STATE_IDLE, STATE_RUNNING
from custom_components.navimow.sensor import (
    NavimowZoneLastAreaSensor,
    NavimowZoneLastDurationSensor,
    NavimowZoneLastMowedSensor,
    _current_zone_display,
    _zone_display_name,
    _zone_raw_name,
)
from custom_components.navimow.zone_registry import ZoneRecord, ZoneRegistry


def _make_coordinator(zone_records: dict[int, ZoneRecord] | None = None):
    coord = MagicMock()
    coord.device.id = "REDACTED-ROBOT-SERIAL"
    coord.device.name = "Razibus"
    coord.device.model = "i210 LiDAR Pro"
    coord.device.firmware_version = "1.0.0"
    coord.device.serial_number = "REDACTED-ROBOT-SERIAL"
    coord.zone_registry = ZoneRegistry()
    if zone_records:
        coord.zone_registry.zones.update(zone_records)
    return coord


def _make_entry(options: dict | None = None):
    entry = MagicMock()
    entry.entry_id = "test-entry"
    entry.options = options or {}
    return entry


def _rec(boundary_id: int, *, surface: float | None = 200.0) -> ZoneRecord:
    return ZoneRecord(
        boundary_id=boundary_id,
        last_surface_m2=surface,
        last_cmp_max=10_000,
        last_result="completed",
    )


# --------------------------------------------------------------------- #
# 1. _zone_raw_name — pure options-flow lookup                          #
# --------------------------------------------------------------------- #


def test_zone_raw_name_returns_operator_name_when_mapped() -> None:
    entry = _make_entry({OPTIONS_KEY_ZONES: {"1": {"name": "Prunier"}}})
    assert _zone_raw_name(entry, 1) == "Prunier"


def test_zone_raw_name_returns_none_when_unmapped() -> None:
    """Fallback string ("Zone #<id>" vs "#<id>") is a caller
    responsibility — the helper returns ``None`` so each consumer picks
    its own decoration."""
    entry = _make_entry({OPTIONS_KEY_ZONES: {"1": {"name": "Prunier"}}})
    assert _zone_raw_name(entry, 3) is None


def test_zone_raw_name_returns_none_on_empty_string_reset() -> None:
    """An empty-string name in options is the rename flow's reset
    gesture (see ``test_empty_string_name_falls_back_to_hash_id`` in
    PR4). The raw helper treats it the same as an unmapped id."""
    entry = _make_entry({OPTIONS_KEY_ZONES: {"1": {"name": ""}}})
    assert _zone_raw_name(entry, 1) is None


def test_zone_raw_name_returns_none_when_options_absent() -> None:
    entry = _make_entry()
    assert _zone_raw_name(entry, 1) is None


# --------------------------------------------------------------------- #
# 2. _zone_display_name — byte-identical regression                     #
# --------------------------------------------------------------------- #


def test_display_name_regression_named_no_suffix() -> None:
    entry = _make_entry({OPTIONS_KEY_ZONES: {"1": {"name": "Prunier"}}})
    assert _zone_display_name(entry, 1) == "Prunier"


def test_display_name_regression_named_with_suffix() -> None:
    entry = _make_entry({OPTIONS_KEY_ZONES: {"1": {"name": "Prunier"}}})
    assert _zone_display_name(entry, 1, " durée") == "Prunier durée"
    assert _zone_display_name(entry, 1, " dernière tonte") == "Prunier dernière tonte"


def test_display_name_regression_fallback_no_suffix() -> None:
    entry = _make_entry()
    assert _zone_display_name(entry, 1) == "Zone #1"


def test_display_name_regression_fallback_with_suffix() -> None:
    entry = _make_entry()
    assert _zone_display_name(entry, 3, " durée") == "Zone #3 durée"


# --------------------------------------------------------------------- #
# 3. _current_zone_display — intentional short-fallback divergence      #
# --------------------------------------------------------------------- #


def _make_coord_with_current_boundary(boundary: int, options: dict | None):
    coord = MagicMock()
    coord.run_tracker = MagicMock()
    coord.run_tracker.state = STATE_RUNNING
    coord.run_tracker.current_run = {"zones": [{"boundary_id": boundary}]}
    entry = _make_entry(options)
    coord.config_entry = entry
    return coord


def test_current_zone_display_named_matches_display() -> None:
    """When a name is set, ``_current_zone_display`` returns exactly the
    same string as the entities' display base — the divergence is only
    on the fallback."""
    coord = _make_coord_with_current_boundary(
        1, {OPTIONS_KEY_ZONES: {"1": {"name": "Prunier"}}}
    )
    assert _current_zone_display(coord) == "Prunier"


def test_current_zone_display_short_fallback_preserved() -> None:
    """HARD-15 must not regress HARD-11's ``#<id>`` fallback — the
    sensor state is a live display, not an entity title. The
    ``zone_name`` attribute keeps the entities' ``Zone #<id>``
    fallback (see attribute tests below); templates correlate on
    ``boundary_id``, not the display string."""
    coord = _make_coord_with_current_boundary(3, None)
    assert _current_zone_display(coord) == "#3"


def test_current_zone_display_none_when_no_current_run() -> None:
    coord = MagicMock()
    coord.run_tracker = MagicMock()
    coord.run_tracker.state = STATE_IDLE
    coord.run_tracker.current_run = None
    coord.config_entry = _make_entry()
    assert _current_zone_display(coord) is None


# --------------------------------------------------------------------- #
# 4. Attribute contract on the three per-zone sub-classes               #
# --------------------------------------------------------------------- #


def _trio_attrs(coord, entry, boundary_id: int) -> dict:
    """Return the three sub-classes' ``extra_state_attributes`` keyed
    by the sensor short name (surface / duration / last_mowed)."""
    return {
        "surface": NavimowZoneLastAreaSensor(
            coord, entry, boundary_id
        ).extra_state_attributes,
        "duration": NavimowZoneLastDurationSensor(
            coord, entry, boundary_id
        ).extra_state_attributes,
        "last_mowed": NavimowZoneLastMowedSensor(
            coord, entry, boundary_id
        ).extra_state_attributes,
    }


def test_zone_name_attribute_present_on_all_three_subclasses_named() -> None:
    coord = _make_coordinator({1: _rec(1)})
    entry = _make_entry({OPTIONS_KEY_ZONES: {"1": {"name": "Prunier"}}})
    attrs = _trio_attrs(coord, entry, 1)
    for kind, a in attrs.items():
        assert a is not None, f"{kind} returned None"
        assert a["zone_name"] == "Prunier", f"{kind} → {a['zone_name']!r}"
        assert a["boundary_id"] == 1, f"{kind} → {a['boundary_id']!r}"


def test_zone_name_attribute_fallback_on_all_three_subclasses() -> None:
    """Unmapped id → attribute is ``Zone #<id>`` (matches the entity
    title), NOT the short ``#<id>`` used by ``current_zone``. This
    cosmetic divergence is by design — see review on issue #94."""
    coord = _make_coordinator({7: _rec(7)})
    entry = _make_entry()
    attrs = _trio_attrs(coord, entry, 7)
    for kind, a in attrs.items():
        assert a is not None, f"{kind} returned None"
        assert a["zone_name"] == "Zone #7", f"{kind} → {a['zone_name']!r}"
        assert a["boundary_id"] == 7, f"{kind} → {a['boundary_id']!r}"


def test_zone_name_identical_across_the_three_subclasses() -> None:
    """The essential HARD-15 guard: templates that read ``zone_name``
    off *any* of the three sub-classes get the same string — no
    dependency on which sub-class the selectattr lands on. Also pins
    the ``super().extra_state_attributes`` merge pattern: if a
    sub-class shadowed the base dict, one of the entries would be
    missing ``zone_name`` and this test would fail."""
    coord = _make_coordinator({3: _rec(3)})
    entry = _make_entry({OPTIONS_KEY_ZONES: {"3": {"name": "Figuier"}}})
    attrs = _trio_attrs(coord, entry, 3)
    names = {kind: a["zone_name"] for kind, a in attrs.items()}
    assert len(set(names.values())) == 1, f"divergent zone_name: {names}"
    assert names["surface"] == "Figuier"


def test_zone_name_empty_string_reset_produces_fallback() -> None:
    coord = _make_coordinator({1: _rec(1)})
    entry = _make_entry({OPTIONS_KEY_ZONES: {"1": {"name": ""}}})
    attrs = _trio_attrs(coord, entry, 1)
    for kind, a in attrs.items():
        assert a["zone_name"] == "Zone #1", f"{kind} → {a['zone_name']!r}"


def test_zone_name_attribute_absent_when_record_forgotten() -> None:
    """No ``ZoneRecord`` → entity is unavailable → attributes are
    ``None`` (base preserves that, sub-classes short-circuit on it).
    Consumers see the entity as unavailable rather than a ghost dict."""
    coord = _make_coordinator({})
    entry = _make_entry({OPTIONS_KEY_ZONES: {"1": {"name": "Prunier"}}})
    attrs = _trio_attrs(coord, entry, 1)
    for kind, a in attrs.items():
        assert a is None, f"{kind} → {a!r}"


def test_zone_name_reflects_options_mutation_without_reload() -> None:
    """``zone_name`` is a property (re-read each time), not a cached
    ``_attr_``. So mutating ``entry.options`` — as the rename flow does
    before firing ``SIGNAL_ZONE_NAMES_UPDATED`` — makes the next read
    of ``extra_state_attributes`` return the new name. The signal
    exists to push ``async_write_ha_state()`` (already covered by
    PR4's ``test_options_update_listener_fires_names_updated_signal``);
    this test locks the value-side of the contract."""
    coord = _make_coordinator({1: _rec(1)})
    entry = _make_entry({OPTIONS_KEY_ZONES: {"1": {"name": "Prunier"}}})
    surf = NavimowZoneLastAreaSensor(coord, entry, 1)
    assert surf.extra_state_attributes["zone_name"] == "Prunier"
    # Rename via the options flow — same in-place mutation the real
    # NavimowOptionsFlowHandler.async_step_rename performs.
    entry.options[OPTIONS_KEY_ZONES]["1"]["name"] = "Cerisier"
    assert surf.extra_state_attributes["zone_name"] == "Cerisier"
    # And an empty-string reset falls back to the display fallback.
    entry.options[OPTIONS_KEY_ZONES]["1"]["name"] = ""
    assert surf.extra_state_attributes["zone_name"] == "Zone #1"


def test_subclass_attrs_still_present_and_merged() -> None:
    """The merge pattern must preserve sub-class-specific attributes,
    not replace them. Locks the ``{**super()..., ...}`` invariant."""
    coord = _make_coordinator(
        {
            1: ZoneRecord(
                boundary_id=1,
                last_surface_m2=227.82,
                last_cmp_max=10_000,
                size_estimate_m2=227.82,
                last_result="completed",
                last_mowed_ms=1_779_694_000_000,
                last_duration_s=2400,
            )
        }
    )
    entry = _make_entry({OPTIONS_KEY_ZONES: {"1": {"name": "Prunier"}}})
    surf = NavimowZoneLastAreaSensor(coord, entry, 1).extra_state_attributes
    lm = NavimowZoneLastMowedSensor(coord, entry, 1).extra_state_attributes
    # LastArea keeps its own fields (FEAT-08 naming: `area_precise`,
    # `size_estimate` was promoted to the dedicated `_total_area`
    # sensor and no longer surfaces here).
    assert "size_estimate" not in surf
    assert surf["area_precise"] == 227.82
    assert surf["last_cmp_max"] == 10_000
    # And gains the base pair.
    assert surf["boundary_id"] == 1
    assert surf["zone_name"] == "Prunier"
    # LastMowed keeps its own field and gains the base pair.
    assert lm["last_result"] == "completed"
    assert lm["boundary_id"] == 1
    assert lm["zone_name"] == "Prunier"
