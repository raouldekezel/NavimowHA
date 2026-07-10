"""FEAT-09 — display-ready joined zone-name string for the last closed
session (`sensor.<slug>_last_run_zones`), fourth sibling of the
`last_run_*` family.

Every test exercises one seam:

- `_last_run_zones_display` pure value_fn: named single-zone,
  named multi-zone, interleaved (NOT deduped — operator preference on
  #96), mixed named/unmapped fallback, empty, missing `last_finished_run`.
- Fallback shape: per boundary `#<id>`, matching `_current_zone_display`
  (short cosmetic divergence with the per-zone entity title's
  `Zone #<id>`, intentional per HARD-15 review on #94).
- Description contract in `SENSOR_DESCRIPTIONS`: present, right
  translation_key, right icon, no attrs_fn, `refresh_on_zone_rename` set.
- Rename-refresh wiring: `NavimowSensor.async_added_to_hass` subscribes
  to `SIGNAL_ZONE_NAMES_UPDATED_<entry_id>` when the description opts
  in, and re-render pushes `async_write_ha_state` (not a re-read of
  cached state — `value_fn` reads `options` each call).
- Regression: `_last_run_start_dt`, `_last_run_duration` value, and
  `_last_run_result` value/attrs_fn are untouched.
"""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import MagicMock, patch

import pytest

from custom_components.navimow.const import OPTIONS_KEY_ZONES, SIGNAL_ZONE_NAMES_UPDATED
from custom_components.navimow.sensor import (
    SENSOR_DESCRIPTIONS,
    NavimowSensor,
    NavimowSensorEntityDescription,
    _last_run_start_dt,
    _last_run_zones_display,
)

# --------------------------------------------------------------------- #
# Fixtures                                                              #
# --------------------------------------------------------------------- #


def _make_coordinator(
    last_finished_run: dict | None = None,
    options: dict | None = None,
):
    coord = MagicMock()
    coord.last_finished_run = last_finished_run
    entry = MagicMock()
    entry.entry_id = "test-entry"
    entry.options = options or {}
    coord.config_entry = entry
    return coord


def _seg(boundary_id: int, first_time: int = 0, last_time: int = 0) -> dict:
    """Return one segment shaped like the tracker emits."""
    return {
        "boundary_id": boundary_id,
        "first_time": first_time,
        "last_time": last_time,
        "cmp_max": 10_000,
        "sub_entry": 0.0,
        "sub_exit": 0.0,
    }


# --------------------------------------------------------------------- #
# 1. _last_run_zones_display — pure value_fn                            #
# --------------------------------------------------------------------- #


def test_named_single_zone_run_renders_operator_name() -> None:
    coord = _make_coordinator(
        last_finished_run={"zones": [_seg(3)]},
        options={OPTIONS_KEY_ZONES: {"3": {"name": "Figuier"}}},
    )
    assert _last_run_zones_display(coord) == "Figuier"


def test_named_multizone_run_renders_arrow_join() -> None:
    coord = _make_coordinator(
        last_finished_run={"zones": [_seg(1), _seg(3)]},
        options={
            OPTIONS_KEY_ZONES: {
                "1": {"name": "Prunier"},
                "3": {"name": "Figuier"},
            }
        },
    )
    assert _last_run_zones_display(coord) == "Prunier → Figuier"


def test_interleaved_run_not_deduped() -> None:
    """The tracker's `zones` list is a list of *segments* — the operator
    explicitly does NOT want to hide interleaving (review reply on
    issue #96, 2026-07-10). A run that leaves and returns to the same
    zone reads as `Prunier → Figuier → Prunier`, not `Prunier → Figuier`.
    """
    coord = _make_coordinator(
        last_finished_run={"zones": [_seg(1), _seg(3), _seg(1)]},
        options={
            OPTIONS_KEY_ZONES: {
                "1": {"name": "Prunier"},
                "3": {"name": "Figuier"},
            }
        },
    )
    assert _last_run_zones_display(coord) == "Prunier → Figuier → Prunier"


