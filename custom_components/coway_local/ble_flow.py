"""Flow steps that provision a fan through Home Assistant's own Bluetooth.

Shared by the config flow (Bluetooth discovery) and the options flow (Add fan).
Unlike the browser setup page, the Wi-Fi password passes through Home Assistant
here. It is held only in memory for the duration of the flow, never stored.
"""

from __future__ import annotations

import asyncio
import importlib
import logging
import time

import probatio as vol
from homeassistant.helpers.selector import (
    SelectOptionDict,
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
    TextSelector,
    TextSelectorConfig,
    TextSelectorType,
)

from .ble_protocol import (
    CowayBleError,
    Network,
    usable_networks,
    valid_password,
    wifi_config,
)

_LOGGER = logging.getLogger(__name__)
ADDRESS = "address"
NETWORK = "network"
PASSWORD = "password"
RESCAN = "rescan"


def ble_client():
    """Import the bleak-backed client only when HA's bluetooth stack is in use."""
    return importlib.import_module(".ble_client", __package__)


def bluetooth_available(hass) -> bool:
    return "bluetooth" in hass.config.components


class BluetoothProvisioningMixin:
    """Connect -> choose Wi-Fi -> provision over BLE -> wait for MQTT enrollment."""

    pairing = None
    pairing_service = None
    verify_task = None
    _ble_session = None
    _ble_task: asyncio.Task | None = None
    _ble_address: str | None = None
    _ble_name: str = ""
    _ble_identity = None
    _ble_networks: list[Network] = []
    _ble_network: Network | None = None
    _ble_password: str = ""
    _ble_error = None
    _ble_progress_codes: set[int]

    def _coway_service(self):
        raise NotImplementedError

    # Lifecycle

    def _ble_cleanup(self) -> None:
        self._ble_password = ""
        if self._ble_task and not self._ble_task.done():
            self._ble_task.cancel()
        if self._ble_session is not None:
            session, self._ble_session = self._ble_session, None
            self.hass.async_create_background_task(
                session.disconnect(), "Coway Local Bluetooth disconnect"
            )
        if self.pairing and self.pairing_service and not self.pairing.complete:
            self.pairing_service.cancel_pairing(self.pairing)

    def _ble_failed(self, error):
        reason = getattr(error, "reason", "ble_connect_failed")
        self._ble_cleanup()
        return self.async_abort(reason=reason, description_placeholders={"error": str(error)})

    def _ble_start(self, address: str, name: str) -> None:
        if self._ble_session is not None and self._ble_session.address != address:
            self._ble_cleanup()
        self._ble_address, self._ble_name = address, name
        if self._ble_session is None:
            self._ble_identity = None
            self._ble_session = ble_client().CowayBleSession(self.hass, address, name)

    async def _ble_run(self, step_id, progress_action, work, next_step_id, placeholders=None):
        """Show progress while `work` runs; then continue to `next_step_id`."""
        if self._ble_task is None:
            self._ble_error = None
            self._ble_task = self.hass.async_create_background_task(
                work(), f"Coway Local Bluetooth {step_id}"
            )
        if not self._ble_task.done():
            return self.async_show_progress(
                step_id=step_id,
                progress_action=progress_action,
                progress_task=self._ble_task,
                description_placeholders=placeholders,
            )
        task, self._ble_task = self._ble_task, None
        if task.cancelled():
            self._ble_error = CowayBleError("ble_disconnected", "Setup was canceled.")
        elif (error := task.exception()) is not None:
            if not isinstance(error, CowayBleError):
                _LOGGER.exception("Unexpected Coway Bluetooth error", exc_info=error)
            self._ble_error = error
        return self.async_show_progress_done(
            next_step_id="bluetooth_failed" if self._ble_error else next_step_id
        )

    # Steps

    async def async_step_bluetooth_connect(self, user_input=None):
        async def work():
            identity = await self._ble_session.connect()
            if self._ble_identity is None:
                self._ble_identity = identity
            elif identity.mac != self._ble_identity.mac:
                raise CowayBleError("ble_not_coway", "A different fan answered over Bluetooth.")
            self._ble_networks = usable_networks(await self._ble_session.scan_networks())

        return await self._ble_run(
            "bluetooth_connect",
            "bluetooth_connect",
            work,
            "bluetooth_wifi",
            {"name": self._ble_label()},
        )

    async def async_step_bluetooth_wifi(self, user_input=None):
        errors = {}
        placeholders = {"name": self._ble_label(), "error": ""}
        if user_input is not None:
            if user_input.get(RESCAN):
                return await self.async_step_bluetooth_connect()
            network = self._network_by_key(user_input.get(NETWORK))
            password = user_input.get(PASSWORD, "")
            if network is None:
                errors[NETWORK] = "network_required"
            elif not valid_password(network, password):
                errors[PASSWORD] = "invalid_password"
            else:
                self._ble_network, self._ble_password = network, password
                return await self.async_step_bluetooth_provision()
        elif self._ble_error is not None:
            # The fan rejected the Wi-Fi settings; let the user fix them and retry.
            errors["base"] = self._ble_error.reason
            placeholders["error"] = str(self._ble_error)
            self._ble_error = None

        options = [
            SelectOptionDict(value=self._network_key(n), label=self._network_label(n))
            for n in self._ble_networks
        ]
        schema = {
            vol.Optional(RESCAN, default=False): bool,
        }
        if options:
            default = self._network_key(self._ble_network) if self._ble_network else None
            schema = {
                vol.Required(NETWORK, **({"default": default} if default else {})): SelectSelector(
                    SelectSelectorConfig(options=options, mode=SelectSelectorMode.DROPDOWN)
                ),
                vol.Optional(PASSWORD, default=""): TextSelector(
                    TextSelectorConfig(type=TextSelectorType.PASSWORD)
                ),
                **schema,
            }
        else:
            errors.setdefault("base", "no_networks")
        return self.async_show_form(
            step_id="bluetooth_wifi",
            data_schema=vol.Schema(schema),
            errors=errors,
            description_placeholders=placeholders,
        )

    async def async_step_bluetooth_provision(self, user_input=None):
        if self._ble_task is None:
            service = self._coway_service()
            if service is None:
                return self.async_abort(reason="hub_required")
            if self.pairing is None or self.pairing.finished.is_set():
                try:
                    self.pairing = service.begin_pairing()
                except ValueError, ConnectionError:
                    self._ble_cleanup()
                    return self.async_abort(reason="cannot_start_pairing")
                self.pairing_service = service
            self._ble_progress_codes = set()
            bundle = service.setup_bundle(self.pairing)
            expected = self._ble_identity

            async def work():
                config = {}
                try:
                    # Reconnects if the link idled out while the user typed the password.
                    identity = await self._ble_session.connect()
                    if expected is not None and identity.mac != expected.mac:
                        raise CowayBleError(
                            "ble_not_coway", "A different fan answered over Bluetooth."
                        )
                    try:
                        config = wifi_config(
                            bundle, identity, self._ble_network, self._ble_password
                        )
                    except ValueError as error:
                        raise CowayBleError("provision_failed", str(error)) from error
                    await self._ble_session.provision(config, self._ble_progress)
                finally:
                    config.clear()
                    self._ble_password = ""

        else:
            work = None
        return await self._ble_run(
            "bluetooth_provision",
            "bluetooth_provision",
            work,
            "bluetooth_sent",
            {"name": self._ble_label(), "network": self._ble_network.ssid},
        )

    def _ble_progress(self, code: int) -> None:
        stages = ble_client().PROGRESS_STAGES
        if code in stages and code not in self._ble_progress_codes:
            self._ble_progress_codes.add(code)
            self.async_update_progress(stages[code])

    async def async_step_bluetooth_sent(self, user_input=None):
        """Bluetooth work is done; MQTT authentication now proves enrollment."""
        if self._ble_session is not None:
            session, self._ble_session = self._ble_session, None
            await session.disconnect()
        return await self.async_step_verify()

    async def async_step_bluetooth_failed(self, user_input=None):
        error, session = self._ble_error, self._ble_session
        if getattr(error, "reason", "") == "broker_unreachable" and self.pairing:
            # The fan has its credentials and keeps retrying the broker; it often
            # connects later. Keep waiting for MQTT enrollment, as the browser page does.
            return await self.async_step_bluetooth_sent()
        if self.pairing and self.pairing_service and not self.pairing.complete:
            self.pairing_service.cancel_pairing(self.pairing)
            self.pairing = None
        retryable = getattr(error, "reason", "") in ("wifi_failed", "provision_failed")
        # Provisioning reconnects on its own, so the link need not still be up.
        if retryable and session is not None and self._ble_networks:
            return await self.async_step_bluetooth_wifi()
        return self._ble_failed(error)

    # Shared with the browser flow: wait for the fan to enroll over MQTT.

    async def _wait_for_pairing(self):
        try:
            async with asyncio.timeout(max(0, self.pairing.expires - time.time())):
                await self.pairing.finished.wait()
        except TimeoutError:
            self.pairing_service.cancel_pairing(self.pairing)

    async def async_step_verify(self, user_input=None):
        if self.verify_task is None:
            if self.pairing.finished.is_set() or time.time() > self.pairing.expires:
                return await self.async_step_finish()
            self.verify_task = self.hass.async_create_background_task(
                self._wait_for_pairing(), "Coway Local enrollment verification"
            )
        if self.verify_task.done():
            return self.async_show_progress_done(next_step_id="finish")
        return self.async_show_progress(
            step_id="verify",
            progress_action="verify_connection",
            progress_task=self.verify_task,
        )

    async def async_step_finish(self, user_input=None):
        if self.pairing.complete:
            # Enrollment is already persisted by the service. End the flow without
            # saving options, which would show HA's fixed "Options successfully saved".
            return self.async_abort(reason="fan_ready")
        reason = "pairing_expired" if time.time() >= self.pairing.expires else "pairing_interrupted"
        return self.async_abort(reason=reason)

    # Helpers

    def _ble_label(self) -> str:
        identity = self._ble_identity
        return (identity.name if identity else self._ble_name) or "Coway fan"

    @staticmethod
    def _network_key(network: Network) -> str:
        return network.bssid

    @staticmethod
    def _network_label(network: Network) -> str:
        lock = "" if network.security else " (open)"
        return f"{network.ssid}{lock} · ch {network.channel} · {network.rssi} dBm"

    def _network_by_key(self, key) -> Network | None:
        return next((n for n in self._ble_networks if self._network_key(n) == key), None)
