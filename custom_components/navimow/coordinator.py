"""DataUpdateCoordinator for Navimow integration."""

import copy
import logging
import time
from dataclasses import replace
from datetime import timedelta
from typing import Any

from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers import config_entry_oauth2_flow
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.storage import Store
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator
from mower_sdk.api import MowerAPI
from mower_sdk.models import (
    Device,
    DeviceAttributesMessage,
    DeviceStateMessage,
    DeviceStatus,
)
from mower_sdk.sdk import NavimowSDK

from .const import (
    DOMAIN,
    EVENT_RUN_FINISHED,
    EVENT_RUN_STARTED,
    FUTURE_TIMESTAMP_TOLERANCE_MS,
    HISTORY_MAX,
    HTTP_FALLBACK_MIN_INTERVAL,
    MQTT_DISCONNECT_TICKS_TO_WARN,
    MQTT_STALE_SECONDS,
    POSITION_THROTTLE_SECONDS,
    SIGNAL_POSITION_UPDATE,
    SIGNAL_ZONE_DISCOVERED,
    STALE_DROP_STREAK_TO_WARN,
    STORE_VERSION,
    TRACKER_HEARTBEAT_SECONDS,
    UPDATE_INTERVAL,
)
from .location import parse_location_type_1, parse_location_type_2
from .run_tracker import EVENT_RUN_FINISHED as _TRACKER_EVENT_RUN_FINISHED
from .run_tracker import EVENT_RUN_STARTED as _TRACKER_EVENT_RUN_STARTED
from .run_tracker import STATE_RUNNING as _TRACKER_STATE_RUNNING
from .run_tracker import Event as RunEvent
from .run_tracker import RunTracker
from .zone_registry import ZoneRegistry

# Map internal tracker Event.kind → HA event bus event name. Keeps the
# HA-facing surface a pure translation, so a future rename on either
# side lands in exactly one place. The `run_reopened` kind was retired
# by FEAT-06 (#54) — the tracker now emits `run_started` for a new
# session and never resurrects a closed run.
_TRACKER_KIND_TO_HA_EVENT = {
    _TRACKER_EVENT_RUN_STARTED: EVENT_RUN_STARTED,
    _TRACKER_EVENT_RUN_FINISHED: EVENT_RUN_FINISHED,
}

_LOGGER = logging.getLogger(__name__)