def test_unmapped_boundary_falls_back_to_short_hash() -> None:
    """The fallback is `#<id>` — same short choice as
    `_current_zone_display` (HARD-11 / HARD-15 divergence with the
    per-zone entity title's `Zone #<id>`, intentional cosmetic
    consistency between the two dashboard tiles that display *state*)."""
    coord = _make_coordinator(
        last_finished_run={"zones": [_seg(7)]},
        options={},
    )
    assert _last_run_zones_display(coord) == "#7"


def test_mixed_named_and_unmapped_applies_fallback_per_boundary() -> None:
    """The fallback is applied *per boundary*, not to the whole string —
    a mixed run keeps the operator names it does have."""
    coord = _make_coordinator(
        last_finished_run={"zones": [_seg(1), _seg(3)]},
        options={OPTIONS_KEY_ZONES: {"1": {"name": "Prunier"}}},
    )
    assert _last_run_zones_display(coord) == "Prunier → #3"


def test_empty_string_reset_treated_as_unmapped() -> None:
    """An empty-string name in options is the rename flow's reset
    gesture (see FEAT-04 PR4). Same treatment as unmapped."""
    coord = _make_coordinator(
        last_finished_run={"zones": [_seg(1)]},
        options={OPTIONS_KEY_ZONES: {"1": {"name": ""}}},
    )
    assert _last_run_zones_display(coord) == "#1"


def test_no_last_finished_run_returns_none() -> None:
    """At boot before the first close, HA renders `unknown` — consistent
    with the other `last_run_*` sensors."""
    coord = _make_coordinator(last_finished_run=None)
    assert _last_run_zones_display(coord) is None


def test_empty_zones_list_returns_none() -> None:
    """A close with `zones == []` (only sentinel packets, BUG-06
    filtered) renders as None — same as boot, honest."""
    coord = _make_coordinator(last_finished_run={"zones": []})
    assert _last_run_zones_display(coord) is None


def test_missing_zones_key_returns_none() -> None:
    """Defensive — a payload that never carried `zones` (shouldn't
    happen post-FEAT-06, but the value_fn must not crash)."""
    coord = _make_coordinator(last_finished_run={"result": "completed"})
    assert _last_run_zones_display(coord) is None


def test_segment_with_none_boundary_id_is_skipped() -> None:
    """Defensive — `run_tracker` filters `boundary_id in (None, 0)`
    upstream (BUG-06), but the value_fn skips a bad segment rather than
    crashing on the KeyError."""
    coord = _make_coordinator(
        last_finished_run={
            "zones": [_seg(1), {"boundary_id": None}, _seg(3)],
        },
        options={
            OPTIONS_KEY_ZONES: {
                "1": {"name": "Prunier"},
                "3": {"name": "Figuier"},
            }
        },
    )
    assert _last_run_zones_display(coord) == "Prunier → Figuier"


def test_segment_with_zero_boundary_id_is_skipped() -> None:
    """BUG-06's `boundary=0` sentinel is filtered upstream in
    `run_tracker._append_zone`, but the value_fn matches the codebase
    idiom (`_current_zone_display`, per-zone entities) and skips `0`
    too — a stray `0` would otherwise render `#0`, the exact artifact
    BUG-06 killed. Belt-and-suspenders review nit on #99."""
    coord = _make_coordinator(
        last_finished_run={
            "zones": [_seg(1), {"boundary_id": 0}, _seg(3)],
        },
        options={
            OPTIONS_KEY_ZONES: {
                "1": {"name": "Prunier"},
                "3": {"name": "Figuier"},
            }
        },
    )
    assert _last_run_zones_display(coord) == "Prunier → Figuier"


def test_no_config_entry_falls_back_all_the_way() -> None:
    """Defensive: if for some reason `coordinator.config_entry` is
    unset (test harness omission, or wiring order edge case), the
    value_fn falls back on every boundary rather than crashing."""
    coord = MagicMock()
    coord.last_finished_run = {"zones": [_seg(1), _seg(3)]}
    # Simulate `config_entry` never set — mimic the attribute error the
    # module guards with `getattr(c, "config_entry", None)`.
    del coord.config_entry
    assert _last_run_zones_display(coord) == "#1 → #3"


