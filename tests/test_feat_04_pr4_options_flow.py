"""FEAT-04 PR 4 — options flow + live rename + forget wiring.

Every test exercises one seam:

- Name resolution: entity picks up ``options["zones"][str(bid)]["name"]``
  and falls back to ``Zone #<id>`` when unmapped.
- Options-flow rename step: writes into ``config_entry.options``.
- Options-flow forget step: fires SIGNAL_ZONE_FORGOTTEN.
- Sensor wiring: forget signal drops from registry + removes entities.
- Sensor wiring: options-update triggers SIGNAL_ZONE_NAMES_UPDATED.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

from custom_components.navimow.config_flow import NavimowOptionsFlowHandler
from custom_components.navimow.const import (
    DOMAIN,
    OPTIONS_KEY_ZONES,
    SIGNAL_ZONE_FORGOTTEN,
    SIGNAL_ZONE_NAMES_UPDATED,
)
from custom_components.navimow.sensor import (
    NavimowZoneDurationSensor,
    NavimowZoneLastMowedSensor,
    NavimowZoneSurfaceSensor,
    _wire_options_update_listener,
    _wire_zone_forget,
    _zone_display_name,
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
# 1. Name resolution                                                    #
# --------------------------------------------------------------------- #


def test_display_name_falls_back_to_hash_id_without_options() -> None:
    entry = _make_entry()
    assert _zone_display_name(entry, 1) == "Zone #1"
    assert _zone_display_name(entry, 3, " durée") == "Zone #3 durée"


def test_display_name_uses_options_map_when_present() -> None:
    entry = _make_entry({OPTIONS_KEY_ZONES: {"1": {"name": "Prunier"}}})
    assert _zone_display_name(entry, 1) == "Prunier"
    assert _zone_display_name(entry, 1, " dernière tonte") == "Prunier dernière tonte"
    # An unmapped id still falls back.
    assert _zone_display_name(entry, 3) == "Zone #3"


def test_zone_entities_read_name_from_options_on_construction() -> None:
    coord = _make_coordinator({1: _rec(1)})
    entry = _make_entry({OPTIONS_KEY_ZONES: {"1": {"name": "Prunier"}}})
    surf = NavimowZoneSurfaceSensor(coord, entry, 1)
    dur = NavimowZoneDurationSensor(coord, entry, 1)
    lm = NavimowZoneLastMowedSensor(coord, entry, 1)
    assert surf.name == "Prunier"
    assert dur.name == "Prunier durée"
    assert lm.name == "Prunier dernière tonte"


def test_empty_string_name_falls_back_to_hash_id() -> None:
    """An empty-string name in options behaves like no name at all —
    the rename flow accepts blank as a reset gesture."""
    entry = _make_entry({OPTIONS_KEY_ZONES: {"1": {"name": ""}}})
    assert _zone_display_name(entry, 1) == "Zone #1"


# --------------------------------------------------------------------- #
# 2. Options flow — render paths (menu + forms)                         #
# --------------------------------------------------------------------- #


async def test_init_step_shows_menu_with_rename_and_forget() -> None:
    """The top-level step must render a menu, not a form. Pins the
    contract that PR 4's UI keeps rename + forget as the two entry
    points (any future extension adds an option; the two present
    here must not disappear)."""
    entry = _make_entry()
    hass = MagicMock()
    handler = NavimowOptionsFlowHandler(entry)
    handler.hass = hass
    handler.async_show_menu = MagicMock(return_value={"type": "menu"})

    await handler.async_step_init()

    handler.async_show_menu.assert_called_once()
    kwargs = handler.async_show_menu.call_args.kwargs
    assert kwargs["step_id"] == "init"
    assert set(kwargs["menu_options"]) == {"rename_zone", "forget_zone"}


async def test_rename_step_with_no_input_renders_form_with_choices() -> None:
    """Calling ``async_step_rename_zone(None)`` on a populated entry
    shows the form with the boundary selector and name text field.
    Pins the render path — the submit branches are covered below."""
    coord = _make_coordinator(
        {
            1: _rec(1, surface=227.82),
            3: _rec(3, surface=123.54),
        }
    )
    entry = _make_entry({OPTIONS_KEY_ZONES: {"1": {"name": "Prunier"}}})
    hass = MagicMock()
    hass.data = {DOMAIN: {entry.entry_id: {"coordinators": {"dev": coord}}}}

    handler = NavimowOptionsFlowHandler(entry)
    handler.hass = hass
    handler.async_show_form = MagicMock(return_value={"type": "form"})

    result = await handler.async_step_rename_zone()

    handler.async_show_form.assert_called_once()
    kwargs = handler.async_show_form.call_args.kwargs
    assert kwargs["step_id"] == "rename_zone"
    # Schema must accept `boundary_id` (the selector) and `name` (the
    # text field). Feeding the schema a real dict is enough — voluptuous
    # will raise on an unknown key or a missing required one.
    schema = kwargs["data_schema"]
    assert schema({"boundary_id": "1", "name": "Prunier"}) == {
        "boundary_id": "1",
        "name": "Prunier",
    }
    # The known boundaries appear in the selector's `In` accept list —
    # `_In` on unknown values raises `MultipleInvalid`.
    import voluptuous as vol

    with __import__("pytest").raises(vol.Invalid):
        schema({"boundary_id": "99", "name": "Nope"})
    assert result == {"type": "form"}


# --------------------------------------------------------------------- #
# 3. Options flow — rename (submit)                                      #
# --------------------------------------------------------------------- #


async def test_rename_step_writes_name_into_options() -> None:
    coord = _make_coordinator({1: _rec(1)})
    entry = _make_entry()
    hass = MagicMock()
    hass.data = {DOMAIN: {entry.entry_id: {"coordinators": {"dev": coord}}}}

    handler = NavimowOptionsFlowHandler(entry)
    handler.hass = hass
    handler.async_create_entry = MagicMock(return_value={"type": "create_entry"})

    result = await handler.async_step_rename_zone(
        {"boundary_id": "1", "name": "Prunier"}
    )
    handler.async_create_entry.assert_called_once()
    kwargs = handler.async_create_entry.call_args.kwargs
    data = kwargs.get("data") or handler.async_create_entry.call_args.args[-1]
    assert data[OPTIONS_KEY_ZONES] == {"1": {"name": "Prunier"}}
    assert result == {"type": "create_entry"}


async def test_rename_step_with_blank_name_removes_mapping() -> None:
    coord = _make_coordinator({1: _rec(1)})
    entry = _make_entry({OPTIONS_KEY_ZONES: {"1": {"name": "Prunier"}}})
    hass = MagicMock()
    hass.data = {DOMAIN: {entry.entry_id: {"coordinators": {"dev": coord}}}}

    handler = NavimowOptionsFlowHandler(entry)
    handler.hass = hass
    handler.async_create_entry = MagicMock(return_value={})

    await handler.async_step_rename_zone({"boundary_id": "1", "name": "   "})

    data = handler.async_create_entry.call_args.kwargs.get("data") or (
        handler.async_create_entry.call_args.args[-1]
    )
    assert "1" not in data[OPTIONS_KEY_ZONES]


async def test_rename_step_aborts_when_no_zones() -> None:
    entry = _make_entry()
    hass = MagicMock()
    hass.data = {DOMAIN: {entry.entry_id: {"coordinators": {}}}}
    handler = NavimowOptionsFlowHandler(entry)
    handler.hass = hass
    handler.async_abort = MagicMock(return_value={"type": "abort"})

    await handler.async_step_rename_zone()
    handler.async_abort.assert_called_once_with(reason="no_zones")


# --------------------------------------------------------------------- #
# 4. Options flow — forget                                              #
# --------------------------------------------------------------------- #


async def test_forget_step_fires_signal_and_removes_from_options() -> None:
    coord = _make_coordinator({1: _rec(1)})
    entry = _make_entry({OPTIONS_KEY_ZONES: {"1": {"name": "Prunier"}}})
    hass = MagicMock()
    hass.data = {
        DOMAIN: {entry.entry_id: {"coordinators": {"REDACTED-ROBOT-SERIAL": coord}}}
    }

    handler = NavimowOptionsFlowHandler(entry)
    handler.hass = hass
    handler.async_create_entry = MagicMock(return_value={"type": "create_entry"})

    with patch(
        "homeassistant.helpers.dispatcher.async_dispatcher_send"
    ) as dispatcher_send:
        await handler.async_step_forget_zone({"boundary_id": "1", "confirm": True})

    # Signal fires with the boundary id as int.
    dispatcher_send.assert_called_once_with(
        hass,
        f"{SIGNAL_ZONE_FORGOTTEN}_REDACTED-ROBOT-SERIAL",
        1,
    )
    # Options no longer carry the zone.
    data = handler.async_create_entry.call_args.kwargs.get("data") or (
        handler.async_create_entry.call_args.args[-1]
    )
    assert "1" not in data[OPTIONS_KEY_ZONES]


async def test_forget_step_without_confirmation_aborts() -> None:
    coord = _make_coordinator({1: _rec(1)})
    entry = _make_entry({OPTIONS_KEY_ZONES: {"1": {"name": "Prunier"}}})
    hass = MagicMock()
    hass.data = {DOMAIN: {entry.entry_id: {"coordinators": {"dev": coord}}}}

    handler = NavimowOptionsFlowHandler(entry)
    handler.hass = hass
    handler.async_abort = MagicMock(return_value={"type": "abort"})

    with patch("homeassistant.helpers.dispatcher.async_dispatcher_send") as send:
        await handler.async_step_forget_zone({"boundary_id": "1", "confirm": False})

    handler.async_abort.assert_called_once_with(reason="forget_cancelled")
    send.assert_not_called()


# --------------------------------------------------------------------- #
# 5. Sensor wiring — forget                                             #
# --------------------------------------------------------------------- #


def test_wire_zone_forget_drops_registry_and_removes_entities() -> None:
    coord = _make_coordinator({1: _rec(1), 3: _rec(3)})
    hass = MagicMock()
    config_entry = MagicMock()

    captured = []

    def _fake_connect(_hass, _signal, cb):
        captured.append((_signal, cb))
        return lambda: None

    with patch(
        "custom_components.navimow.sensor.async_dispatcher_connect",
        side_effect=_fake_connect,
    ):
        _wire_zone_forget(hass, config_entry, coord)

    # Listens on SIGNAL_ZONE_FORGOTTEN_<device>.
    assert captured[0][0] == f"{SIGNAL_ZONE_FORGOTTEN}_REDACTED-ROBOT-SERIAL"

    on_forget = captured[0][1]

    with patch("homeassistant.helpers.entity_registry.async_get") as ent_reg_get:
        ent_reg = MagicMock()
        # FEAT-08 (#88): the sweep now covers four entities per boundary
        # — trio + `_surface`. Any lingering `unavailable` after forget
        # would be operator-visible, hence the extra sweep target.
        ent_reg.async_get_entity_id.side_effect = ["s1", "s2", "s3", "s4"]
        ent_reg_get.return_value = ent_reg

        on_forget(1)

    # Registry no longer knows boundary 1.
    assert 1 not in coord.zone_registry.zones
    # Boundary 3 untouched.
    assert 3 in coord.zone_registry.zones
    # Four entities removed: trio + FEAT-08 `_surface`.
    assert ent_reg.async_remove.call_count == 4


def test_wire_zone_forget_is_idempotent_on_unknown_boundary() -> None:
    coord = _make_coordinator({})  # empty registry
    hass = MagicMock()
    config_entry = MagicMock()

    captured = []

    def _fake_connect(_hass, _signal, cb):
        captured.append(cb)
        return lambda: None

    with patch(
        "custom_components.navimow.sensor.async_dispatcher_connect",
        side_effect=_fake_connect,
    ):
        _wire_zone_forget(hass, config_entry, coord)

    on_forget = captured[0]

    with patch("homeassistant.helpers.entity_registry.async_get") as ent_reg_get:
        ent_reg = MagicMock()
        # No entities registered for this boundary.
        ent_reg.async_get_entity_id.return_value = None
        ent_reg_get.return_value = ent_reg

        # Must not raise — echo after actual forget, or forget on
        # something the registry never held.
        on_forget(1)
    # No async_remove calls.
    assert ent_reg.async_remove.call_count == 0


# --------------------------------------------------------------------- #
# 6. Sensor wiring — options-update listener                            #
# --------------------------------------------------------------------- #


async def test_options_update_listener_fires_names_updated_signal() -> None:
    hass = MagicMock()
    config_entry = MagicMock()
    config_entry.entry_id = "test-entry"

    captured_cb = []

    def _fake_add_update_listener(cb):
        captured_cb.append(cb)
        return lambda: None

    config_entry.add_update_listener.side_effect = _fake_add_update_listener

    _wire_options_update_listener(hass, config_entry)

    assert captured_cb, "listener not registered"
    cb = captured_cb[0]

    with patch(
        "custom_components.navimow.sensor.async_dispatcher_send"
    ) as dispatcher_send:
        await cb(hass, config_entry)

    dispatcher_send.assert_called_once_with(
        hass,
        f"{SIGNAL_ZONE_NAMES_UPDATED}_test-entry",
    )
    # Unsub piped through async_on_unload.
    config_entry.async_on_unload.assert_called_once()
