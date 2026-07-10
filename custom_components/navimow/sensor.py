"""Sensor platform for Navimow integration."""

from __future__ import annotations

import math
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from homeassistant.components.sensor import (
    RestoreSensor,
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import PERCENTAGE, UnitOfArea, UnitOfLength, UnitOfTime
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.dispatcher import (
    async_dispatcher_connect,
    async_dispatcher_send,
)
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import (
    DOMAIN,
    OPTIONS_KEY_ZONES,
    SIGNAL_POSITION_UPDATE,
    SIGNAL_ZONE_DISCOVERED,
    SIGNAL_ZONE_FORGOTTEN,
    SIGNAL_ZONE_NAMES_UPDATED,
)
from .coordinator import NavimowCoordinator
from .run_tracker import STATE_PAUSED_DOCKED, STATE_RUNNING, VS_RETURNING
from .zone_registry import ZoneRecord


def _current_run_or_none(c: NavimowCoordinator) -> dict[str, Any] | None:
    """Return the tracker's current open run, or `None` at rest."""
    if c.run_tracker.state in (STATE_RUNNING, STATE_PAUSED_DOCKED):
        return c.run_tracker.current_run
    return None


def _current_boundary(c: NavimowCoordinator) -> int | None:
    """Read the current boundary from the tracker alone.

    BUG-11: source from ``run_tracker`` when it has an open run, so the
    boundary survives an HA restart mid-mow. The tracker's snapshot is
    restored from the Store before SDK callbacks register.

    BUG-12: **no stats fallback**. ``coordinator.stats`` is intentionally
    not persisted (FEAT-02 design), and — more importantly for this
    branch — it is never cleared: the Navimow cloud stops emitting
    type-2 packets as soon as the robot docks (design MQTT §5), so
    ``stats["boundary"]`` freezes on the last-mowed value and would
    render the last zone indefinitely after a run ends. Since ``stats``
    is set **before** the tracker processes the same packet in
    ``_handle_location_stats``, any time ``stats`` carries a fresh
    boundary the tracker will have one too — the fallback was only ever
    a defence against the tracker rejecting a packet (layer 1/2/3
    guard) or against BUG-06's ``boundary=0`` sentinel, and in both
    those cases ``None`` is the honest answer.

    The tracker filters BUG-06's ``boundary=0`` out of
    ``current_run.zones``, so the sentinel does not leak here either.
    """
    run = _current_run_or_none(c)
    if run is not None:
        zones = run.get("zones")
        if zones:
            return zones[-1].get("boundary_id")
    return None


def _current_zone_display(c: NavimowCoordinator) -> str | None:
    """HARD-11: resolve the current boundary's operator-chosen name.

    Reads ``options["zones"]`` off the config entry stashed on the
    coordinator (HARD-15: via the shared ``_zone_raw_name`` helper —
    the last duplicated options lookup); falls back to the short
    ``#<id>`` (not the verbose ``Zone #<id>`` used by per-zone entities
    and the ``zone_name`` attribute) when no rename exists — the
    sensor state is a live display, not an entity title. Templates
    correlate on ``boundary_id``, not the display string, so this
    cosmetic divergence is intentional.

    BUG-11 / BUG-12: the boundary comes from the tracker alone
    (survives restart, clears at end-of-run). See ``_current_boundary``
    for the source-of-truth rationale.
    """
    boundary_id = _current_boundary(c)
    if not boundary_id:
        return None
    entry = getattr(c, "config_entry", None)
    if entry is not None:
        name = _zone_raw_name(entry, boundary_id)
        if name:
            return name
    return f"#{boundary_id}"


def _run_state_display(c: NavimowCoordinator) -> str:
    """Map tracker (state, vehicle_state) to the display enum."""
    ts = c.run_tracker.state
    if ts == STATE_RUNNING:
        # `returning` = run open AND vs=5 (docked in MAP-01). vs=4
        # (mowing) is the ordinary open-run state and stays as
        # `running`. Fable brief mentions vs ∈ {4, 5} but vs=4 is the
        # dominant mowing signal — folding it into `returning` would
        # spuriously flag every mowing tick as returning-to-dock.
        return "returning" if c.vehicle_state == VS_RETURNING else "running"
    if ts == STATE_PAUSED_DOCKED:
        return "paused"
    return "idle"


def _last_run_start_dt(c: NavimowCoordinator) -> datetime | None:
    """`last_run_started` value — start time of the last *closed*
    session, `None` before the first close. FEAT-06 (#54): the three
    `last_run_*` sensors share one subject ("the last closed
    session"); the open run is exposed via `current_run_started` +
    `run_state` + `run_progress` + `zone_progress`.
    """
    if c.last_finished_run is None:
        return None
    epoch_ms = c.last_finished_run.get("start_time")
    if epoch_ms is None:
        return None
    return datetime.fromtimestamp(epoch_ms / 1000, tz=UTC)


def _current_run_start_dt(c: NavimowCoordinator) -> datetime | None:
    """`current_run_started` value — start time of the ongoing session
    (`None` when no session is open). FEAT-06 sibling of
    `_last_run_start_dt`; the pair distinguishes "current" from "last
    closed" for the dashboard row.
    """
    open_run = _current_run_or_none(c)
    if open_run is None:
        return None
    epoch_ms = open_run.get("start_time")
    if epoch_ms is None:
        return None
    return datetime.fromtimestamp(epoch_ms / 1000, tz=UTC)


def _last_run_zones_display(c: NavimowCoordinator) -> str | None:
    """FEAT-09: joined operator-chosen names of the zones mowed in the
    last **closed** session.

    Walks `last_finished_run["zones"]` — the tracker's list of *segments*,
    one entry per contiguous mow on the same boundary — resolves each
    `boundary_id` via `_zone_raw_name` (HARD-15, the single source of
    truth for `options[OPTIONS_KEY_ZONES][*]["name"]` reads), falls back
    per boundary to `#<id>` when unmapped, and joins with ` → ` (Unicode
    arrow, no i18n).

    Segments are **not** deduped: an interleaved run reads honestly as
    `Prunier → Figuier → Prunier`. Operator preference (review on #96) —
    the tracker's segment model already carries that fact, hiding it in
    the display would defeat the purpose of the sibling to
    `_last_run_result.zones`.

    Returns `None` when there is no closed run yet or when the segment
    list is empty (BUG-06's `boundary=0` sentinel keeps zones from ever
    being populated with a bogus id — that filter runs upstream in
    `run_tracker._append_zone`, not here).
    """
    if c.last_finished_run is None:
        return None
    zones = c.last_finished_run.get("zones") or []
    if not zones:
        return None
    entry = getattr(c, "config_entry", None)
    parts: list[str] = []
    for z in zones:
        b = z.get("boundary_id")
        # `if not b` matches the codebase idiom (`_current_zone_display`,
        # per-zone entities): skips both `None` and the BUG-06 `0`
        # sentinel. The tracker filters `boundary_id == 0` in
        # `_append_zone` upstream, but a stray `0` here would otherwise
        # render `#0` — the exact artifact BUG-06 killed.
        if not b:
            continue
        name = _zone_raw_name(entry, b) if entry is not None else None
        parts.append(name if name else f"#{b}")
    if not parts:
        return None
    return " → ".join(parts)


@dataclass(frozen=True, kw_only=True)
class NavimowSensorEntityDescription(SensorEntityDescription):
    """Describes Navimow sensor entity."""

    value_fn: Callable[[NavimowCoordinator], Any]
    attrs_fn: Callable[[NavimowCoordinator], dict[str, Any] | None] | None = None
    # HARD-02: opt-in HA state persistence. When True, the sensor inherits
    # RestoreSensor behaviour — the last observed value is written to
    # `.storage/core.restore_state` and re-applied at HA startup, so a
    # cumulative counter (e.g. `area_week`) survives a restart even though
    # the cloud is silent on /location while the robot is docked. Session-
    # scoped sensors (e.g. `current_zone`) leave this False so a stale
    # value never masks the "idle" reality.
    restore: bool = False
    # FEAT-09: opt-in re-render on options-flow zone rename. When True the
    # sensor connects to `SIGNAL_ZONE_NAMES_UPDATED_<entry_id>` in
    # `async_added_to_hass` and calls `async_write_ha_state` on receipt,
    # so a rename in the options flow refreshes the display without a
    # fresh mow. Only useful when `value_fn` reads
    # `config_entry.options[OPTIONS_KEY_ZONES]` (the `last_run_zones`
    # sensor is the sole current consumer — mirrors the per-zone
    # entities' `_refresh_name` wiring).
    refresh_on_zone_rename: bool = False


SENSOR_DESCRIPTIONS: tuple[NavimowSensorEntityDescription, ...] = (
    NavimowSensorEntityDescription(
        key="battery",
        translation_key="battery",
        device_class=SensorDeviceClass.BATTERY,
        native_unit_of_measurement=PERCENTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda coordinator: (
            state.battery if (state := coordinator.get_device_state()) else None
        ),
    ),
    # === /location type 2 mowing metrics (FEAT-02) ===
    #
    # HARD-14: the raw `progression` sensor (state = `mowing_percentage`,
    # attrs = `current_mow_progress` / `area_session` / `action`) was
    # retired. It read `c.stats["mowing_percentage"]` unconditionally, so
    # it froze at the last session's value (typically `100`) on a docked
    # robot for hours — the exact defect tracked by HARD-05. The
    # tracker-driven `run_progress` (task progress, `None` at rest) and
    # `zone_progress` (per-zone `cmp_max`, also `None` at rest) surface
    # the same information honestly. The parsers keep the fields alive
    # for the tracker; only the sensor entity is gone.
    NavimowSensorEntityDescription(
        key="weekly_area",
        translation_key="weekly_area",
        # HARD-08: `SensorDeviceClass.AREA` (HA 2024.12+) unlocks the
        # per-user unit-conversion (ft², etc.) HA drives from the device
        # class + native unit pair, plus consistent icon/formatting and
        # typed history graphs. `native_value` stays m²; conversion is
        # HA-side, no code change on our end.
        device_class=SensorDeviceClass.AREA,
        native_unit_of_measurement=UnitOfArea.SQUARE_METERS,
        state_class=SensorStateClass.TOTAL_INCREASING,
        icon="mdi:grass",
        # HARD-11: ceil the cumulative weekly surface for parity with the
        # per-zone / aggregate surfaces (FEAT-04 §6 D-size). `None`
        # passes through unchanged so HA renders `unknown` at first
        # boot when no type-2 has arrived yet.
        value_fn=lambda c: (
            math.ceil(v)
            if (v := (c.stats or {}).get("area_week")) is not None
            else None
        ),
        # FEAT-08 (#88 naming): expose the precise float as `area_precise`
        # — uniform contract across every area sensor
        # (`weekly_area`, `zone_<id>_last_area`, `zone_<id>_total_area`,
        # `zones_total_area`, `last_run_area`) so a template reads the
        # same attribute name on any of them.
        attrs_fn=lambda c: (
            {"area_precise": v}
            if (v := (c.stats or {}).get("area_week")) is not None
            else None
        ),
        # HARD-02: cumulative weekly counter must survive an HA restart.
        # The cloud stops publishing /location type-2 while the robot is
        # docked (FEAT-02 diag), so without RestoreSensor the value would
        # sit at `unknown` for potentially days until the next mow.
        restore=True,
    ),
    # BUG-06: filter `boundary=0` as the session-init sentinel. The very
    # first type-2 payload of a fresh mow carries `currentMowBoundary=0`
    # with every other field also zero (`currentMowProgress=0`,
    # `mowingPercentage=0`, `action=-1`, ...); the cloud only publishes
    # the real boundary in the second packet ~60 s later. See the FEAT-02
    # diag payload at `docs/diag/2026-05-25_feat-02_multizone-run/`
    # (line 1, `time=1779694241252`). Falsy filter (`else None`) collapses
    # both `None` and `0` into HA "unknown". `attrs_fn` keeps the raw
    # numeric so `#0` remains inspectable in developer tools.
    NavimowSensorEntityDescription(
        key="current_zone",
        translation_key="current_zone",
        icon="mdi:map-marker-radius",
        # HARD-11: resolve the operator-chosen name via the same helper
        # the per-zone family uses. Falls back to `#<id>` when no name
        # is set for this boundary (transit corridor, freshly-discovered
        # zone). `config_entry` is stashed on the coordinator at setup
        # time (see `async_setup_entry`); when missing (test seams that
        # skip that plumbing) we drop back to the pre-HARD-11 raw form.
        value_fn=lambda c: _current_zone_display(c),
        # HARD-13: same fallback as value_fn — but `is not None` (not
        # truthy) so BUG-06's session-init sentinel `boundary=0` still
        # surfaces here for developer-tools debugging.
        attrs_fn=lambda c: (
            {"boundary_id": b} if (b := _current_boundary(c)) is not None else None
        ),
        # HARD-17: opt into FEAT-09's dispatcher-driven rename refresh
        # (via `NavimowSensor.async_added_to_hass`) so that renaming a
        # zone in the options flow updates this tile instantly rather
        # than at the next ≤30 s coordinator tick. The `value_fn` above
        # reads `config_entry.options` each call, so pushing
        # `async_write_ha_state` on `SIGNAL_ZONE_NAMES_UPDATED` is
        # sufficient — no cache to bust. Same mechanism the per-zone
        # entities use (HARD-15's `_refresh_name`).
        refresh_on_zone_rename=True,
    ),
    # === FEAT-05 (c) — tracker-driven run/zone sensors ===
    # `run_progress` (%): held during `PAUSED_DOCKED`, `None` at rest.
    # Reads from the tracker's open run, not from `stats`, so a lingering
    # `stats["mowing_percentage"]` from a closed run does not leak into
    # the sensor (BUG-07 symptom for this entity).
    #
    # FEAT-06 (#54): this is **task** progress, not session progress —
    # the firmware's `mowingPercentage` re-bases on a fresh task
    # definition (freshly-mowed zones are credited), so a session that
    # continues an already-partly-mowed task starts at a non-zero value
    # (e.g. 65 % on the 2026-07-04 afternoon zone #3 cycle, per
    # `docs/diag/2026-07-04_spike-02_run-semantics-task-vs-session/`).
    # Operator-decided: keep the raw firmware `mp`, do not renormalise
    # to session scope — the number honestly reflects the task the
    # firmware is executing.
    # FEAT-08 (#88 naming): renamed `run_progress` → `current_run_progress`
    # to align with the `current` / `last` axis. Migration lives in
    # `__init__.py::_async_migrate_unique_ids`.
    NavimowSensorEntityDescription(
        key="current_run_progress",
        translation_key="current_run_progress",
        native_unit_of_measurement=PERCENTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        icon="mdi:progress-check",
        value_fn=lambda c: (
            r["last_mp"] if (r := _current_run_or_none(c)) is not None else None
        ),
    ),
    # `current_zone_progress` (%): `currentMowProgress / 100` of the
    # current zone, held during pause, `None` at rest. Renamed from
    # `zone_progress` per FEAT-08 (#88 naming).
    NavimowSensorEntityDescription(
        key="current_zone_progress",
        translation_key="current_zone_progress",
        native_unit_of_measurement=PERCENTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        icon="mdi:progress-helper",
        value_fn=lambda c: (
            r["zones"][-1]["cmp_max"] / 100.0
            if (r := _current_run_or_none(c)) is not None and r.get("zones")
            else None
        ),
    ),
    # `current_run_state`: enum idle/running/paused/returning. Renamed
    # from `run_state` per FEAT-08 (#88 naming). `returning` heuristic
    # documented in `_run_state_display`. `options` must match every
    # value the display fn can return — HA's enum-checks block
    # short-circuits when `options is None` (no error raised), so
    # declaring them here enables value-in-options validation and
    # exposes the OPTIONS capability attribute for the frontend.
    NavimowSensorEntityDescription(
        key="current_run_state",
        translation_key="current_run_state",
        device_class=SensorDeviceClass.ENUM,
        options=["idle", "running", "paused", "returning"],
        icon="mdi:state-machine",
        value_fn=_run_state_display,
    ),
    # `current_run_started` — start time of the ongoing session, `None`
    # at rest. FEAT-06 (#54): pairs with `last_run_started` (last closed
    # session) so the dashboard can show a coherent "current" row and a
    # coherent "last" row without either sensor lying about the subject.
    NavimowSensorEntityDescription(
        key="current_run_started",
        translation_key="current_run_started",
        device_class=SensorDeviceClass.TIMESTAMP,
        icon="mdi:play-circle-outline",
        value_fn=_current_run_start_dt,
    ),
    # `last_run_started` — start time of the last **closed** session.
    # FEAT-06 (#54): reads `last_finished_run` exclusively; there is no
    # open-run fallback (the ongoing session lives on
    # `current_run_started`). Persisted via `last_finished_run` in Store.
    NavimowSensorEntityDescription(
        key="last_run_started",
        translation_key="last_run_started",
        device_class=SensorDeviceClass.TIMESTAMP,
        icon="mdi:calendar-clock",
        value_fn=_last_run_start_dt,
    ),
    # `last_run_duration` (seconds) — duration of the last **closed**
    # session (from `last_finished_run.duration_ms`). Same subject as
    # `last_run_started`: the last closed session. Not a live counter.
    NavimowSensorEntityDescription(
        key="last_run_duration",
        translation_key="last_run_duration",
        native_unit_of_measurement=UnitOfTime.SECONDS,
        device_class=SensorDeviceClass.DURATION,
        state_class=SensorStateClass.MEASUREMENT,
        icon="mdi:timer-outline",
        value_fn=lambda c: (
            round(c.last_finished_run["duration_ms"] / 1000)
            if c.last_finished_run
            and c.last_finished_run.get("duration_ms") is not None
            else None
        ),
    ),
    # `last_run_result` — `completed` / `interrupted` for the last
    # **closed** session, with `zones`, `mow_start_type`, and `history`
    # as attributes (feeds the future green/red run history card). Same
    # subject as the other two `last_run_*` sensors (FEAT-06).
    #
    # FEAT-08 (#88 naming): `session_area` was promoted out — the
    # dedicated `last_run_area` sensor below carries the value + its
    # `area_precise` float. Kept off the attribute dict so a template
    # doesn't sprout two ways to read the same fact.
    NavimowSensorEntityDescription(
        key="last_run_result",
        translation_key="last_run_result",
        device_class=SensorDeviceClass.ENUM,
        options=["completed", "interrupted"],
        icon="mdi:flag-checkered",
        value_fn=lambda c: (
            c.last_finished_run.get("result") if c.last_finished_run else None
        ),
        attrs_fn=lambda c: (
            {
                "zones": c.last_finished_run.get("zones"),
                "mow_start_type": c.last_finished_run.get("mow_start_type"),
                "history": c.history,
            }
            if c.last_finished_run
            else None
        ),
    ),
    # FEAT-08 (#88 naming): `last_run_area` promoted from the
    # `session_area` attribute of `last_run_result`. `session_area` is
    # the tracker's `last_sub − sub₀` — the surface actually mowed in
    # the last closed session, honest under interruption. `ceil` state
    # + `area_precise` attr for the uniform area contract.
    NavimowSensorEntityDescription(
        key="last_run_area",
        translation_key="last_run_area",
        device_class=SensorDeviceClass.AREA,
        native_unit_of_measurement=UnitOfArea.SQUARE_METERS,
        state_class=SensorStateClass.MEASUREMENT,
        icon="mdi:texture-box",
        value_fn=lambda c: (
            math.ceil(v)
            if c.last_finished_run
            and (v := c.last_finished_run.get("session_area")) is not None
            else None
        ),
        attrs_fn=lambda c: (
            {"area_precise": v}
            if c.last_finished_run
            and (v := c.last_finished_run.get("session_area")) is not None
            else None
        ),
    ),
    # FEAT-09 (#96) — `last_run_zones`: display-ready joined zone-name
    # string for the last **closed** session. Fourth sibling in the
    # `last_run_*` family (start / duration / result / zones), same
    # subject and same refresh cadence. `value_fn` walks the tracker's
    # segment list, so an interleaved run reads honestly as
    # `Prunier → Figuier → Prunier` (no dedup, operator preference on
    # #96). The unmapped fallback is `#<id>` per boundary — same short
    # cosmetic choice as `_current_zone_display` (HARD-11 / HARD-15
    # divergence with the per-zone entity title's `Zone #<id>`).
    # `attrs_fn` is deliberately absent: the raw segment list already
    # lives on `_last_run_result.zones`, doubling it here would
    # duplicate recorder cost on a value that changes once per session.
    # `refresh_on_zone_rename=True` re-renders the tile the moment the
    # operator renames a boundary in the options flow — no wait for the
    # next mow, no reload. See `NavimowSensor.async_added_to_hass`.
    NavimowSensorEntityDescription(
        key="last_run_zones",
        translation_key="last_run_zones",
        icon="mdi:texture-box",
        value_fn=_last_run_zones_display,
        refresh_on_zone_rename=True,
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Navimow sensors from a config entry."""
    data = hass.data[DOMAIN][config_entry.entry_id]
    devices = data["devices"]
    coordinators: dict[str, NavimowCoordinator] = data["coordinators"]

    entities: list[SensorEntity] = []
    for device in devices:
        coordinator = coordinators[device.id]
        # HARD-11: stash the config entry so description-based value_fn's
        # can reach `options["zones"]` (current_zone name resolution).
        coordinator.config_entry = config_entry
        for description in SENSOR_DESCRIPTIONS:
            entities.append(
                NavimowSensor(
                    coordinator=coordinator,
                    entity_description=description,
                )
            )
        # FEAT-01 — the position sensor is dispatcher-driven (throttled)
        # rather than coordinator-driven, so it does not share the tick
        # cadence of the other sensors.
        entities.append(NavimowPositionSensor(coordinator))
        # FEAT-04 PR 3 — zone family: one static aggregate + a lazy trio
        # per boundary. The aggregate is always added; the per-zone
        # trios are added eagerly for the boundaries already known
        # (registry rebuilt from history in PR 2's restore path) and
        # lazily on `SIGNAL_ZONE_DISCOVERED` for boundaries that appear
        # at runtime.
        entities.append(NavimowZonesAggregateSensor(coordinator))
        # FEAT-08: first-class total-area aggregate — a dashboard card
        # binds on `state = ceil(Σ size_estimate)` directly, without
        # pulling the value out of an attribute. The count aggregate
        # above is now trimmed to just `zone_ids` (FEAT-08 attr promotion).
        entities.append(NavimowZonesTotalAreaSensor(coordinator))
        for boundary_id in coordinator.zone_registry.zones:
            entities.extend(_build_zone_family(coordinator, config_entry, boundary_id))
        _wire_zone_discovery(hass, config_entry, coordinator, async_add_entities)
        _wire_zone_forget(hass, config_entry, coordinator)
        _wire_options_update_listener(hass, config_entry)
    async_add_entities(entities)


def _build_zone_family(
    coordinator: NavimowCoordinator,
    config_entry: ConfigEntry,
    boundary_id: int,
) -> list[SensorEntity]:
    """Return the four per-zone sensors for a boundary.

    FEAT-08 (#88): renamed from ``_build_zone_trio`` — the family
    grew to include ``NavimowZoneTotalAreaSensor`` (the size-estimate
    entity) alongside the three original last-mow sensors.
    """
    return [
        NavimowZoneLastAreaSensor(coordinator, config_entry, boundary_id),
        NavimowZoneLastDurationSensor(coordinator, config_entry, boundary_id),
        NavimowZoneLastMowedSensor(coordinator, config_entry, boundary_id),
        NavimowZoneTotalAreaSensor(coordinator, config_entry, boundary_id),
    ]


def _wire_zone_discovery(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    coordinator: NavimowCoordinator,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Connect the ``SIGNAL_ZONE_DISCOVERED_<device_id>`` listener.

    Runtime-discovered boundaries land here. A guard against
    double-add is essential because PR 4 lets the operator forget a
    zone: if the same ``boundary_id`` reappears the following mow,
    ``ingest_run`` re-fires the signal, and HA's own unique-id dedup
    does the rest — but we still avoid an unnecessary call by
    tracking the set locally. The set is mutated on ``forget`` so a
    re-discovery does add the trio back.
    """
    known: set[int] = set(coordinator.zone_registry.zones.keys())

    @callback
    def _on_discovery(boundary_id: int) -> None:
        if boundary_id in known:
            return
        known.add(boundary_id)
        async_add_entities(_build_zone_family(coordinator, config_entry, boundary_id))

    @callback
    def _on_forget(boundary_id: int) -> None:
        # Keep the known set in sync so a future re-discovery re-adds.
        known.discard(boundary_id)

    unsub = async_dispatcher_connect(
        hass,
        f"{SIGNAL_ZONE_DISCOVERED}_{coordinator.device.id}",
        _on_discovery,
    )
    config_entry.async_on_unload(unsub)
    unsub_forget = async_dispatcher_connect(
        hass,
        f"{SIGNAL_ZONE_FORGOTTEN}_{coordinator.device.id}",
        _on_forget,
    )
    config_entry.async_on_unload(unsub_forget)


def _wire_zone_forget(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    coordinator: NavimowCoordinator,
) -> None:
    """Handle ``SIGNAL_ZONE_FORGOTTEN_<device_id>``.

    Drops the boundary from the registry AND removes the three
    per-zone entities from the entity registry. Deferred import of
    ``entity_registry`` keeps the module top-level thin. The removal
    is idempotent — a signal echoing after the fact is safe.
    """

    @callback
    def _on_forget(boundary_id: int) -> None:
        coordinator.zone_registry.forget(boundary_id)
        # Remove the three entity registry entries so they don't linger
        # as `unavailable`. If a run later re-discovers the same id,
        # PR 3's dispatcher re-adds a fresh trio.
        from homeassistant.helpers import entity_registry as er

        ent_reg = er.async_get(hass)
        device_id = coordinator.device.id
        # FEAT-08 (#88 naming): the four suffixes reflect the new
        # per-zone family — `_last_area`, `_last_duration`,
        # `_last_mowed`, `_total_area`. Any lingering entity after
        # the record is dropped would show up as `unavailable` in
        # the sensor list.
        for suffix in ("_last_area", "_last_duration", "_last_mowed", "_total_area"):
            uid = f"{DOMAIN}_{device_id}_zone_{boundary_id}{suffix}"
            entity_id = ent_reg.async_get_entity_id("sensor", DOMAIN, uid)
            if entity_id:
                ent_reg.async_remove(entity_id)

    unsub = async_dispatcher_connect(
        hass,
        f"{SIGNAL_ZONE_FORGOTTEN}_{coordinator.device.id}",
        _on_forget,
    )
    config_entry.async_on_unload(unsub)


def _wire_options_update_listener(
    hass: HomeAssistant, config_entry: ConfigEntry
) -> None:
    """Fire ``SIGNAL_ZONE_NAMES_UPDATED`` after any options-flow save.

    Per-zone entities listen on the signal and re-derive their
    ``_attr_name`` from ``config_entry.options[OPTIONS_KEY_ZONES]``,
    then call ``async_write_ha_state`` — no integration reload, no
    entity re-registration.
    """

    async def _on_options_updated(hass_: HomeAssistant, entry: ConfigEntry) -> None:
        async_dispatcher_send(
            hass_,
            f"{SIGNAL_ZONE_NAMES_UPDATED}_{entry.entry_id}",
        )

    unsub = config_entry.add_update_listener(_on_options_updated)
    config_entry.async_on_unload(unsub)


class NavimowSensor(CoordinatorEntity[NavimowCoordinator], RestoreSensor):
    """Representation of a Navimow sensor.

    Inherits `RestoreSensor` so descriptions that opt in via
    `entity_description.restore=True` (HARD-02) survive HA restarts. For
    non-restoring descriptions the behaviour is unchanged: `native_value`
    returns whatever `value_fn` computes from the coordinator, `None`
    included.
    """

    entity_description: NavimowSensorEntityDescription
    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: NavimowCoordinator,
        entity_description: NavimowSensorEntityDescription,
    ) -> None:
        super().__init__(coordinator)
        self.entity_description = entity_description

        device = coordinator.device
        self._attr_unique_id = f"{DOMAIN}_{device.id}_{entity_description.key}"
        self._attr_device_info = _device_info(coordinator)

    async def async_added_to_hass(self) -> None:
        """Seed the restore cache from the last stored value (HARD-02)
        and wire the FEAT-09 rename-refresh dispatcher when the
        description opts in.
        """
        await super().async_added_to_hass()
        if self.entity_description.refresh_on_zone_rename:
            # FEAT-09: re-render on options-flow zone rename. `value_fn`
            # already re-reads options each call, so pushing
            # `async_write_ha_state` is sufficient — no cache to bust.
            entry = getattr(self.coordinator, "config_entry", None)
            if entry is not None:

                @callback
                def _on_names_updated() -> None:
                    self.async_write_ha_state()

                self.async_on_remove(
                    async_dispatcher_connect(
                        self.hass,
                        f"{SIGNAL_ZONE_NAMES_UPDATED}_{entry.entry_id}",
                        _on_names_updated,
                    )
                )
        if not self.entity_description.restore:
            return
        last = await self.async_get_last_sensor_data()
        if last is not None and last.native_value is not None:
            self._attr_native_value = last.native_value

    @property
    def available(self) -> bool:
        if self.coordinator.get_device_state() is not None:
            return True
        return super().available

    @property
    def native_value(self) -> Any:
        """Return the live coordinator value, or the restored fallback.

        Live values always win — and they refresh the internal cache so
        the next restart resumes from the freshest value we ever saw. The
        restored fallback only fires when both `value_fn` returns `None`
        *and* the description opts in to restoration; otherwise `None`
        passes through unchanged (HA renders `unknown`).
        """
        live = self.entity_description.value_fn(self.coordinator)
        if live is not None:
            self._attr_native_value = live
            return live
        if self.entity_description.restore:
            return self._attr_native_value
        return None

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        if self.entity_description.attrs_fn is None:
            return None
        return self.entity_description.attrs_fn(self.coordinator)


# === FEAT-04 PR 3 — per-zone family + aggregate ==========================


def _device_info(coordinator: NavimowCoordinator) -> DeviceInfo:
    """HARD-07: single source of truth for the mower's ``DeviceInfo``.

    All Navimow entities — `NavimowSensor` (FEAT-02 / run family),
    `NavimowPositionSensor` (FEAT-01, dispatcher-driven), and the
    zone family (`_NavimowZoneEntity` sub-classes + the aggregate,
    FEAT-04 PR 3) — attach to the same mower device via the shared
    `identifiers={(DOMAIN, device.id)}`. Keeping the description in
    one place prevents the silent-drift trap where the device entry
    starts taking whichever entity registered last after a partial
    metadata edit.

    Zones sit on the mower's device on purpose — the design was
    explicit that we do not create a per-zone device (FEAT-04 §6):
    dynamic naming, the ability to survive a firmware id renumbering,
    and options-flow-driven renames are all data the integration
    owns, not the device registry.
    """
    device = coordinator.device
    return DeviceInfo(
        identifiers={(DOMAIN, device.id)},
        name=device.name,
        manufacturer="Navimow",
        model=device.model or "Unknown",
        sw_version=device.firmware_version or None,
        serial_number=device.serial_number or device.id,
    )


def _zone_raw_name(config_entry: ConfigEntry, boundary_id: int) -> str | None:
    """HARD-15: return the operator's chosen name for ``boundary_id``,
    or ``None`` when unmapped (missing key, or empty-string reset).

    Single source-of-truth for
    ``config_entry.options[OPTIONS_KEY_ZONES][str(boundary_id)]["name"]``
    reads; ``_zone_display_name`` / ``_current_zone_display`` and the
    per-zone ``zone_name`` attribute all route through here and
    decorate ``None`` with their own fallback string.
    """
    zones_opt = config_entry.options.get(OPTIONS_KEY_ZONES, {}) or {}
    entry = zones_opt.get(str(boundary_id))
    name = (entry or {}).get("name")
    return name if name else None


def _zone_display_name(
    config_entry: ConfigEntry, boundary_id: int, suffix: str = ""
) -> str:
    """Compose the entity display name.

    HARD-15: reads the operator's chosen name via ``_zone_raw_name``
    and falls back to ``Zone #<id>`` when unmapped. Optional ``suffix``
    (`` durée`` / `` dernière tonte``) is appended verbatim. PR 4's
    options-update signal re-derives it and calls
    ``async_write_ha_state``.
    """
    name = _zone_raw_name(config_entry, boundary_id)
    base = name if name else f"Zone #{boundary_id}"
    return f"{base}{suffix}"


class _NavimowZoneEntity(CoordinatorEntity[NavimowCoordinator], SensorEntity):
    """Base for the three per-zone sensors.

    Anchored on the firmware ``boundary_id`` in the ``unique_id`` so the
    entities survive an app-side rename (which does not touch the id),
    and are cleanable via the options-flow ``forget`` (PR 4).

    The display name is read from ``config_entry.options`` — the
    operator's rename (PR 4) refreshes it live via a dispatcher signal,
    no reload required.
    """

    _attr_has_entity_name = True
    # Suffix appended to the operator's zone name in the display. Subclass
    # overrides for `_duree` / `_derniere_tonte`.
    _name_suffix: str = ""

    def __init__(
        self,
        coordinator: NavimowCoordinator,
        config_entry: ConfigEntry,
        boundary_id: int,
    ) -> None:
        super().__init__(coordinator)
        self._boundary_id = boundary_id
        self._config_entry = config_entry
        self._attr_device_info = _device_info(coordinator)
        # ``_attr_name`` is set directly (not via ``translation_key``)
        # because per-zone names are dynamic and translation keys
        # resolve statically at load — see design §6.
        self._refresh_name()

    def _refresh_name(self) -> None:
        self._attr_name = _zone_display_name(
            self._config_entry, self._boundary_id, self._name_suffix
        )

    async def async_added_to_hass(self) -> None:
        """Subscribe to the rename signal so the display refreshes live."""
        await super().async_added_to_hass()

        @callback
        def _on_names_updated() -> None:
            self._refresh_name()
            self.async_write_ha_state()

        self.async_on_remove(
            async_dispatcher_connect(
                self.hass,
                f"{SIGNAL_ZONE_NAMES_UPDATED}_{self._config_entry.entry_id}",
                _on_names_updated,
            )
        )

    @property
    def _record(self) -> ZoneRecord | None:
        return self.coordinator.zone_registry.zones.get(self._boundary_id)

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        """HARD-15: the two consumer-facing fields common to the three
        per-zone sub-classes.

        ``boundary_id`` is the stable join key (unchanged across an
        app-side rename); ``zone_name`` is the display-ready string —
        the operator's rename (options-flow PR 4) or the
        ``Zone #<id>`` fallback, matching the entity title. Templates
        display ``zone_name`` and correlate on ``boundary_id`` (the
        divergence with ``current_zone``'s shorter ``#<id>`` fallback
        is intentional and cosmetic — see ``_current_zone_display``).

        Sub-classes merge this dict into their own attributes rather
        than shadow it, so all three sensors expose the pair.
        """
        if self._record is None:
            return None
        name = _zone_raw_name(self._config_entry, self._boundary_id)
        return {
            "boundary_id": self._boundary_id,
            "zone_name": name if name else f"Zone #{self._boundary_id}",
        }

    @property
    def available(self) -> bool:
        # Present as long as the registry still knows this boundary.
        # ``forget`` (§7) removes the entity from the registry outright
        # rather than flipping `available` to False.
        return self._record is not None


class NavimowZoneLastAreaSensor(_NavimowZoneEntity):
    """Last-mow area for one boundary, ``ceil``'d to the next m².

    FEAT-08 (#88 naming): renamed from ``NavimowZoneSurfaceSensor``
    (unique_id ``zone_<id>`` → ``zone_<id>_last_area``). The
    `zone_<id>_total_area` sibling now carries the size-estimate as its
    own state; the two coexist by design — last-mow answers "did we
    finish the zone last time?", total answers "how big is this zone?".

    Attributes carry the precise float as ``area_precise`` — uniform
    contract across every area sensor.
    """

    # HARD-08: same rationale as `weekly_area` — `SensorDeviceClass.AREA`
    # + m² gives HA the pair it needs to drive per-user unit conversion,
    # consistent icon/formatting, and typed history graphs. `ceil`'d
    # `native_value` stays m²; the precise float lives in attributes.
    _attr_device_class = SensorDeviceClass.AREA
    _attr_native_unit_of_measurement = UnitOfArea.SQUARE_METERS
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_icon = "mdi:texture-box"
    # FR display: "Prunier dernière surface" — parallel to
    # ` dernière tonte` on the timestamp sibling.
    _name_suffix = " dernière surface"

    def __init__(
        self,
        coordinator: NavimowCoordinator,
        config_entry: ConfigEntry,
        boundary_id: int,
    ) -> None:
        super().__init__(coordinator, config_entry, boundary_id)
        self._attr_unique_id = (
            f"{DOMAIN}_{coordinator.device.id}_zone_{boundary_id}_last_area"
        )

    @property
    def native_value(self) -> int | None:
        rec = self._record
        if rec is None or rec.last_surface_m2 is None:
            return None
        return math.ceil(rec.last_surface_m2)

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        rec = self._record
        if rec is None:
            return None
        # FEAT-08 (#88 naming): `size_estimate` was promoted to
        # `NavimowZoneTotalAreaSensor` and dropped from this attr dict
        # to keep one canonical source for that quantity.
        return {
            **(super().extra_state_attributes or {}),
            "area_precise": rec.last_surface_m2,
            "last_cmp_max": rec.last_cmp_max,
            "last_result": rec.last_result,
        }


class NavimowZoneLastDurationSensor(_NavimowZoneEntity):
    """Last-mow in-zone wall-clock duration (recharge inside a segment
    included). Design §5 D1.

    FEAT-08 (#88 naming): renamed from ``NavimowZoneDurationSensor``
    (unique_id ``zone_<id>_duration`` → ``zone_<id>_last_duration``).
    """

    _attr_native_unit_of_measurement = UnitOfTime.SECONDS
    _attr_device_class = SensorDeviceClass.DURATION
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_icon = "mdi:timer-outline"
    # HA display fallback: FR shows "5 min", EN shows "5 minutes", etc.
    # The native unit stays seconds so history-graph charts do not
    # shatter on a unit change if a later phase widens the range.
    _name_suffix = " durée"

    def __init__(
        self,
        coordinator: NavimowCoordinator,
        config_entry: ConfigEntry,
        boundary_id: int,
    ) -> None:
        super().__init__(coordinator, config_entry, boundary_id)
        self._attr_unique_id = (
            f"{DOMAIN}_{coordinator.device.id}_zone_{boundary_id}_last_duration"
        )

    @property
    def native_value(self) -> int | None:
        rec = self._record
        return rec.last_duration_s if rec is not None else None


class NavimowZoneLastMowedSensor(_NavimowZoneEntity):
    """Timestamp of this boundary's own last mow exit — NOT the run
    end (Fable correction, design §5). On an interleaved run
    ``[1, 3, 1]`` zone 1's last-mowed sits *after* zone 3's."""

    _attr_device_class = SensorDeviceClass.TIMESTAMP
    _attr_icon = "mdi:calendar-clock"
    _name_suffix = " dernière tonte"

    def __init__(
        self,
        coordinator: NavimowCoordinator,
        config_entry: ConfigEntry,
        boundary_id: int,
    ) -> None:
        super().__init__(coordinator, config_entry, boundary_id)
        self._attr_unique_id = (
            f"{DOMAIN}_{coordinator.device.id}_zone_{boundary_id}_last_mowed"
        )

    @property
    def native_value(self) -> datetime | None:
        rec = self._record
        if rec is None or rec.last_mowed_ms is None:
            return None
        return datetime.fromtimestamp(rec.last_mowed_ms / 1000, tz=UTC)

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        rec = self._record
        if rec is None:
            return None
        return {
            **(super().extra_state_attributes or {}),
            "last_result": rec.last_result,
        }


class NavimowZoneTotalAreaSensor(_NavimowZoneEntity):
    """FEAT-08 — first-class zone total area (last complete pass).

    The sibling ``NavimowZoneLastAreaSensor`` renders the *last mow's*
    surface (honest under interruptions, but shrinks below the real
    zone size when a firmware task straddles sessions and the tracker
    sees only its incremental `sub` delta — the operator observed
    96 m² for a 228 m² zone on ``raoul.11``). This sensor exposes the
    quantity a dashboard actually wants for the ``Prunier: 228 m²``
    tile: ``ceil(size_estimate_m2)`` from the last complete pass
    (``cmp_max >= COMPLETE_PASS_CMP``), auto-corrected on the next
    complete pass — the last-wins semantics of the registry (§4).

    Design decisions (issue #88 + comment naming scheme):

    - **`_total_area` key** (not `_surface`): the naming axis is
      ``total`` = full zone size vs ``last`` = last-mow surface,
      uniform with `zones_total_area` / `last_run_area` / etc.
    - **State is `None` until the first complete pass** — no fake ``0``
      fallback. HA renders ``unknown`` and the tile stays honest.
    - **Reads the registry directly** — no new source, no new signal,
      same update path as the sibling per-zone entities.
    - **`area_precise` attr** — uniform contract across every area
      sensor (`weekly_area`, `_last_area`, `_total_area`,
      `zones_total_area`, `last_run_area`).
    """

    # HARD-08 alignment: `SensorDeviceClass.AREA` + m² for per-user
    # unit conversion + typed history graphs. `ceil`'d int state
    # (§6 D-size), precise float in the attributes.
    _attr_device_class = SensorDeviceClass.AREA
    _attr_native_unit_of_measurement = UnitOfArea.SQUARE_METERS
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_icon = "mdi:texture-box"
    # `<zone name> surface` on the display (FR). Raoul confirmed on
    # #88 (2026-07-10 comment): "surface" reads naturally in French
    # for the zone-size tile even though the key uses `area`.
    _name_suffix = " surface"

    def __init__(
        self,
        coordinator: NavimowCoordinator,
        config_entry: ConfigEntry,
        boundary_id: int,
    ) -> None:
        super().__init__(coordinator, config_entry, boundary_id)
        self._attr_unique_id = (
            f"{DOMAIN}_{coordinator.device.id}_zone_{boundary_id}_total_area"
        )

    @property
    def native_value(self) -> int | None:
        rec = self._record
        if rec is None or rec.size_estimate_m2 is None:
            return None
        return math.ceil(rec.size_estimate_m2)

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        rec = self._record
        if rec is None:
            return None
        stamp = rec.size_estimate_updated_ms
        return {
            **(super().extra_state_attributes or {}),
            "area_precise": rec.size_estimate_m2,
            "last_complete_pass_at": (
                datetime.fromtimestamp(stamp / 1000, tz=UTC)
                if stamp is not None
                else None
            ),
        }


class NavimowZonesAggregateSensor(CoordinatorEntity[NavimowCoordinator], SensorEntity):
    """Static aggregate over all zones.

    State = zone **count** (decision D-agg, design §12): a small badge
    number that rarely changes. Interesting numbers (surface totals,
    ids, per-zone summary) live in attributes so recorder churn stays
    minimal.

    Static (single instance per device) → carries ``translation_key`` in
    ``strings.json``/``en.json``/``fr.json`` (§6 lesson from PR #50 —
    a keyless static entity ships nameless).
    """

    _attr_has_entity_name = True
    _attr_translation_key = "zones"
    _attr_icon = "mdi:map-outline"
    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(self, coordinator: NavimowCoordinator) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{DOMAIN}_{coordinator.device.id}_zones"
        self._attr_device_info = _device_info(coordinator)

    @property
    def native_value(self) -> int:
        return len(self.coordinator.zone_registry.zones)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        # FEAT-08 (#88 naming): `total_area` and `per_zone` were
        # promoted out — `zones_total_area` carries the sum and each
        # boundary owns its own `zone_<id>_total_area` / `_last_area`
        # sensors. Keeping the count sensor's attrs down to `zone_ids`
        # avoids a template accidentally reading a stale summary here
        # instead of the dedicated entities.
        return {
            "zone_ids": sorted(self.coordinator.zone_registry.zones.keys()),
        }


class NavimowZonesTotalAreaSensor(CoordinatorEntity[NavimowCoordinator], SensorEntity):
    """FEAT-08 — first-class Σ zone surface, ceil'd.

    Sibling to ``NavimowZonesAggregateSensor``: same registry, same
    update path. The count aggregate answers "how many zones has the
    robot discovered?"; this one answers "what's the mowed acreage?"
    for the dashboard's headline number.

    Design decisions (issue #88):

    - **Not folded into the count aggregate** (which keeps its own
      ``total_area`` attr for backward-compat). The count sensor rarely
      changes; this one nudges on each complete pass and belongs on a
      separate history graph.
    - **``ceil`` state, precise not exposed** at the aggregate level.
      Precise floats live on the per-zone ``_surface`` entities where
      the granularity matters; the aggregate rounds up once at the sum
      to avoid drift from N-zone floor'd summation.
    - **Zones without a complete pass contribute 0** — same rule as the
      count aggregate's ``total_area`` attribute, kept explicit here so
      the state stays honest before all zones have been fully mowed.
    """

    _attr_has_entity_name = True
    _attr_translation_key = "zones_total_area"
    _attr_device_class = SensorDeviceClass.AREA
    _attr_native_unit_of_measurement = UnitOfArea.SQUARE_METERS
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_icon = "mdi:texture-box"

    def __init__(self, coordinator: NavimowCoordinator) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{DOMAIN}_{coordinator.device.id}_zones_total_area"
        self._attr_device_info = _device_info(coordinator)

    async def async_added_to_hass(self) -> None:
        """Wire the FEAT-08 zone-rename refresh so ``zone_names`` in
        attrs stays in sync without waiting for the next coordinator
        tick (parallel to HARD-15's per-zone entity refresh)."""
        await super().async_added_to_hass()
        entry = getattr(self.coordinator, "config_entry", None)
        if entry is None:
            return

        @callback
        def _on_names_updated() -> None:
            self.async_write_ha_state()

        self.async_on_remove(
            async_dispatcher_connect(
                self.hass,
                f"{SIGNAL_ZONE_NAMES_UPDATED}_{entry.entry_id}",
                _on_names_updated,
            )
        )

    @property
    def native_value(self) -> int:
        return math.ceil(self._total_precise())

    def _total_precise(self) -> float:
        return sum(
            rec.size_estimate_m2
            for rec in self.coordinator.zone_registry.zones.values()
            if rec.size_estimate_m2 is not None
        )

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        zones = self.coordinator.zone_registry.zones
        zone_ids = sorted(zones.keys())
        # `zone_names`: parallel list in the same order as `zone_ids`.
        # Uses the operator-chosen name from options when present, falls
        # back to `Zone #<id>` (the same fallback per-zone entities show
        # in their title, so a card can join id ↔ name unambiguously).
        # Comment on #88 (2026-07-10 13:05): "we may want a boundary
        # and zone_names for the zones_total_area we'll see".
        entry = getattr(self.coordinator, "config_entry", None)

        def _name(bid: int) -> str:
            if entry is not None:
                raw = _zone_raw_name(entry, bid)
                if raw:
                    return raw
            return f"Zone #{bid}"

        return {
            "zone_ids": zone_ids,
            "zone_names": [_name(bid) for bid in zone_ids],
            "area_precise": self._total_precise(),
        }


class NavimowPositionSensor(SensorEntity):
    """Robot position on the local map (FEAT-01).

    Decoupled from the coordinator tick: /location type 1 arrives every 2 s
    and is throttled to ~5 s in the coordinator before being pushed via
    dispatcher to this entity. Excluded from the recorder in the documented
    configuration (`recorder: exclude: entities:`) — otherwise ~3600 state
    changes per mowing run.

    This is NOT a `device_tracker`: the local (station-relative) meters
    coordinate system is not lat/lon. Downstream cards should read the
    `x`/`y`/`theta` attributes.
    """

    _attr_has_entity_name = True
    _attr_translation_key = "position"
    _attr_native_unit_of_measurement = UnitOfLength.METERS
    _attr_icon = "mdi:robot-mower"

    def __init__(self, coordinator: NavimowCoordinator) -> None:
        device = coordinator.device
        self._device_id = device.id
        self._position: dict[str, Any] | None = coordinator.position
        self._attr_unique_id = f"{DOMAIN}_{device.id}_position"
        self._attr_device_info = _device_info(coordinator)

    async def async_added_to_hass(self) -> None:
        self.async_on_remove(
            async_dispatcher_connect(
                self.hass,
                f"{SIGNAL_POSITION_UPDATE}_{self._device_id}",
                self._handle_position,
            )
        )

    @callback
    def _handle_position(self, position: dict[str, Any]) -> None:
        self._position = position
        self.async_write_ha_state()

    @property
    def available(self) -> bool:
        return self._position is not None

    @property
    def native_value(self) -> Any:
        return self._position.get("distance") if self._position else None

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        if not self._position:
            return None
        return {
            "x": self._position.get("x"),
            "y": self._position.get("y"),
            "theta": self._position.get("theta"),
            "vehicle_state": self._position.get("vehicle_state"),
        }