# --------------------------------------------------------------------- #
# 2. Description contract in SENSOR_DESCRIPTIONS                        #
# --------------------------------------------------------------------- #


def _get_description(key: str) -> NavimowSensorEntityDescription | None:
    for d in SENSOR_DESCRIPTIONS:
        if d.key == key:
            return d
    return None


def test_last_run_zones_description_registered() -> None:
    d = _get_description("last_run_zones")
    assert d is not None
    assert d.translation_key == "last_run_zones"
    assert d.icon == "mdi:texture-box"
    # No device_class: this is a display-ready string, not an enum or
    # timestamp.
    assert d.device_class is None
    # No attrs_fn: raw zones list already lives on
    # `_last_run_result.zones`, doubling here would waste recorder.
    assert d.attrs_fn is None
    # `refresh_on_zone_rename` opts in to the base class's dispatcher
    # subscription (§3 below).
    assert d.refresh_on_zone_rename is True
    # value_fn is the pure function tested above.
    assert d.value_fn is _last_run_zones_display


def test_other_last_run_descriptions_do_not_opt_into_rename_refresh() -> None:
    """Only `last_run_zones` reads `options` — the other three
    `last_run_*` descriptors carry timestamps / durations / result
    enums that don't depend on the operator's zone names."""
    for key in ("last_run_started", "last_run_duration", "last_run_result"):
        d = _get_description(key)
        assert d is not None
        assert (
            d.refresh_on_zone_rename is False
        ), f"{key} unexpectedly opts into rename refresh"


def test_refresh_on_zone_rename_defaults_off() -> None:
    """Belt-and-braces: the new dataclass field must default False,
    so untouched descriptions keep their previous behaviour."""
    d = NavimowSensorEntityDescription(
        key="probe",
        value_fn=lambda c: None,
    )
    assert d.refresh_on_zone_rename is False


# --------------------------------------------------------------------- #
# 3. Rename-refresh wiring in NavimowSensor.async_added_to_hass         #
# --------------------------------------------------------------------- #


class _RestoreStub:
    """Minimal stub of the pytest-homeassistant-custom-component
    machinery needed to call `async_added_to_hass` without pulling in a
    full HA event loop. Only proxies the two `RestoreSensor` /
    `CoordinatorEntity` methods our patch touches."""

    async def async_get_last_sensor_data(self):
        return None


async def _call_added_to_hass(description: NavimowSensorEntityDescription) -> list:
    """Instantiate a `NavimowSensor` with the given description, call
    `async_added_to_hass`, and return the list of
    `(hass, signal, callback)` triples that `async_dispatcher_connect`
    was called with. Uses `object.__setattr__` to swap the two parent
    coroutines with the stub — we're testing the FEAT-09 wiring, not
    the base plumbing."""
    coord = _make_coordinator()
    coord.get_device_state.return_value = None
    sensor = NavimowSensor(coordinator=coord, entity_description=description)
    sensor.hass = MagicMock()
    sensor.async_write_ha_state = MagicMock()
    sensor.async_on_remove = MagicMock()

    calls: list = []

    def _fake_dispatcher_connect(hass, signal, cb):
        calls.append((hass, signal, cb))
        return lambda: None

    async def _fake_super_added(self):
        return None

    async def _fake_get_last(self):
        return None

    # Patch the two parent-class methods (CoordinatorEntity's added_to_hass
    # and RestoreSensor's last-value getter) so we don't need a real HA
    # event loop just to exercise the four new lines.
    with (
        patch(
            "custom_components.navimow.sensor.async_dispatcher_connect",
            side_effect=_fake_dispatcher_connect,
        ),
        patch(
            "homeassistant.helpers.update_coordinator.CoordinatorEntity"
            ".async_added_to_hass",
            _fake_super_added,
        ),
        patch(
            "homeassistant.components.sensor.RestoreSensor.async_get_last_sensor_data",
            _fake_get_last,
        ),
    ):
        await sensor.async_added_to_hass()

    return calls


