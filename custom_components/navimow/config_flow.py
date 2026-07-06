"""Config flow for Navimow integration."""

from __future__ import annotations

import copy
import logging
import math
from typing import Any

import voluptuous as vol
from homeassistant import config_entries
from homeassistant.core import callback
from homeassistant.data_entry_flow import FlowResult
from homeassistant.helpers import config_entry_oauth2_flow

from .auth import NavimowOAuth2Implementation
from .const import (
    API_BASE_URL,
    CLIENT_ID,
    CLIENT_SECRET,
    DOMAIN,
    MQTT_BROKER,
    MQTT_PASSWORD,
    MQTT_PORT,
    MQTT_USERNAME,
    OPTIONS_KEY_ZONES,
)

_LOGGER = logging.getLogger(__name__)
_LOGGER.debug("Navimow config_flow module imported")


class NavimowOAuth2FlowHandler(
    config_entry_oauth2_flow.AbstractOAuth2FlowHandler, domain=DOMAIN
):
    """Handle a Navimow OAuth2 config flow."""

    DOMAIN = DOMAIN
    VERSION = 1

    @property
    def logger(self) -> logging.Logger:
        """Return logger."""
        return _LOGGER

    @property
    def oauth2_implementation(self) -> NavimowOAuth2Implementation:
        """Return the OAuth2 implementation."""
        _LOGGER.debug(
            "Creating OAuth2 implementation for domain=%s, client_id_set=%s, client_secret_set=%s",
            DOMAIN,
            bool(CLIENT_ID),
            bool(CLIENT_SECRET),
        )
        implementation = NavimowOAuth2Implementation(
            self.hass, DOMAIN, CLIENT_ID, CLIENT_SECRET
        )
        # Ensure HA has the implementation registered before redirect/callback.
        config_entry_oauth2_flow.async_register_implementation(
            self.hass, DOMAIN, implementation
        )
        _LOGGER.debug("OAuth2 implementation registered for domain=%s", DOMAIN)
        return implementation

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Handle a flow initiated by the user."""
        _LOGGER.debug("Starting OAuth2 flow: source=%s", self.source)
        # Check whether it is already configured
        await self.async_set_unique_id(DOMAIN)
        self._abort_if_unique_id_configured()

        # Check the required configuration
        if not CLIENT_ID or not CLIENT_SECRET:
            _LOGGER.error(
                "Missing OAuth2 client configuration: client_id_set=%s, client_secret_set=%s",
                bool(CLIENT_ID),
                bool(CLIENT_SECRET),
            )
            return self.async_abort(
                reason="missing_config",
                description_placeholders={
                    "error": "CLIENT_ID or CLIENT_SECRET is not configured. Please set them in const.py."
                },
            )

        # Ensure implementation is registered before authorize step.
        _LOGGER.debug("Registering OAuth2 implementation before authorize step")
        _ = self.oauth2_implementation
        # Only one OAuth2 implementation; go straight to the authorization step
        _LOGGER.debug("Proceeding to OAuth2 authorize step")
        return await super().async_step_user()

    async def async_step_oauth2_authorize(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Ensure implementation exists before redirect."""
        _LOGGER.debug("Entering oauth2_authorize step")
        # Force register implementation in case HA missed it.
        _ = self.oauth2_implementation
        return await super().async_step_oauth2_authorize(user_input)

    async def async_step_reauth(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Perform reauth upon an API authentication error."""
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Dialog that informs the user that reauth is required."""
        if user_input is None:
            return self.async_show_form(
                step_id="reauth_confirm",
                data_schema=None,
            )

        # Only one OAuth2 implementation; go straight to the authorization step
        return await super().async_step_user()

    async def async_oauth_create_entry(self, data: dict[str, Any]) -> FlowResult:
        """Create an entry for the flow, or update existing entry for reauth."""
        # HA has already handled the token exchange; data["token"] already contains the token info
        # For a reauth, HA updates the entry automatically
        if self.source == config_entries.SOURCE_REAUTH:
            existing_entry = self.entry
            self.hass.config_entries.async_update_entry(
                existing_entry,
                data={
                    **existing_entry.data,
                    **data,  # contains the new token
                },
            )
            await self.hass.config_entries.async_reload(existing_entry.entry_id)
            return self.async_abort(reason="reauth_successful")

        # Save the config and token (HA has already handled the token exchange)
        return self.async_create_entry(
            title="Navimow",
            data={
                "auth_implementation": DOMAIN,
                **data,  # contains the token (handled automatically by HA)
                "api_base_url": API_BASE_URL,
                "mqtt_broker": MQTT_BROKER,
                "mqtt_port": MQTT_PORT,
                "mqtt_username": MQTT_USERNAME,
                "mqtt_password": MQTT_PASSWORD,
            },
        )

    @staticmethod
    @callback
    def async_get_options_flow(
        config_entry: config_entries.ConfigEntry,
    ) -> config_entries.OptionsFlow:
        """Get the options flow for this handler."""
        return NavimowOptionsFlowHandler(config_entry)


class NavimowOptionsFlowHandler(config_entries.OptionsFlow):
    """Handle Navimow options.

    FEAT-04 PR 4 — menu-driven zone naming and removal. The zone
    catalog itself lives in ``config_entry.options[OPTIONS_KEY_ZONES]``,
    shaped ``{"<boundary_id>": {"name": "Prunier"}}`` — JSON forces the
    id to a string. Renaming a zone updates its entry; forgetting a
    zone drops the entry AND fires ``SIGNAL_ZONE_FORGOTTEN`` so the
    sensor platform can remove the three per-zone entities.

    The list of discoverable zones comes from the live coordinator's
    ``zone_registry`` — no separate persisted directory of zones.
    """

    def __init__(self, config_entry: config_entries.ConfigEntry) -> None:
        self._config_entry = config_entry

    def _known_boundary_ids(self) -> list[int]:
        """List boundary ids from every coordinator of this entry.

        Falls back to the ids stored in options (previously-named zones
        that may momentarily not appear in a fresh registry) so a
        rename remains possible even if the runtime state is
        transiently empty.
        """
        boundary_ids: set[int] = set()
        try:
            data = self.hass.data[DOMAIN][self._config_entry.entry_id]
            for coord in data["coordinators"].values():
                boundary_ids.update(coord.zone_registry.zones.keys())
        except (KeyError, AttributeError):
            pass
        for str_id in self._config_entry.options.get(OPTIONS_KEY_ZONES, {}):
            try:
                boundary_ids.add(int(str_id))
            except (TypeError, ValueError):
                continue
        return sorted(boundary_ids)

    def _zone_label(self, boundary_id: int) -> str:
        """User-facing label in the selector: current name (if any) +
        boundary id + last-mowed surface for recognition.

        Surface is presented ``math.ceil``'d for parity with the
        per-zone sensor state (D-size). Round-and-ceil disagree by
        1 m² on non-integers — the picker would read ``227`` while
        the entity shows ``228`` on the same zone otherwise.
        """
        zones_opt = self._config_entry.options.get(OPTIONS_KEY_ZONES, {})
        name = zones_opt.get(str(boundary_id), {}).get("name")
        try:
            data = self.hass.data[DOMAIN][self._config_entry.entry_id]
            for coord in data["coordinators"].values():
                rec = coord.zone_registry.zones.get(boundary_id)
                if rec is not None and rec.last_surface_m2 is not None:
                    surface_int = math.ceil(rec.last_surface_m2)
                    if name:
                        return f"{name} — #{boundary_id} ({surface_int} m²)"
                    return f"#{boundary_id} ({surface_int} m²)"
        except (KeyError, AttributeError):
            pass
        if name:
            return f"{name} — #{boundary_id}"
        return f"#{boundary_id}"

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Top-level menu — rename or forget."""
        return self.async_show_menu(
            step_id="init",
            menu_options=["rename_zone", "forget_zone"],
        )

    async def async_step_rename_zone(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Zone rename step: pick a boundary + name."""
        boundary_ids = self._known_boundary_ids()
        if not boundary_ids:
            return self.async_abort(reason="no_zones")

        if user_input is not None:
            new_options = copy.deepcopy(dict(self._config_entry.options))
            zones = new_options.setdefault(OPTIONS_KEY_ZONES, {})
            str_id = user_input["boundary_id"]
            name = user_input["name"].strip()
            if name:
                zones[str_id] = {"name": name}
            else:
                # Empty name clears the mapping — sensor falls back to `#<id>`.
                zones.pop(str_id, None)
            return self.async_create_entry(title="", data=new_options)

        choices = {str(bid): self._zone_label(bid) for bid in boundary_ids}
        schema = vol.Schema(
            {
                vol.Required("boundary_id"): vol.In(choices),
                vol.Required("name", default=""): str,
            }
        )
        return self.async_show_form(step_id="rename_zone", data_schema=schema)

    async def async_step_forget_zone(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Zone forget step: pick a boundary + confirm."""
        boundary_ids = self._known_boundary_ids()
        if not boundary_ids:
            return self.async_abort(reason="no_zones")

        if user_input is not None:
            if not user_input.get("confirm"):
                return self.async_abort(reason="forget_cancelled")
            new_options = copy.deepcopy(dict(self._config_entry.options))
            str_id = user_input["boundary_id"]
            zones = new_options.get(OPTIONS_KEY_ZONES, {})
            zones.pop(str_id, None)
            new_options[OPTIONS_KEY_ZONES] = zones
            # Fire the forget signal so the sensor platform can remove
            # the three per-zone entities. Deferred import avoids a
            # circular import at module load.
            from homeassistant.helpers.dispatcher import async_dispatcher_send

            from .const import SIGNAL_ZONE_FORGOTTEN

            try:
                data = self.hass.data[DOMAIN][self._config_entry.entry_id]
                for device_id in data["coordinators"]:
                    async_dispatcher_send(
                        self.hass,
                        f"{SIGNAL_ZONE_FORGOTTEN}_{device_id}",
                        int(str_id),
                    )
            except (KeyError, AttributeError, ValueError):
                pass
            return self.async_create_entry(title="", data=new_options)

        choices = {str(bid): self._zone_label(bid) for bid in boundary_ids}
        schema = vol.Schema(
            {
                vol.Required("boundary_id"): vol.In(choices),
                vol.Required("confirm", default=False): bool,
            }
        )
        return self.async_show_form(step_id="forget_zone", data_schema=schema)
