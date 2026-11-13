"""The bleak-backed session against a simulated fan GATT server."""

import asyncio
import base64

import pytest
from cryptography.hazmat.primitives import padding
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

from custom_components.coway_local import ble_client
from custom_components.coway_local.ble_protocol import (
    CHAR_CONFIG,
    CHAR_DATA,
    CHAR_INFO,
    CHAR_SCAN,
    SERVICE_EXTRA,
    SERVICE_MAIN,
    CowayBleError,
    Network,
    Reassembler,
    decode_proto,
    encode_proto,
    fragments,
)

MAC = "001122334455"
CONFIG = {
    "cmd": 2,
    "type": 1,
    "serial": "LOCAL0" + MAC,
    "macAddr": MAC,
    "ssid": "Home",
    "bssid": "aabbccddeeff",
    "pwd": "password1",
    "caUrl": "http://192.0.2.1:8080/ca.pem",
    "certSvrUrl": "https://192.0.2.1:9443/n/QVAtMTAyMks",
    "certToken": "a" * 32,
}


def scan_result():
    raw = b"Home"
    item = (
        b"\0\0"
        + len(raw).to_bytes(2, "little")
        + raw.ljust(32, b"\0")
        + bytes.fromhex("aabbccddeeff")
        + (-45).to_bytes(4, "little", signed=True)
        + bytes([4, 6])
    )
    return b"*ICT*SCANRESULT:" + item + b"[OK]".ljust(48, b"\0")


class Characteristic:
    def __init__(self, uuid):
        self.uuid = uuid


class Service:
    def __init__(self, *uuids):
        self.characteristics = {uuid: Characteristic(uuid) for uuid in uuids}

    def get_characteristic(self, uuid):
        return self.characteristics.get(uuid)


class Services:
    def __init__(self, services):
        self.services = services

    def get_service(self, uuid):
        return self.services.get(uuid)


class FakeFan:
    """Answers like the observed AP-1022K: 0xFF on read, then fragmented notifications."""

    def __init__(self, statuses=(10, 20, 40, 50, 52, 61), services=True):
        self.statuses = statuses
        self.is_connected = True
        self.callbacks = {}
        self.writes = Reassembler()
        self.key = None
        self.received = None
        self.disconnected = None
        main = Service(CHAR_INFO, CHAR_SCAN, CHAR_CONFIG)
        extra = Service(CHAR_DATA)
        self.services = Services({SERVICE_MAIN: main, SERVICE_EXTRA: extra} if services else {})

    async def start_notify(self, characteristic, callback):
        self.callbacks[characteristic.uuid] = callback

    def notify(self, uuid, message):
        loop = asyncio.get_running_loop()
        for packet in fragments(message, 23):
            loop.call_soon(self.callbacks[uuid], None, bytearray(packet))

    async def read_gatt_char(self, uuid):
        if uuid == CHAR_INFO:
            self.notify(CHAR_INFO, encode_proto({1: "COWAY123", 2: MAC}))
        elif uuid == CHAR_SCAN:
            self.notify(CHAR_SCAN, scan_result())
        return bytearray(b"\xff")

    def status(self, command, code, ok=1):
        self.notify(CHAR_CONFIG, encode_proto({1: command, 2: ok, 3: code}))

    async def write_gatt_char(self, uuid, packet, response):
        assert uuid == CHAR_CONFIG and response and len(packet) <= 20
        message = self.writes.feed(bytes(packet))
        if message is None:
            return
        if message.startswith(b"P001"):
            self.key = base64.b64decode(decode_proto(message[4:])[2])
            self.status(1, 72)
        elif message.startswith(b"P002"):
            raw = base64.b64decode(message[4:])
            cipher = Cipher(algorithms.AES(self.key + MAC.encode()), modes.CBC(raw[:16]))
            decryptor, unpadder = cipher.decryptor(), padding.PKCS7(128).unpadder()
            plain = decryptor.update(raw[16:]) + decryptor.finalize()
            self.received = decode_proto(unpadder.update(plain) + unpadder.finalize())
            for code in self.statuses:
                ok = 0 if code in (1, 2, 3, 4, 5, 6) else 1
                self.status(2, code, ok)

    async def disconnect(self):
        self.is_connected = False

    async def clear_cache(self):
        pass


@pytest.fixture
def fan(monkeypatch):
    holder = {"fan": FakeFan()}

    async def connect(client_class, device, name, disconnected_callback, **kwargs):
        holder["fan"].disconnected = disconnected_callback
        return holder["fan"]

    monkeypatch.setattr(ble_client, "establish_connection", connect)
    monkeypatch.setattr(ble_client.CowayBleSession, "_ble_device", lambda self: object())
    return holder


async def test_identity_scan_and_provisioning(fan):
    session = ble_client.CowayBleSession(None, "AA:BB:CC:DD:EE:FF", "AP-1022K")
    identity = await session.connect()
    assert identity.mac == MAC and identity.name == "AP-1022K"
    assert identity.module_name == "COWAY123"
    assert await session.scan_networks() == [Network("Home", "aabbccddeeff", -45, 4, 6)]

    codes = []
    await session.provision(dict(CONFIG), codes.append)
    assert codes == [72, 10, 20, 40, 50, 52, 61]
    received = fan["fan"].received
    assert received[2] == b"Home" and received[3] == b"password1"
    assert received[13] == b"a" * 32 and received[14] == b"LOCAL0" + MAC.encode()
    await session.disconnect()
    assert not fan["fan"].is_connected


async def test_wifi_failure_is_reported(fan):
    fan["fan"] = FakeFan(statuses=(2,))
    session = ble_client.CowayBleSession(None, "AA:BB:CC:DD:EE:FF", "")
    assert (await session.connect()).name == "COWAY123"
    with pytest.raises(CowayBleError) as error:
        await session.provision(dict(CONFIG), lambda code: None)
    assert error.value.reason == "wifi_failed"


async def test_disconnect_during_provisioning(fan):
    fan["fan"] = FakeFan(statuses=())
    session = ble_client.CowayBleSession(None, "AA:BB:CC:DD:EE:FF", "")
    await session.connect()
    task = asyncio.create_task(session.provision(dict(CONFIG), lambda code: None))
    await asyncio.sleep(0.05)
    fan["fan"].disconnected(fan["fan"])
    with pytest.raises(CowayBleError) as error:
        await task
    assert error.value.reason == "ble_disconnected"


async def test_other_devices_are_rejected(fan):
    fan["fan"] = FakeFan(services=False)
    session = ble_client.CowayBleSession(None, "AA:BB:CC:DD:EE:FF", "")
    with pytest.raises(CowayBleError) as error:
        await session.connect()
    assert error.value.reason == "ble_not_coway"
    assert not session.connected


async def test_no_scanner_in_range(fan, monkeypatch):
    monkeypatch.setattr(ble_client.CowayBleSession, "_ble_device", lambda self: None)
    session = ble_client.CowayBleSession(None, "AA:BB:CC:DD:EE:FF", "")
    with pytest.raises(CowayBleError) as error:
        await session.connect()
    assert error.value.reason == "ble_not_found"
