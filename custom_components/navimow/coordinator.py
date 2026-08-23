"""DataUpdateCoordinator for Navimow integration."""

import copy
import logging
import time
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
from .run_tracker import STATE_IDLE as _TRACKER_STATE_IDLE
from .run_tracker import STATE_RUNNING as _TRACKER_STATE_RUNNING
from .run_tracker import Event as RunEvent
from .run_tracker import RunTracker
from .zone_registry import ZoneRegistry

# Map internal tracker Event.kind → HA event bus event name. The two constant
# families share their names (`EVENT_RUN_*` in both `const` and `run_tracker`)
# and are aliased apart at import: the tracker side is a kind, the const side is
# the domain-prefixed bus name. Keeps the HA-facing surface a pure translation,
# so a rename on either side lands in exactly one place.
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
        # Separate state-freshness clock. Attribute packets bump
        # `_last_mqtt_update` but not this one — otherwise a docked robot
        # receiving periodic attribute pushes would suppress the HTTP fallback
        # even while its state is genuinely stale.
        self._last_mqtt_state_update: float | None = None
        self._last_http_fetch: float | None = None
        self._last_data_source: str | None = None
        # Edge-triggers the MQTT disconnect WARNING / reconnect INFO pair, so a
        # routine 1 h outage produces one WARNING on entry and one INFO when the
        # SDK reports the WSS session back up, instead of ~120 identical lines.
        # True once the WARNING is out; False again once the paired INFO is.
        self._mqtt_disconnect_warned: bool = False
        # Debounces that WARNING so a routine sub-second token-refresh
        # reconnect spanning a tick does not raise it. Incremented on each tick
        # observing `is_connected=False`, reset to 0 on any tick observing True.
        self._mqtt_disconnect_ticks: int = 0
        # Live pose from the /realtimeDate/location channel, which the SDK does
        # not subscribe. Stored apart from `_last_state` so it does NOT feed the
        # HTTP fallback freshness logic.
        self.position: dict[str, Any] | None = None
        self.vehicle_state: int | None = None
        self._last_position_dispatch: float = 0.0
        # Mowing stats (type-2 items), cached across ticks: the /location
        # channel stops publishing type-2 while docked, so the last observed
        # values are kept rather than showing "unknown" until the next session.
        self.stats: dict[str, Any] | None = None
        # Layer-1 ordering guard: firmware `time` (epoch ms) of the last
        # accepted /location packet, tracked per stream — type-1 poses at ~2 s
        # and type-2 stats at ~30-90 s have independent cadences. The stamped
        # value is clamped to `now + FUTURE_TIMESTAMP_TOLERANCE_MS`, so a
        # future-stamped packet cannot poison the cursor indefinitely.
        # Content-level judgement belongs to the tracker, not here.
        self._last_accepted_time_type1: int | None = None
        self._last_accepted_time_type2: int | None = None
        # Consecutive-drop counters, one per stream. Increment on drop,
        # reset on any acceptance. A single WARNING fires when a counter
        # reaches `STALE_DROP_STREAK_TO_WARN` so an operator notices a
        # stuck cursor without log flooding.
        self._type1_drop_streak: int = 0
        self._type2_drop_streak: int = 0
        # Pure state machine turning the accepted /location stream into
        # run/zone events. Fed by `_handle_location_stats`,
        # `_handle_location_position` (on a vs change) and the update tick.
        self.run_tracker = RunTracker()
        # Capped history of closed runs (result, duration, zones, mst), exposed
        # as an attribute of `last_run_result` and restored from Store on setup.
        self.history: list[dict[str, Any]] = []
        # Most-recently-closed run's `run_finished` payload; drives the
        # `last_run_*` sensors (started/duration/result).
        self.last_finished_run: dict[str, Any] | None = None
        # Pure per-boundary registry, fed by `_forward_run_events` on
        # `run_finished` and rebuilt from `history` on restore. Holds no
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
        # Project the restored history onto the zone registry: the last
        # complete pass per zone wins `size_estimate`, so every value the
        # sensor platform reads is already correct before the first live packet
        # arrives. Guarded against a corrupt on-disk shape — if a run entry is
        # malformed (`zones` not a list, say) the projection cannot proceed but
        # restore must not crash; the registry stays empty and future
        # `run_finished` events re-populate it as sessions close.
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

    def _tracker_persist_fingerprint(self) -> tuple[Any, Any, Any]:
        """The persisted fields a silent vehicle-state transition can move:
        `(state, vehicle_state, dock_arrival_time)`.

        The persisted `vehicle_state` drives the sustained-timer re-arm after a
        restart, so a docked idle↔charge flip that moves it has to be saved even
        when `tracker.state` did not move. Keying the silent save on a `state`
        change alone leaves `vehicle_state` stale, and a restart could then
        re-arm the timer on a stale `vs = 1` and mint a spurious `interrupted`
        close while the robot was charging, mapping, or already departed. The
        coordinator compares this tuple before and after a forwarded transition,
        and saves when it moved.
        """
        run = self.run_tracker.current_run
        return (
            self.run_tracker.state,
            self.run_tracker.vehicle_state,
            run.get("dock_arrival_time") if run else None,
        )

    # === /location channel (real-time pose + mowing stats) ===

    @callback
    def handle_location_item(self, item: dict[str, Any]) -> None:
        """Route one item from the /location payload array.

        The payload is a JSON array discriminated by `type`:
        - type 1 = pose (postureX/Y/Theta + vehicleState) ~every 2 s
        - type 2 = mowing stats ~every 30-90 s
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

        A packet stamped anomalously far in the future (a content/timestamp
        mismatch, or a robot RTC skewed ahead) is still accepted — content-level
        judgement belongs to the tracker — but the cursor it stamps is clamped,
        so a subsequent stream of legitimate present-time packets self-heals the
        guard within the tolerance window.
        """
        now_ms = int(time.time() * 1000)
        return min(incoming_time_ms, now_ms + FUTURE_TIMESTAMP_TOLERANCE_MS)

    @callback
    def _handle_location_stats(self, item: dict[str, Any]) -> None:
        parsed = parse_location_type_2(item)
        if parsed is None:
            return
        # Layer-1: drop items whose firmware `time` is not strictly greater than
        # the last accepted type-2's — ordering regressions and duplicates.
        # Ordering only; content-level checks belong to the tracker. Skipped
        # when `time` is missing (never observed on i210 over ~180 committed
        # packets, but the parser accepts the shape).
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
        # Feed the run tracker downstream of layer-1, so it only ever sees
        # ordering-clean packets.
        fingerprint_before = self._tracker_persist_fingerprint()
        run_events = self.run_tracker.process_type2(parsed)
        self._forward_run_events(run_events)
        # A departure-gated resume (PAUSED_DOCKED → RUNNING, dock stamp
        # cleared) emits no run event; persist that silent transition too,
        # symmetric with the type-1 path and keyed on the same
        # `(state, vehicle_state, dock_arrival_time)` delta. When an event WAS
        # emitted, the forward above already saved.
        if (
            not run_events
            and self.run_tracker.state != _TRACKER_STATE_IDLE
            and self._tracker_persist_fingerprint() != fingerprint_before
        ):
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

        # Same ordering guard on the type-1 stream, with its own cursor: the two
        # streams have distinct cadences (~2 s vs ~30-90 s), and a single shared
        # cursor would drop the whole slower stream after every faster-stream
        # update.
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
            # Forward the vs change so the tracker can move an open run into
            # PAUSED_DOCKED and arm the sustained interruption timer. The
            # type-1 `time` goes with it: the tracker anchors a provisional
            # run's `start_time` on the vs=4 activation edge, and stamps the
            # wander end on dock entry.
            fingerprint_before = self._tracker_persist_fingerprint()
            run_events = self.run_tracker.process_vehicle_state(
                vehicle_state, time_ms=parsed.get("time")
            )
            self._forward_run_events(run_events)
            # Any accepted type-1 that moves the persisted
            # `(state, vehicle_state, dock_arrival_time)` tuple while a run is
            # open — a dock entry, a docked idle↔charge flip, a departure edge —
            # closes nothing, so it emits no run event and schedules no save
            # (the heartbeat save runs only while RUNNING). Persist it anyway,
            # so the stamp AND the fresh `vehicle_state` survive a restart
            # between the edge and the close. Keyed on the tuple delta rather
            # than on `state` alone, or a docked flip that moves only
            # `vehicle_state` is lost — see `_tracker_persist_fingerprint`.
            # Delta-keyed rather than per-type-1: the save is fire-and-forget
            # with no debounce and type-1 arrives every ~2 s, so it must fire on
            # edges only. At rest the persisted `vehicle_state` drives nothing,
            # so IDLE is skipped.
            if (
                not run_events
                and self.run_tracker.state != _TRACKER_STATE_IDLE
                and self._tracker_persist_fingerprint() != fingerprint_before
            ):
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
                # Fold this run into the zone registry and announce first-time
                # boundaries, so the sensor platform can lazy-add its per-zone
                # entities.
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
            # Apply the SDK's cached /state as-is, battery included: the MQTT
            # /state battery is authoritative again.
            self._last_state = cached_state
            self._last_data_source = "mqtt_cache"

        cached_attrs = self.sdk.get_cached_attributes(self.device.id)
        if cached_attrs is not None:
            self._last_attributes = cached_attrs

        now = time.monotonic()
        # State-specific freshness: attribute packets can arrive periodically
        # while the vehicle state is genuinely stale, so the catch-all
        # `_last_mqtt_update` here would suppress the HTTP fallback and leave HA
        # showing old state indefinitely.
        is_state_stale = (
            self._last_mqtt_state_update is None
            or now - self._last_mqtt_state_update > MQTT_STALE_SECONDS
        )
        can_http_fetch = (
            self._last_http_fetch is None
            or now - self._last_http_fetch > HTTP_FALLBACK_MIN_INTERVAL
        )
        # Edge-triggered MQTT connectivity log: WARNING when the WSS is first
        # noticed down AND the state has aged past the stale threshold (an
        # actionable outage, not a reconnect blip), INFO when the SDK reports it
        # back up. Keeps a 1 h outage to two lines instead of ~120, and
        # decouples "connectivity recovered" from "state is fresh again", so a
        # lingering HTTP-fallback-only mode still reports the reconnect the
        # moment it happens.
        #
        # The WARN is further debounced by a counter of consecutive
        # `is_connected=False` ticks, so a routine sub-second reconnect (the
        # ~40 min token refresh) that spans a tick does not raise it. The
        # counter resets on any True observation.
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
        # Tick the tracker so the sustained-dock interruption detector fires
        # even when no MQTT traffic is arriving — catching a run that has
        # silently ended is the whole point of the timer.
        self._forward_run_events(self.run_tracker.tick())
        # Heartbeat Store save while a run is open. Every tracker transition
        # already saves through `_forward_run_events`; this is the
        # between-transition backstop for a hard crash mid-run, throttled by
        # `TRACKER_HEARTBEAT_SECONDS` — never per-tick.
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
        # Accept the MQTT /state push as-is, battery included.
        self._last_state = state
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
