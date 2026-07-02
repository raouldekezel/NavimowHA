"""DataUpdateCoordinator for Navimow integration."""

import logging
import time
from datetime import timedelta
from typing import Any

from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers import config_entry_oauth2_flow
from homeassistant.helpers.dispatcher import async_dispatcher_send
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
    HTTP_FALLBACK_MIN_INTERVAL,
    MQTT_STALE_SECONDS,
    POSITION_THROTTLE_SECONDS,
    SIGNAL_POSITION_UPDATE,
    UPDATE_INTERVAL,
)
from .location import parse_location_type_1, parse_location_type_2

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

    async def async_setup(self) -> None:
        """Register callbacks from SDK."""
        self.sdk.on_state(self._handle_state)
        self.sdk.on_attributes(self._handle_attributes)

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

    @callback
    def _handle_location_stats(self, item: dict[str, Any]) -> None:
        parsed = parse_location_type_2(item)
        if parsed is None:
            return
        self.stats = parsed
        # Stats belong to the coordinator's shared data dict, so refresh
        # entities via the standard path (they are on the ~30 s tick anyway;
        # this just makes updates land immediately when a payload arrives).
        self.async_set_updated_data(self._build_data())

    @callback
    def _handle_location_position(self, item: dict[str, Any]) -> None:
        parsed = parse_location_type_1(item)
        if parsed is None:
            return

        self.position = parsed
        vehicle_state = parsed["vehicle_state"]

        # A vehicleState change (e.g. transition to charging = 2) must refresh
        # the CoordinatorEntity subscribers immediately (binary_sensor en_charge,
        # etc.).
        vs_changed = vehicle_state is not None and vehicle_state != self.vehicle_state
        if vs_changed:
            self.vehicle_state = vehicle_state
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
            # 确定性认证失败（refresh_token 缺失或被服务端拒绝）→ 直接上报，让 HA 引导用户重新认证
            raise
        except Exception as err:
            # 瞬态错误（网络超时、DNS 等）→ 不立即触发重新认证流程。
            # 尝试沿用缓存中的 access_token；若缓存也不可用才升级为认证失败。
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
        # 每次 update 都主动刷新 token，确保 api._token 与 oauth_session 保持同步。
        # 若仅在 HTTP fallback 时刷新，MQTT 正常推数据期间 token 长期不更新，
        # 过期后用户下发指令会立即收到 CODE_OAUTH_INFO_ILLEGAL。
        try:
            await self._async_ensure_valid_token()
        except ConfigEntryAuthFailed:
            raise

        cached_state = self.sdk.get_cached_state(self.device.id)
        if cached_state is not None:
            # BUG-04: don't overwrite a fresher HTTP fallback state with the
            # SDK's cached MQTT state. The SDK caches the last MQTT push
            # indefinitely; when MQTT reports a stale value (e.g. battery=0
            # after an over-discharge, or battery=100 while charging), that
            # cache would keep clobbering the fresher HTTP status every tick.
            # Skip re-applying the cache when HTTP is newer than the last
            # observed MQTT state push. Ref segwaynavimow/NavimowHA#11.
            http_is_newer = self._last_http_fetch is not None and (
                self._last_mqtt_state_update is None
                or self._last_http_fetch > self._last_mqtt_state_update
            )
            if not http_is_newer:
                self._last_state = cached_state
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
        if (
            not self._mqtt_disconnect_warned
            and is_state_stale
            and not self.sdk.is_connected
        ):
            _LOGGER.warning(
                "MQTT appears disconnected for device %s; relying on HTTP fallback",
                self.device.id,
            )
            self._mqtt_disconnect_warned = True
        elif self._mqtt_disconnect_warned and self.sdk.is_connected:
            _LOGGER.info("MQTT reconnected for device %s", self.device.id)
            self._mqtt_disconnect_warned = False

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
        self.data = self._build_data()
        return self.data

    def _handle_state(self, state: DeviceStateMessage) -> None:
        if state.device_id != self.device.id:
            return
        # BUG-05: the Navimow cloud replays the last-buffered `/state` payload
        # verbatim at every WSS reconnect (~40 min), overwriting a fresher
        # state with the pre-reconnect one. Drop the push if its own
        # payload-embedded `timestamp` (epoch ms) is strictly older than the
        # timestamp of the state we already hold. Payloads without a timestamp
        # (defensive `None`) fall through unchanged.
        new_ts = getattr(state, "timestamp", None)
        prev = self._last_state
        prev_ts = getattr(prev, "timestamp", None) if prev is not None else None
        # Only compare when BOTH sides expose an actual numeric epoch — any
        # missing/non-numeric value falls through to the pre-BUG-05 accept
        # path (defensive for firmwares that omit the field, and safe for
        # tests that pass MagicMock-shaped states).
        if (
            isinstance(new_ts, (int, float))
            and isinstance(prev_ts, (int, float))
            and new_ts < prev_ts
        ):
            _LOGGER.debug(
                "MQTT state DROPPED as stale: device=%s state=%s battery=%s "
                "new_ts=%s < prev_ts=%s",
                state.device_id,
                state.state,
                state.battery,
                new_ts,
                prev_ts,
            )
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
