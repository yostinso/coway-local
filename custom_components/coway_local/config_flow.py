"""Server setup, browser Bluetooth enrollment links and native Bluetooth setup."""

import secrets
import time
from dataclasses import asdict
from urllib.parse import urlencode

import probatio as vol
from homeassistant import config_entries
from homeassistant.components import network
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import callback
from homeassistant.data_entry_flow import section
from homeassistant.helpers.network import NoURLAvailableError

from .ble_flow import ADDRESS, BluetoothProvisioningMixin, ble_client, bluetooth_available
from .ble_protocol import advertised_product, display_name
from .const import DEFAULT_PORTS, DOMAIN, PROVISIONER_URL
from .provisioning import EXPIRES_KEY, RETURN_PATH, STATE_KEY, frontend_url
from .service import ServerConfig

ADVANCED = "advanced"


def server_schema(defaults):
    """Show the address; keep settings that rarely change in a collapsed section."""
    advanced = {
        vol.Optional(
            "provisioner_url", default=defaults.get("provisioner_url", PROVISIONER_URL)
        ): str,
    }
    for key, port in DEFAULT_PORTS.items():
        advanced[vol.Required(key, default=defaults.get(key, port))] = vol.All(
            vol.Coerce(int), vol.Range(min=1024, max=65535)
        )
    advanced[vol.Optional("bind_host", default=defaults.get("bind_host", "0.0.0.0"))] = str

    return vol.Schema(
        {
            vol.Required("host", default=defaults.get("host", "")): str,
            vol.Required(ADVANCED): section(vol.Schema(advanced), {"collapsed": True}),
        }
    )


def flatten(user_input):
    values = dict(user_input)
    values.update(values.pop(ADVANCED, {}))
    return values


class ConfigFlow(BluetoothProvisioningMixin, config_entries.ConfigFlow, domain=DOMAIN):
    VERSION = 1

    def _coway_service(self):
        for entry in self._async_current_entries(include_ignore=False):
            if entry.state is ConfigEntryState.LOADED:
                return entry.runtime_data
        return None

    @callback
    def async_remove(self):
        self._ble_cleanup()

    async def async_step_bluetooth(self, discovery_info):
        """A fan in Wi-Fi setup mode, seen by a local adapter or an ESPHome proxy."""
        if advertised_product(discovery_info.manufacturer_data) is None:
            return self.async_abort(reason="not_supported")
        await self.async_set_unique_id(discovery_info.address)
        self._abort_if_unique_id_configured()
        name = display_name(discovery_info.name, discovery_info.address)
        self._ble_address, self._ble_name = discovery_info.address, name
        self.context["title_placeholders"] = {"name": name or "Coway fan"}
        return await self.async_step_bluetooth_confirm()

    async def async_step_bluetooth_confirm(self, user_input=None):
        if user_input is not None:
            if self._coway_service() is None:
                return self.async_abort(reason="hub_required")
            self._ble_start(self._ble_address, self._ble_name)
            return await self.async_step_bluetooth_connect()
        self._set_confirm_only()
        return self.async_show_form(
            step_id="bluetooth_confirm",
            description_placeholders={"name": self._ble_name or "Coway fan"},
        )

    async def async_step_user(self, user_input=None):
        if self._async_current_entries():
            return self.async_abort(reason="single_instance_allowed")
        errors = {}
        defaults = {}
        if user_input is not None:
            defaults = flatten(user_input)
            try:
                config = ServerConfig(**defaults).validate()
            except ValueError, TypeError:
                errors["base"] = "invalid_server"
            else:
                await self.async_set_unique_id(DOMAIN)
                self._abort_if_unique_id_configured()
                return self.async_create_entry(title="Coway Local", data=asdict(config))
        else:
            try:
                defaults["host"] = await network.async_get_source_ip(self.hass)
            except Exception:
                defaults["host"] = ""
        return self.async_show_form(
            step_id="user", data_schema=server_schema(defaults), errors=errors
        )

    @staticmethod
    @callback
    def async_get_options_flow(config_entry):
        return OptionsFlow()


class OptionsFlow(BluetoothProvisioningMixin, config_entries.OptionsFlow):
    def __init__(self):
        self.pairing = None
        self.pairing_service = None
        self.setup_url = None
        self.verify_task = None

    def _coway_service(self):
        return self.config_entry.runtime_data

    @callback
    def async_remove(self):
        self.context.pop(STATE_KEY, None)
        self._ble_cleanup()
        if self.pairing and self.pairing_service:
            self.pairing_service.cancel_pairing(self.pairing)

    async def async_step_init(self, user_input=None):
        options = ["add_fan", "server"]
        if bluetooth_available(self.hass):
            options.insert(0, "add_fan_bluetooth")
        return self.async_show_menu(step_id="init", menu_options=options)

    async def async_step_add_fan_bluetooth(self, user_input=None):
        """Provision through HA's Bluetooth adapters and ESPHome proxies; no browser."""
        fans = ble_client().discovered_fans(self.hass)
        if user_input is not None and user_input[ADDRESS] in fans:
            address = user_input[ADDRESS]
            self._ble_start(address, fans[address])
            return await self.async_step_bluetooth_connect()
        if not fans:
            return self.async_abort(reason="no_devices_found")
        choices = {address: f"{name or 'Coway fan'} ({address})" for address, name in fans.items()}
        return self.async_show_form(
            step_id="add_fan_bluetooth",
            data_schema=vol.Schema({vol.Required(ADDRESS): vol.In(choices)}),
        )

    async def async_step_add_fan(self, user_input=None):
        service = self.config_entry.runtime_data
        try:
            base = frontend_url(self.hass)
            self.pairing = service.begin_pairing()
            self.pairing_service = service
        except ValueError, ConnectionError, NoURLAvailableError:
            return self.async_abort(reason="cannot_start_pairing")
        state = secrets.token_urlsafe(24)
        self.context[STATE_KEY] = state
        self.context[EXPIRES_KEY] = self.pairing.expires
        query = urlencode({"flow_id": self.flow_id, "state": state})
        self.setup_url = service.setup_url(self.pairing, f"{base}{RETURN_PATH}?{query}")
        return await self.async_step_pairing()

    async def async_step_pairing(self, user_input=None):
        if user_input and user_input.get("returned"):
            return self.async_external_step_done(next_step_id="verify")
        if self.pairing.finished.is_set() or time.time() > self.pairing.expires:
            return self.async_external_step_done(next_step_id="finish")
        return self.async_external_step(
            step_id="pairing",
            url=self.setup_url,
        )

    async def async_step_server(self, user_input=None):
        defaults = self.config_entry.data | self.config_entry.options
        errors = {}
        if user_input is not None:
            try:
                config = ServerConfig(**flatten(user_input)).validate()
            except ValueError, TypeError:
                errors["base"] = "invalid_server"
                defaults = flatten(user_input)
            else:
                return self.async_create_entry(title="", data=asdict(config))
        return self.async_show_form(
            step_id="server", data_schema=server_schema(defaults), errors=errors
        )