class NavimowCoordinator(DataUpdateCoordinator[dict[str, Any]]):
    """Coordinator for Navimow data updates."""

    def __init__(
        self,
        hass: HomeAssistant,
        sdk: NavimowSDK,
        api: MowerAPI,
        device: Device,
        oauth_session: config_entry_oauth2_flow.OAuth2Session | None = None,
    ) -> None:
        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            update_interval=timedelta(seconds=UPDATE_INTERVAL),
        )
        self.sdk = sdk
        self.api = api
        self.device = device
        self.oauth_session = oauth_session
        self.data: dict[str, Any] = {}
        self._last_state: DeviceStateMessage | None = None
        self._last_attributes: DeviceAttributesMessage | None = None
        self._last_mqtt_update: float | None = None
        # Separate state-freshness clock (BUG-03). Attribute packets bump
        # `_last_mqtt_update` but not this one — otherwise a docked robot
        # receiving periodic attribute pushes would suppress the HTTP
        # fallback even while its state is genuinely stale.
        self._last_mqtt_state_update: float | None = None
        self._last_http_fetch: float | None = None
        self._last_data_source: str | None = None
        # BUG-01: edge-trigger the MQTT disconnect WARNING/reconnect INFO
        # pair, so a routine 1 h outage produces one WARNING (on entry) and
        # one INFO (when the SDK reports the WSS session back up), not
        # ~120 identical lines. Flag flips True once we have emitted the
        # WARNING; flips False once we have emitted the paired INFO.
        self._mqtt_disconnect_warned: bool = False
        # HARD-04: debounce the WARNING so a routine sub-second token-refresh
        # reconnect that happens to span a tick does not raise it. Counter
        # increments each tick that observes `is_connected=False`, resets to
        # 0 on any tick that observes True. WARN fires only when the counter
        # reaches MQTT_DISCONNECT_TICKS_TO_WARN.
        self._mqtt_disconnect_ticks: int = 0
        # FEAT-01: live pose from the /realtimeDate/location channel that
        # the SDK does not subscribe. Stored separately from `_last_state`
        # so it does NOT interfere with the HTTP fallback freshness logic.
        self.position: dict[str, Any] | None = None
        self.vehicle_state: int | None = None
        self._last_position_dispatch: float = 0.0
        # FEAT-02: mowing stats (type 2 items). Cached across ticks: the
        # /location channel stops publishing type 2 while docked, so we keep
        # the last observed values rather than showing "unknown" until the
        # next mowing session.
        self.stats: dict[str, Any] | None = None
        # FEAT-05 layer-1 guard: firmware `time` (epoch ms) of the last
        # accepted /location packet, tracked per stream (type-1 poses at
        # ~2 s and type-2 stats at ~30-90 s have independent cadences).
        # Same pathology family as BUG-05 on /state; the tracker in step (b)
        # layers `wk` monotonicity + wk₀+sub invariant on top. In-memory
        # only in (a); persistence arrives in (c). The stamped value is
        # clamped to `now + FUTURE_TIMESTAMP_TOLERANCE_MS` to keep a
        # future-stamped packet from poisoning the cursor indefinitely.
        self._last_accepted_time_type1: int | None = None
        self._last_accepted_time_type2: int | None = None
        # Consecutive-drop counters, one per stream. Increment on drop,
        # reset on any acceptance. A single WARNING fires when a counter
        # reaches `STALE_DROP_STREAK_TO_WARN` so an operator notices a
        # stuck cursor without log flooding.
        self._type1_drop_streak: int = 0
        self._type2_drop_streak: int = 0
        # FEAT-05 (b): pure state machine that turns the accepted
        # /location stream into run/zone events. Fed by
        # `_handle_location_stats`, `_handle_location_position` (on vs
        # change), and `_async_update_data.tick()`.
        self.run_tracker = RunTracker()
        # FEAT-05 (c): capped history of closed runs (result, duration,
        # zones, mst) — exposed as an attribute of `last_run_result` for
        # the future custom card, restored from Store on setup.
        self.history: list[dict[str, Any]] = []
        # Most-recently-closed run's `run_finished` payload; drives the
        # `last_run_*` sensors (started/duration/result).
        self.last_finished_run: dict[str, Any] | None = None
        # FEAT-04 PR 2: pure per-boundary registry, fed by `_forward_run_events`
        # on `run_finished` and rebuilt from `history` on restore. Holds no
        # persisted state of its own — the projection is complete every boot.
        self.zone_registry = ZoneRegistry()
        # `homeassistant.helpers.storage.Store` instance, created on
        # `async_setup` once the device id is known.
        self._store: Store | None = None
        self._last_store_save_monotonic: float = 0.0

    async def async_setup(self) -> None:
        """Restore persistence + register callbacks from SDK.

        Restore happens *before* subscribing to the SDK so the tracker
        and the layer-1 cursors see live packets against the last
        known state, not against a cold-boot IDLE.
        """
        await self._async_restore_store()
        self.sdk.on_state(self._handle_state)
        self.sdk.on_attributes(self._handle_attributes)

    async def _async_restore_store(self) -> None:
        """Load the run tracker + cursors + history from Store."""
        self._store = Store(
            self.hass,
            STORE_VERSION,
            f"{DOMAIN}.{self.device.id}.run_tracker",
        )
        try:
            payload = await self._store.async_load()
        except Exception as err:  # noqa: BLE001
            _LOGGER.warning(
                "run_tracker store load failed for %s: %s", self.device.id, err
            )
            return
        if not payload:
            return
        tracker_snap = payload.get("tracker")
        if tracker_snap and not self.run_tracker.restore(tracker_snap):
            _LOGGER.warning(
                "run_tracker snapshot version mismatch for %s — discarding",
                self.device.id,
            )
        cursors = payload.get("cursors") or {}
        self._last_accepted_time_type1 = cursors.get("type1")
        self._last_accepted_time_type2 = cursors.get("type2")
        history = payload.get("history") or []
        # Trust the on-disk order but re-cap defensively in case a prior
        # release stored more than HISTORY_MAX (or the cap has since
        # dropped).
        self.history = list(history[-HISTORY_MAX:])
        self.last_finished_run = payload.get("last_finished_run")
        # FEAT-04 PR 2: project the restored history onto the zone registry.
        # The last complete pass per zone wins `size_estimate`, so every
        # value the sensor platform (PR 3) will read is already correct
        # before the first live packet arrives. Guarded against a corrupt
        # on-disk shape: if a run entry is malformed (e.g. `zones` is not a
        # list) the projection cannot proceed, but restore must not crash —
        # the registry stays empty and future `run_finished` events will
        # re-populate it as sessions close.
        try:
            self.zone_registry.rebuild(self.history)
        except Exception as err:  # noqa: BLE001
            _LOGGER.warning(
                "zone_registry rebuild failed for %s (corrupt history?); "
                "registry starts empty and will re-populate on next run_finished: %s",
                self.device.id,
                err,
            )
            self.zone_registry = ZoneRegistry()

    def _build_store_payload(self) -> dict[str, Any]:
        # `snapshot()` already deep-copies `current_run` for us; the
        # history / last_finished_run are cheap to deep-copy at save
        # time and the copy decouples the fire-and-forget Store save
        # (which serialises in an executor) from any subsequent mutation
        # on the HA loop.
        return {
            "tracker": self.run_tracker.snapshot(),
            "cursors": {
                "type1": self._last_accepted_time_type1,
                "type2": self._last_accepted_time_type2,
            },
            "history": copy.deepcopy(self.history),
            "last_finished_run": copy.deepcopy(self.last_finished_run),
        }

    def _schedule_store_save(self) -> None:
        """Fire-and-forget save. Never awaited from the tracker path so
        MQTT dispatch is not gated on disk I/O.
        """
        if self._store is None:
            return
        self.hass.async_create_task(self._store.async_save(self._build_store_payload()))
        self._last_store_save_monotonic = time.monotonic()

    # === /location channel (real-time pose + mowing stats) — FEAT-01 ===

    @callback
    def handle_location_item(self, item: dict[str, Any]) -> None:
        """Route one item from the /location payload array.

        The payload is a JSON array discriminated by `type`:
        - type 1 = pose (postureX/Y/Theta + vehicleState) ~every 2 s
        - type 2 = mowing stats ~every 30-90 s (FEAT-02)
        Types 3/4 (heartbeat, taskDelay) ignored.
        """
        msg_type = item.get("type")
        if msg_type == 1:
            self._handle_location_position(item)
        elif msg_type == 2:
            self._handle_location_stats(item)

    def _clamp_cursor(self, incoming_time_ms: int) -> int:
        """Cap a firmware timestamp at `now + FUTURE_TIMESTAMP_TOLERANCE_MS`
        before storing it as an ordering cursor.

        A packet stamped anomalously far in the future (BUG-08-style
        content/timestamp mismatch, or a robot RTC skewed ahead) is still
        accepted — content-level judgement belongs to the step-(b) tracker
        layers — but the cursor it stamps is clamped, so a subsequent
        stream of legitimate (present-time) packets self-heals the guard
        within the tolerance window.
        """
        now_ms = int(time.time() * 1000)
        return min(incoming_time_ms, now_ms + FUTURE_TIMESTAMP_TOLERANCE_MS)

    @callback
    def _handle_location_stats(self, item: dict[str, Any]) -> None:
        parsed = parse_location_type_2(item)
        if parsed is None:
            return
        # FEAT-05 layer-1: drop items whose firmware `time` is not strictly
        # greater than the last accepted type-2's — catches ordering
        # regressions and duplicates. Guard is intentionally ordering-only;
        # the tracker in step (b) layers `wk` monotonicity + wk₀+sub
        # invariant on top for content-level checks. Guard is skipped when
        # `time` is missing (defensive tolerance — never observed on i210
        # over ~180 committed packets but the parser accepts the shape).
        incoming_time = parsed.get("time")
        if incoming_time is not None:
            last_time = self._last_accepted_time_type2
            if last_time is not None and incoming_time <= last_time:
                self._type2_drop_streak += 1
                _LOGGER.debug(
                    "MQTT location type-2 DROPPED as stale (time=%s <= last=%s) device=%s",
                    incoming_time,
                    last_time,
                    self.device.id,
                )
                if self._type2_drop_streak == STALE_DROP_STREAK_TO_WARN:
                    _LOGGER.warning(
                        "MQTT location type-2 dropped %d consecutive packets as stale "
                        "for device %s; cursor may be poisoned by a future-stamped "
                        "packet — will self-heal within ~%ds of a legitimate packet",
                        self._type2_drop_streak,
                        self.device.id,
                        FUTURE_TIMESTAMP_TOLERANCE_MS // 1000,
                    )
                return
            self._last_accepted_time_type2 = self._clamp_cursor(incoming_time)
            self._type2_drop_streak = 0
        self.stats = parsed
        # FEAT-05 (b): feed the run tracker downstream of layer-1 so it
        # only sees ordering-clean packets. Emitted events are just
        # logged here; step (c) will fire them on the HA event bus.
        tracker_state_before = self.run_tracker.state
        run_events = self.run_tracker.process_type2(parsed)
        self._forward_run_events(run_events)
        # HARD-19 §4 (#120): a departure-gated resume (PAUSED_DOCKED →
        # RUNNING, dock stamp cleared) emits no run event; persist that
        # silent transition too, symmetric with the dock-entry save on the
        # type-1 path. When an event WAS emitted the forward above saved.
        if not run_events and self.run_tracker.state != tracker_state_before:
            self._schedule_store_save()
        # Stats belong to the coordinator's shared data dict, so refresh
        # entities via the standard path (they are on the ~30 s tick anyway;
        # this just makes updates land immediately when a payload arrives).
        self.async_set_updated_data(self._build_data())

    @callback
    def _handle_location_position(self, item: dict[str, Any]) -> None:
        parsed = parse_location_type_1(item)
        if parsed is None:
            return

        # FEAT-05 layer-1: same ordering guard on the type-1 stream. Cursor
        # is independent from type-2 (`_last_accepted_time_type2`) because
        # the two streams have distinct cadences (~2 s vs ~30-90 s) and a
        # single shared cursor would drop the whole slower stream after
        # every faster-stream update.
        incoming_time = parsed.get("time")
        if incoming_time is not None:
            last_time = self._last_accepted_time_type1
            if last_time is not None and incoming_time <= last_time:
                self._type1_drop_streak += 1
                _LOGGER.debug(
                    "MQTT location type-1 DROPPED as stale (time=%s <= last=%s) device=%s",
                    incoming_time,
                    last_time,
                    self.device.id,
                )
                if self._type1_drop_streak == STALE_DROP_STREAK_TO_WARN:
                    _LOGGER.warning(
                        "MQTT location type-1 dropped %d consecutive packets as stale "
                        "for device %s; cursor may be poisoned by a future-stamped "
                        "packet — will self-heal within ~%ds of a legitimate packet",
                        self._type1_drop_streak,
                        self.device.id,
                        FUTURE_TIMESTAMP_TOLERANCE_MS // 1000,
                    )
                return
            self._last_accepted_time_type1 = self._clamp_cursor(incoming_time)
            self._type1_drop_streak = 0

        self.position = parsed
        vehicle_state = parsed["vehicle_state"]

        # A vehicleState change (e.g. transition to charging = 2) must refresh
        # the CoordinatorEntity subscribers immediately (binary_sensor en_charge,
        # etc.).
        vs_changed = vehicle_state is not None and vehicle_state != self.vehicle_state
        if vs_changed:
            self.vehicle_state = vehicle_state
            # FEAT-05 (b): forward the vs change to the tracker so it can
            # move an open run into PAUSED_DOCKED / arm the sustained-60 s
            # interruption timer.
            # HARD-18 (#117): also pass the type-1 `time` so the tracker
            # can anchor a provisional run's `start_time` on the vs=4
            # activation edge (and stamp the wander end on dock entry).
            tracker_state_before = self.run_tracker.state
            run_events = self.run_tracker.process_vehicle_state(
                vehicle_state, time_ms=parsed.get("time")
            )
            self._forward_run_events(run_events)
            # HARD-19 §4 (#120): a dock entry (RUNNING → PAUSED_DOCKED,
            # dock_arrival_time stamped) closes nothing, so it emits no run
            # event and `_forward_run_events` schedules no save; the
            # heartbeat save only runs while RUNNING. Persist the silent
            # transition so the stamp (and the PAUSED_DOCKED context) survives
            # a restart between the dock edge and the close — this also
            # repairs the pre-existing mid-pause restart imprecision. When an
            # event WAS emitted the forward above already saved.
            if not run_events and self.run_tracker.state != tracker_state_before:
                self._schedule_store_save()
            self.async_set_updated_data(self._build_data())

        # Position pushes go through a dedicated dispatcher (throttled to
        # POSITION_THROTTLE_SECONDS unless vehicleState changed) so we don't
        # emit ~3600 state changes per mowing run through the coordinator.
        now = time.monotonic()
        if (
            vs_changed
            or (now - self._last_position_dispatch) >= POSITION_THROTTLE_SECONDS
        ):
            self._last_position_dispatch = now
            async_dispatcher_send(
                self.hass,
                f"{SIGNAL_POSITION_UPDATE}_{self.device.id}",
                self.position,
            )

    def _build_data(self) -> dict[str, Any]:
        return {
            "device": self.device,
            "state": self._last_state,
            "attributes": self._last_attributes,
            "meta": {
                "last_data_source": self._last_data_source,
                "last_mqtt_update_monotonic": self._last_mqtt_update,
                "last_mqtt_state_update_monotonic": self._last_mqtt_state_update,
                "last_http_fetch_monotonic": self._last_http_fetch,
            },
        }

    def _forward_run_events(self, events: list[RunEvent]) -> None:
        """Consume events emitted by the tracker.

        Each event: (1) DEBUG-logged for tracing; (2) fired on the HA
        event bus so automations can react; (3) if it is a
        `run_finished`, appended to the capped history + promoted to
        `last_finished_run`; (4) triggers a Store save so the on-disk
        state stays consistent with the visible state.
        """
        if not events:
            return
        for event in events:
            _LOGGER.debug(
                "run_tracker event: kind=%s payload=%s", event.kind, event.payload
            )
            ha_event = _TRACKER_KIND_TO_HA_EVENT.get(event.kind)
            if ha_event is not None:
                self.hass.bus.async_fire(
                    ha_event,
                    {**event.payload, "device_id": self.device.id},
                )
            if event.kind == _TRACKER_EVENT_RUN_FINISHED:
                entry = dict(event.payload)
                self.history.append(entry)
                if len(self.history) > HISTORY_MAX:
                    # FIFO trim — keep the most recent HISTORY_MAX entries.
                    self.history = self.history[-HISTORY_MAX:]
                self.last_finished_run = entry
                # FEAT-04 PR 2: fold this run into the zone registry and
                # announce first-time boundaries so the sensor platform
                # (PR 3) can lazy-add its per-zone entities. No listener
                # exists yet in PR 2 — the dispatch is a documented no-op
                # until then.
                for boundary_id in self.zone_registry.ingest_run(entry):
                    async_dispatcher_send(
                        self.hass,
                        f"{SIGNAL_ZONE_DISCOVERED}_{self.device.id}",
                        boundary_id,
                    )
        self._schedule_store_save()

    def _device_status_to_state(self, status: DeviceStatus) -> DeviceStateMessage:
        error: dict[str, Any] | None = None
        if status.error_code and status.error_code.value != "none":
            error = {
                "code": status.error_code.value,
                "message": status.error_message,
            }
        return DeviceStateMessage(
            device_id=status.device_id,
            timestamp=status.timestamp,
            state=status.status.value,
            battery=status.battery,
            signal_strength=status.signal_strength,
            position=status.position,
            error=error,
            metrics=None,
        )

    async def _async_ensure_valid_token(self) -> str | None:
        if not self.oauth_session:
            return None
        try:
            token: dict[str, Any] | None
            if hasattr(self.oauth_session, "async_ensure_token_valid"):
                await self.oauth_session.async_ensure_token_valid()
                token = self.oauth_session.token
            elif hasattr(self.oauth_session, "async_get_valid_token"):
                token = await self.oauth_session.async_get_valid_token()
            else:
                token = self.oauth_session.token
        except ConfigEntryAuthFailed:
            # Deterministic auth failure (refresh_token missing or rejected by the server) -> surface it directly so HA guides the user through re-authentication
            raise
        except Exception as err:
            # Transient error (network timeout, DNS, etc.) -> do not trigger the re-authentication flow immediately.
            # Try to reuse the cached access_token; only escalate to an auth failure if the cache is unavailable too.
            _LOGGER.warning(
                "Token refresh failed (likely transient), falling back to cached token: %s",
                err,
            )
            cached = getattr(self.oauth_session, "token", None)
            if cached and cached.get("access_token"):
                token = cached
            else:
                raise ConfigEntryAuthFailed(
                    f"Token refresh failed and no cached token available: {err}"
                ) from err
        if not token or not token.get("access_token"):
            raise ConfigEntryAuthFailed("No access token after refresh")
        access_token = token["access_token"]
        self.api.set_token(access_token)
        return access_token

    async def _async_update_data(self) -> dict[str, Any]:
        # Refresh the token on every update so api._token stays in sync with oauth_session.
        # If we only refreshed during the HTTP fallback, the token would go stale while MQTT is pushing data normally,
        # and once expired a user command would immediately get CODE_OAUTH_INFO_ILLEGAL.
        try:
            await self._async_ensure_valid_token()
        except ConfigEntryAuthFailed:
            raise

        cached_state = self.sdk.get_cached_state(self.device.id)
        if cached_state is not None:
            # BUG-08: HTTP is the source of truth for `battery`. The SDK's
            # cached /state can carry a stale battery reading (e.g. battery=0
            # from a past over-discharge, battery=100 forwarded by the cloud
            # mid-mow) that would clobber the fresh HTTP value on every tick.
            # We still take the other fields (state, error, position,
            # signal_strength, timestamp) from the cache — the trace shows
            # they stay coherent with reality — but we thread the previously
            # held battery back into a fresh object. `replace()` is essential
            # here: the SDK holds `cached_state` by reference in its own cache
            # dict and hands the same reference to the callback, so any
            # in-place mutation would corrupt `sdk._state_cache` from another
            # thread.
            prev_battery = (
                self._last_state.battery if self._last_state is not None else None
            )
            self._last_state = (
                replace(cached_state, battery=prev_battery)
                if prev_battery is not None
                else cached_state
            )
            self._last_data_source = "mqtt_cache"

        cached_attrs = self.sdk.get_cached_attributes(self.device.id)
        if cached_attrs is not None:
            self._last_attributes = cached_attrs

        now = time.monotonic()
        # Use state-specific freshness (BUG-03). Attribute packets can arrive
        # periodically while vehicle state is genuinely stale — using the
        # catch-all `_last_mqtt_update` here would suppress the HTTP fallback
        # and leave HA showing old state indefinitely.
        is_state_stale = (
            self._last_mqtt_state_update is None
            or now - self._last_mqtt_state_update > MQTT_STALE_SECONDS
        )
        can_http_fetch = (
            self._last_http_fetch is None
            or now - self._last_http_fetch > HTTP_FALLBACK_MIN_INTERVAL
        )
        # Edge-triggered MQTT connectivity log — WARNING when we first
        # notice the WSS is down AND the state has aged past the stale
        # threshold (i.e. this is an actionable outage, not a routine
        # reconnect blip), INFO when the SDK reports the WSS back up.
        # Prevents log spam (~120 identical lines over a 1 h outage) and
        # decouples "connectivity recovered" from "state is fresh again"
        # so a lingering HTTP-fallback-only mode still reports the
        # reconnect the moment it happens. (BUG-01)
        #
        # HARD-04 extends this: the WARN is further debounced by a counter
        # of consecutive `is_connected=False` ticks, so a routine sub-second
        # reconnect (~40 min token refresh, per FEAT-03 diag) that spans a
        # tick does not raise it. The counter resets on any True observation.
        if not self.sdk.is_connected:
            self._mqtt_disconnect_ticks += 1
        else:
            if self._mqtt_disconnect_warned:
                _LOGGER.info("MQTT reconnected for device %s", self.device.id)
                self._mqtt_disconnect_warned = False
            self._mqtt_disconnect_ticks = 0

        if (
            not self._mqtt_disconnect_warned
            and self._mqtt_disconnect_ticks >= MQTT_DISCONNECT_TICKS_TO_WARN
            and is_state_stale
        ):
            _LOGGER.warning(
                "MQTT appears disconnected for device %s; relying on HTTP fallback",
                self.device.id,
            )
            self._mqtt_disconnect_warned = True

        if is_state_stale and can_http_fetch:
            try:
                status = await self.api.async_get_device_status(self.device.id)
                self._last_state = self._device_status_to_state(status)
                self._last_http_fetch = now
                self._last_data_source = "http_fallback"
                _LOGGER.info(
                    "HTTP fallback succeeded for device %s (MQTT stale)",
                    self.device.id,
                )
            except ConfigEntryAuthFailed:
                raise
            except Exception as err:
                _LOGGER.warning(
                    "HTTP fallback failed for device %s: %s", self.device.id, err
                )

        _LOGGER.debug(
            "Coordinator update: device=%s source=%s mqtt_ts=%s mqtt_state_ts=%s http_ts=%s",
            self.device.id,
            self._last_data_source,
            self._last_mqtt_update,
            self._last_mqtt_state_update,
            self._last_http_fetch,
        )
        # FEAT-05 (b): tick the tracker so the sustained-docked
        # interruption detector fires even when no MQTT traffic is
        # arriving (the whole point of the timer is to catch a run that
        # has silently ended).
        self._forward_run_events(self.run_tracker.tick())
        # FEAT-05 (c): heartbeat Store save while a run is open. Every
        # tracker transition already saves through `_forward_run_events`;
        # this is the between-transition backstop for a hard crash mid-
        # run. `TRACKER_HEARTBEAT_SECONDS` throttles it — never per-tick.
        if (
            self.run_tracker.state == _TRACKER_STATE_RUNNING
            and (time.monotonic() - self._last_store_save_monotonic)
            >= TRACKER_HEARTBEAT_SECONDS
        ):
            self._schedule_store_save()
        self.data = self._build_data()
        return self.data

    def _handle_state(self, state: DeviceStateMessage) -> None:
        if state.device_id != self.device.id:
            return
        _LOGGER.debug(
            "MQTT state received: device=%s state=%s battery=%s",
            state.device_id,
            state.state,
            state.battery,
        )
        now = time.monotonic()
        self._last_mqtt_update = now
        self._last_mqtt_state_update = now
        self._last_data_source = "mqtt_push"
        self.hass.loop.call_soon_threadsafe(self._update_from_state, state)

    def _handle_attributes(self, attrs: DeviceAttributesMessage) -> None:
        if attrs.device_id != self.device.id:
            return
        _LOGGER.debug(
            "MQTT attributes received: device=%s keys=%d",
            attrs.device_id,
            len(getattr(attrs, "__dict__", {}) or {}),
        )
        self._last_mqtt_update = time.monotonic()
        self.hass.loop.call_soon_threadsafe(self._update_from_attributes, attrs)

    def _update_from_state(self, state: DeviceStateMessage) -> None:
        # BUG-08: HTTP is the source of truth for `battery`. The MQTT /state
        # topic occasionally forwards a stale battery reading — same class
        # of clobbering as BUG-04's SDK cache, hitting the callback path
        # instead of the poll path. Preserve the previously held battery so
        # only the HTTP fallback ever writes it. `replace()` is essential
        # here: the SDK caches `state` by reference before invoking the
        # callback, so an in-place mutation would corrupt `sdk._state_cache`
        # from the HA loop thread.
        prev_battery = (
            self._last_state.battery if self._last_state is not None else None
        )
        self._last_state = (
            replace(state, battery=prev_battery) if prev_battery is not None else state
        )
        self._last_data_source = "mqtt_push"
        self.async_set_updated_data(self._build_data())

    def _update_from_attributes(self, attrs: DeviceAttributesMessage) -> None:
        self._last_attributes = attrs
        self.async_set_updated_data(self._build_data())

    def get_device_state(self) -> DeviceStateMessage | None:
        return self.data.get("state")

    def get_device_attributes(self) -> DeviceAttributesMessage | None:
        return self.data.get("attributes")

    def get_device_info(self) -> Any | None:
        return self.data.get("device")