@pytest.mark.asyncio
async def test_async_added_to_hass_subscribes_when_refresh_opted_in() -> None:
    """A description with `refresh_on_zone_rename=True` results in one
    dispatcher_connect on `SIGNAL_ZONE_NAMES_UPDATED_<entry_id>`."""
    calls = await _call_added_to_hass(_get_description("last_run_zones"))
    signals = [signal for _, signal, _ in calls]
    assert f"{SIGNAL_ZONE_NAMES_UPDATED}_test-entry" in signals


@pytest.mark.asyncio
async def test_async_added_to_hass_does_not_subscribe_when_flag_off() -> None:
    """A description without the flag does *not* connect — the other
    `last_run_*` sensors don't read `options` and shouldn't be woken by
    a rename."""
    calls = await _call_added_to_hass(_get_description("last_run_started"))
    signals = [signal for _, signal, _ in calls]
    assert f"{SIGNAL_ZONE_NAMES_UPDATED}_test-entry" not in signals


@pytest.mark.asyncio
async def test_dispatcher_callback_pushes_write_ha_state() -> None:
    """The subscribed callback pushes `async_write_ha_state` — the
    `value_fn` re-reads options each call, so there's no cache to bust."""
    coord = _make_coordinator()
    coord.get_device_state.return_value = None
    sensor = NavimowSensor(
        coordinator=coord,
        entity_description=_get_description("last_run_zones"),
    )
    sensor.hass = MagicMock()
    sensor.async_write_ha_state = MagicMock()
    sensor.async_on_remove = MagicMock()
    captured_cb: list = []

    def _fake_dispatcher_connect(hass, signal, cb):
        captured_cb.append(cb)
        return lambda: None

    async def _noop(self):
        return None

    async def _get_last(self):
        return None

    with (
        patch(
            "custom_components.navimow.sensor.async_dispatcher_connect",
            side_effect=_fake_dispatcher_connect,
        ),
        patch(
            "homeassistant.helpers.update_coordinator.CoordinatorEntity"
            ".async_added_to_hass",
            _noop,
        ),
        patch(
            "homeassistant.components.sensor.RestoreSensor.async_get_last_sensor_data",
            _get_last,
        ),
    ):
        await sensor.async_added_to_hass()

    assert captured_cb, "callback not registered"
    # Rename fires — sensor pushes a re-render.
    captured_cb[0]()
    sensor.async_write_ha_state.assert_called_once()


# --------------------------------------------------------------------- #
# 4. Regression — other last_run_* sensors are untouched                #
# --------------------------------------------------------------------- #


def test_last_run_start_dt_regression_none_and_value() -> None:
    """`_last_run_start_dt` returns `None` at boot, and a UTC datetime
    otherwise — behaviour unchanged."""
    assert _last_run_start_dt(_make_coordinator(last_finished_run=None)) is None
    coord = _make_coordinator(last_finished_run={"start_time": 1_779_694_000_000})
    dt = _last_run_start_dt(coord)
    assert dt == datetime.fromtimestamp(1_779_694_000_000 / 1000, tz=UTC)


def test_last_run_duration_value_fn_regression() -> None:
    d = _get_description("last_run_duration")
    assert d is not None
    assert d.value_fn(_make_coordinator(last_finished_run=None)) is None
    coord = _make_coordinator(
        last_finished_run={"duration_ms": 2_400_000},
    )
    assert d.value_fn(coord) == 2400


def test_last_run_result_value_fn_and_attrs_regression() -> None:
    d = _get_description("last_run_result")
    assert d is not None
    payload = {
        "result": "completed",
        "zones": [_seg(1), _seg(3)],
        "session_area": 353.55,
        "mow_start_type": 1,
    }
    coord = _make_coordinator(last_finished_run=payload)
    coord.history = ["h1", "h2"]
    assert d.value_fn(coord) == "completed"
    attrs = d.attrs_fn(coord)
    assert attrs["zones"] == payload["zones"]
    assert attrs["session_area"] == 353.55
    assert attrs["mow_start_type"] == 1
    assert attrs["history"] == ["h1", "h2"]
