"""Wi-Fi provisioning over Home Assistant's Bluetooth stack.

Connections go through HA's bluetooth integration, so they use whichever
connectable scanner is in range: a local adapter or an ESPHome Bluetooth proxy
(with `active: true`). This module is imported lazily by the flows, so the
integration still loads, and the browser setup page still works, without the
bluetooth integration.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable

from bleak.exc import BleakError
from bleak_retry_connector import (
    BleakClientWithServiceCache,
    BleakNotFoundError,
    establish_connection,
)
from homeassistant.core import HomeAssistant

from .ble_protocol import (
    BROKER_CONNECTED,
    BROKER_RETRY,
    CHAR_CONFIG,
    CHAR_DATA,
    CHAR_INFO,
    CHAR_SCAN,
    CREDENTIALS_DOWNLOADED,
    FAILURE_CODES,
    KEY_ACCEPTED,
    SERVICE_EXTRA,
    SERVICE_MAIN,
    STATUS,
    WIFI_CONNECTED,
    CowayBleError,
    Identity,
    Network,
    Reassembler,
    advertised_product,
    decode_proto,
    display_name,
    fragments,
    p001,
    p002,
    parse_scan,
    random_letters,
    read_identity,
)

_LOGGER = logging.getLogger(__name__)

# The browser page uses 20-byte writes because Web Bluetooth hides the MTU.
# Keep the size the firmware was tested with; ESPHome proxies often negotiate 23 anyway.
WRITE_SIZE = 20
BROKER_HELP = (
    "The fan reported that it could not connect to the local server and has not "
    "reported success since. It may still have connected; check Coway Local before "
    "trying again. Otherwise check the server address and ports."
)


def _bluetooth():
    # Deferred: importing the component pulls in its own requirements (habluetooth,
    # usb, hassio), which only exist where the bluetooth integration is installed.
    from homeassistant.components import bluetooth

    return bluetooth


def discovered_fans(hass: HomeAssistant) -> dict[str, str]:
    """Connectable devices currently advertising like a Coway fan in setup mode."""
    fans = {}
    for info in _bluetooth().async_discovered_service_info(hass, connectable=True):
        if advertised_product(info.manufacturer_data) is not None:
            fans[info.address] = display_name(info.name, info.address)
    return fans


class _Channel:
    """Reassembles notifications from one characteristic into whole messages."""

    def __init__(self, uuid: str) -> None:
        self.uuid = uuid
        self.assembler = Reassembler()
        self.queue: asyncio.Queue[bytes | Exception] = asyncio.Queue()
        # FFA0/FFA1 notify a lone 0xFF before the response.
        self.skip_placeholder = uuid in (CHAR_INFO, CHAR_SCAN)

    def feed(self, data: bytes | bytearray) -> None:
        data = bytes(data)
        if self.skip_placeholder and data == b"\xff":
            return
        try:
            message = self.assembler.feed(data)
        except ValueError as error:
            self.fail(error)
            return
        if message is not None:
            self.queue.put_nowait(message)

    def fail(self, error: Exception) -> None:
        self.queue.put_nowait(error)

    def clear(self) -> None:
        self.assembler = Reassembler()
        while not self.queue.empty():
            self.queue.get_nowait()

    async def receive(self, timeout: float, what: str) -> bytes:
        try:
            async with asyncio.timeout(timeout):
                item = await self.queue.get()
        except TimeoutError as error:
            raise CowayBleError("ble_timeout", f"Timed out waiting for {what}.") from error
        if isinstance(item, CowayBleError):
            raise item
        if isinstance(item, Exception):
            raise CowayBleError("ble_protocol", str(item)) from item
        return item


class CowayBleSession:
    """One GATT connection to a fan in setup mode."""

    def __init__(self, hass: HomeAssistant, address: str, advertised_name: str = "") -> None:
        self.hass = hass
        self.address = address
        self.advertised_name = advertised_name
        self.client: BleakClientWithServiceCache | None = None
        self.channels: dict[str, _Channel] = {}
        self.identity: Identity | None = None

    @property
    def connected(self) -> bool:
        return self.client is not None and self.client.is_connected

    def _ble_device(self):
        return _bluetooth().async_ble_device_from_address(self.hass, self.address, connectable=True)

    def _disconnected(self, _client) -> None:
        error = CowayBleError(
            "ble_disconnected",
            "Bluetooth disconnected. Put the fan back into setup mode and try again.",
        )
        for channel in self.channels.values():
            channel.fail(error)

    async def connect(self) -> Identity:
        """Connect, subscribe to the setup channels and read the fan's identity."""
        if self.connected and self.identity:
            return self.identity
        await self.disconnect()
        device = self._ble_device()
        if device is None:
            raise CowayBleError(
                "ble_not_found",
                "No connectable Bluetooth adapter or proxy can currently see the fan.",
            )
        try:
            self.client = await establish_connection(
                BleakClientWithServiceCache,
                device,
                self.advertised_name or self.address,
                disconnected_callback=self._disconnected,
                ble_device_callback=lambda: self._ble_device() or device,
                max_attempts=3,
            )
            main = self.client.services.get_service(SERVICE_MAIN)
            extra = self.client.services.get_service(SERVICE_EXTRA)
            if main is None or extra is None:
                await self.client.clear_cache()
                raise CowayBleError(
                    "ble_not_coway",
                    "This device does not expose the Coway setup services. "
                    "Put the fan in Wi-Fi setup mode and try again.",
                )
            self.channels = {}
            for service, uuid in (
                (main, CHAR_INFO),
                (main, CHAR_SCAN),
                (main, CHAR_CONFIG),
                (extra, CHAR_DATA),
            ):
                characteristic = service.get_characteristic(uuid)
                if characteristic is None:
                    raise CowayBleError(
                        "ble_not_coway",
                        "This device does not expose the Coway setup characteristics.",
                    )
                channel = self.channels[uuid] = _Channel(uuid)
                await self.client.start_notify(
                    characteristic, lambda _char, data, channel=channel: channel.feed(data)
                )
            await self._read(CHAR_INFO)
            info = decode_proto(await self.channels[CHAR_INFO].receive(12, "the fan's identity"))
            self.identity = read_identity(self.advertised_name, info)
        except CowayBleError:
            await self.disconnect()
            raise
        except ValueError as error:
            await self.disconnect()
            raise CowayBleError("ble_not_coway", str(error)) from error
        except (BleakNotFoundError, BleakError, TimeoutError, OSError) as error:
            await self.disconnect()
            raise CowayBleError("ble_connect_failed", f"Bluetooth error: {error}") from error
        _LOGGER.debug("Connected to Coway fan %s (Wi-Fi MAC %s)", self.address, self.identity.mac)
        return self.identity

    async def _read(self, uuid: str) -> None:
        # Bleak does not report reads as notifications; the read value is a fragment too.
        self.channels[uuid].feed(await self.client.read_gatt_char(uuid))

    async def scan_networks(self) -> list[Network]:
        try:
            self.channels[CHAR_SCAN].clear()
            await self._read(CHAR_SCAN)
            return parse_scan(await self.channels[CHAR_SCAN].receive(25, "the Wi-Fi scan"))
        except ValueError as error:
            raise CowayBleError("ble_protocol", str(error)) from error
        except (BleakError, TimeoutError, OSError) as error:
            raise CowayBleError("ble_connect_failed", f"Bluetooth error: {error}") from error

    async def _send(self, message: bytes) -> None:
        for packet in fragments(message, WRITE_SIZE):
            await self.client.write_gatt_char(CHAR_CONFIG, packet, response=True)

    async def _wait_status(
        self, command: int, final: int, timeout: float, progress: Callable[[int], None]
    ) -> None:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        broker_retry = False
        while (remaining := deadline - loop.time()) > 0:
            try:
                data = await self.channels[CHAR_CONFIG].receive(remaining, "setup progress")
            except CowayBleError as error:
                if broker_retry and error.reason == "ble_timeout":
                    raise CowayBleError("broker_unreachable", BROKER_HELP) from error
                raise
            try:
                fields = decode_proto(data)
            except ValueError as error:
                raise CowayBleError("ble_protocol", str(error)) from error
            code = fields.get(3, 0)
            description = STATUS.get(code, f"Device status {code}")
            _LOGGER.debug("Coway fan %s: %s (%s)", self.address, description, code)
            progress(code)
            if code == BROKER_RETRY:
                # Seen on re-enrollment: the first attempt is rejected, a later one succeeds.
                if not broker_retry:
                    deadline = min(deadline, loop.time() + 60)
                broker_retry = True
                continue
            if fields.get(2) != 1 or code in FAILURE_CODES:
                reason = "wifi_failed" if 1 <= code <= 6 else "provision_failed"
                raise CowayBleError(reason, description)
            if fields.get(1) == command and code == final:
                return
        raise CowayBleError(
            "broker_unreachable" if broker_retry else "ble_timeout",
            BROKER_HELP if broker_retry else "Timed out waiting for the fan to finish setup.",
        )

    async def provision(self, config: dict, progress: Callable[[int], None]) -> None:
        """Send the handshake key and encrypted Wi-Fi settings; wait for the broker."""
        if not self.connected:
            raise CowayBleError("ble_disconnected", "Reconnect the fan before starting setup.")
        key = random_letters(20)
        message = p002(config, key)
        self.channels[CHAR_CONFIG].clear()
        try:
            await self._send(p001(key))
            await self._wait_status(1, KEY_ACCEPTED, 15, progress)
            await self._send(message)
            await self._wait_status(2, BROKER_CONNECTED, 120, progress)
        except (BleakError, TimeoutError, OSError) as error:
            raise CowayBleError("ble_connect_failed", f"Bluetooth error: {error}") from error

    async def disconnect(self) -> None:
        client, self.client = self.client, None
        self.identity = None
        if client is not None:
            try:
                await client.disconnect()
            except BleakError, TimeoutError, OSError:
                _LOGGER.debug("Error disconnecting from %s", self.address, exc_info=True)


PROGRESS_STAGES = {WIFI_CONNECTED: 0.4, CREDENTIALS_DOWNLOADED: 0.7, BROKER_CONNECTED: 1.0}
