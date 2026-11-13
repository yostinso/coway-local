"""Coway FFC0/P001/P002 Bluetooth codec. A Python port of web/v1/protocol.mjs.

Constants come from protocol/ble.json via scripts/gen_protocol.py; edit that file, not
ble_constants.py.

No Home Assistant or Bluetooth stack imports, so it can be tested on its own.
"""

from __future__ import annotations

import base64
import re
import secrets
import string
from dataclasses import dataclass

from cryptography.hazmat.primitives import padding
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

from .ble_constants import (  # noqa: F401 - re-exported for callers
    BROKER_CONNECTED,
    BROKER_RETRY,
    CHAR_CONFIG,
    CHAR_DATA,
    CHAR_INFO,
    CHAR_SCAN,
    CREDENTIALS_DOWNLOADED,
    FAILURE_CODES,
    FIELDS,
    KEY_ACCEPTED,
    MANUFACTURER_COWAY,
    MANUFACTURER_GENERIC,
    SERVICE_EXTRA,
    SERVICE_MAIN,
    SERVICE_OTHER,
    STATUS,
    WIFI_CONNECTED,
)

MAC = re.compile(r"[0-9A-F]{12}\Z")
MAX_MESSAGE = 65536


def canonical_mac(mac: str) -> str:
    return mac.replace(":", "").replace("-", "").upper()


def _varint(value: int) -> bytes:
    if type(value) is not int or value < 0:
        raise ValueError("Invalid protobuf integer")
    out = bytearray()
    while value > 127:
        out.append((value & 127) | 128)
        value >>= 7
    out.append(value)
    return bytes(out)


def encode_proto(fields: dict[int, int | str | bytes]) -> bytes:
    """Encode varint and length-delimited fields; zero and empty values are omitted."""
    out = bytearray()
    for number in sorted(fields):
        value = fields[number]
        if isinstance(value, int):
            if value:
                out += _varint(number * 8) + _varint(value)
        else:
            data = value.encode() if isinstance(value, str) else bytes(value)
            if data:
                out += _varint(number * 8 + 2) + _varint(len(data)) + data
    return bytes(out)


def decode_proto(data: bytes) -> dict[int, int | bytes]:
    fields: dict[int, int | bytes] = {}
    offset = 0

    def read() -> int:
        nonlocal offset
        value = shift = 0
        for _ in range(8):
            if offset >= len(data):
                raise ValueError("Truncated protobuf integer")
            byte = data[offset]
            offset += 1
            value |= (byte & 127) << shift
            if not byte & 128:
                return value
            shift += 7
        raise ValueError("Oversized protobuf integer")

    while offset < len(data):
        tag = read()
        number, wire = tag >> 3, tag & 7
        if not number or number in fields:
            raise ValueError("Invalid or duplicate protobuf field")
        if wire == 0:
            fields[number] = read()
        elif wire == 2:
            length = read()
            if offset + length > len(data):
                raise ValueError("Truncated protobuf field")
            fields[number] = bytes(data[offset : offset + length])
            offset += length
        else:
            raise ValueError("Unsupported protobuf field")
    return fields


def random_letters(length: int) -> bytes:
    return "".join(secrets.choice(string.ascii_lowercase) for _ in range(length)).encode()


def p001(key: bytes) -> bytes:
    if len(key) != 20:
        raise ValueError("Handshake key must be 20 bytes")
    return b"P001" + encode_proto({1: 1, 2: base64.b64encode(key)})


def config_proto(config: dict) -> bytes:
    fields: dict[int, int | str | bytes] = {
        number: config[name] for number, name in FIELDS.items() if name in config
    }
    fields[1] = 2
    fields.setdefault(5, 1)
    return encode_proto(fields)


def p002(config: dict, key: bytes, iv: bytes | None = None) -> bytes:
    """AES-256-CBC (20-letter handshake key + Wi-Fi MAC) over the config protobuf."""
    iv = random_letters(16) if iv is None else iv
    mac = canonical_mac(config["macAddr"])
    if not MAC.fullmatch(mac) or len(key) != 20 or len(iv) != 16:
        raise ValueError("Invalid encryption inputs")
    padder = padding.PKCS7(128).padder()
    plain = padder.update(config_proto(config)) + padder.finalize()
    encryptor = Cipher(algorithms.AES(key + mac.encode()), modes.CBC(iv)).encryptor()
    encrypted = encryptor.update(plain) + encryptor.finalize()
    return b"P002" + base64.b64encode(iv + encrypted)


def fragments(data: bytes, value_size: int = 20) -> list[bytes]:
    """Split a message into GATT writes with a little-endian (count, index) header."""
    if not 20 <= value_size <= 509:
        raise ValueError("Invalid GATT value size")
    size = value_size - 4
    count = -(-len(data) // size)
    if not 1 <= count <= 4096:
        raise ValueError("Invalid fragment count")
    return [
        count.to_bytes(2, "little")
        + (i + 1).to_bytes(2, "little")
        + data[i * size : (i + 1) * size]
        for i in range(count)
    ]


class Reassembler:
    def __init__(self) -> None:
        self.parts: list[bytes] = []
        self.count = 0
        self.size = 0

    def feed(self, data: bytes) -> bytes | None:
        if len(data) < 4:
            raise ValueError("Truncated Coway fragment")
        count = int.from_bytes(data[0:2], "little")
        index = int.from_bytes(data[2:4], "little")
        if not 1 <= count <= 4096 or not 1 <= index <= count:
            raise ValueError("Invalid fragment header")
        if index == 1:
            if self.parts:
                raise ValueError("Previous message was incomplete")
            self.count = count
        if count != self.count or index != len(self.parts) + 1:
            raise ValueError("Out-of-order fragment")
        self.parts.append(bytes(data[4:]))
        self.size += len(data) - 4
        if self.size > MAX_MESSAGE:
            raise ValueError("Coway message too large")
        if index != count:
            return None
        result = b"".join(self.parts)
        self.parts, self.count, self.size = [], 0, 0
        return result


@dataclass(frozen=True)
class Network:
    ssid: str
    bssid: str
    rssi: int
    security: int
    channel: int


def parse_scan(data: bytes) -> list[Network]:
    if data[:16] != b"*ICT*SCANRESULT:":
        raise ValueError("Unexpected Wi-Fi scan response")
    result = []
    for offset in range(16, len(data) - 47, 48):
        item = data[offset : offset + 48]
        if item[:4] == b"[OK]":
            break
        length = int.from_bytes(item[2:4], "little")
        if length > 32:
            raise ValueError("Invalid SSID length")
        result.append(
            Network(
                ssid=item[4 : 4 + length].decode("utf-8", "replace"),
                bssid=item[36:42].hex(),
                rssi=int.from_bytes(item[42:46], "little", signed=True),
                security=item[46],
                channel=item[47],
            )
        )
    return result


def usable_networks(networks: list[Network]) -> list[Network]:
    """Named 2.4 GHz networks, strongest first."""
    return sorted(
        (n for n in networks if n.ssid and n.channel <= 14), key=lambda n: n.rssi, reverse=True
    )


def _base36(data: bytes) -> int | None:
    try:
        return int(data.decode("ascii"), 36)
    except UnicodeError, ValueError:
        return None


def advertised_product(manufacturer_data: dict[int, bytes]) -> str | None:
    """Return the product/material code if an advertisement matches IoCare's layouts.

    The 0x0639 layout takes priority, as it does in the app. A match is a
    discovery hint only: GATT services and the identity read are still required.
    """
    data = manufacturer_data.get(MANUFACTURER_COWAY)
    if data is not None and len(data) >= 7 and data[1] == 0x40:
        code = _base36(data[2:7])
        if code is not None:
            return str(code)
    data = manufacturer_data.get(MANUFACTURER_GENERIC)
    if data is not None and len(data) >= 8:
        if data[2] == 0x40:
            code = _base36(data[3:8])
            return None if code is None else str(code)
        try:
            text = data[2:8].decode("ascii")
        except UnicodeError:
            return None
        return text if text.isprintable() else None
    return None


@dataclass(frozen=True)
class Identity:
    name: str
    mac: str
    module_name: str


def read_identity(advertised_name: str | None, fields: dict) -> Identity:
    mac_field, module_field = fields.get(2), fields.get(1)
    if not isinstance(mac_field, bytes) or (
        module_field is not None and not isinstance(module_field, bytes)
    ):
        raise ValueError("The device did not return a valid Coway Bluetooth identity.")
    mac = canonical_mac(mac_field.decode("utf-8", "replace"))
    if not MAC.fullmatch(mac):
        raise ValueError("The device did not return a valid Wi-Fi MAC address.")
    # FFA0 field 1 is the module SSID (often COWAYxxxxxx), not the product model.
    module_name = module_field.decode("utf-8", "replace") if module_field else ""
    name = (advertised_name or "").strip() or module_name or "Unnamed device"
    return Identity(name=name, mac=mac, module_name=module_name)


def valid_password(network: Network, password: str) -> bool:
    return not network.security or 8 <= len(password.encode()) <= 63


def wifi_config(bundle: dict, identity: Identity, network: Network, password: str) -> dict:
    """Build the P002 config. The device name rides in the enrollment URL path."""
    if not MAC.fullmatch(identity.mac):
        raise ValueError("Read a valid device identity before sending Wi-Fi settings.")
    if not network.ssid or len(network.ssid.encode()) > 32 or network.channel > 14:
        raise ValueError("Select a 2.4 GHz Wi-Fi network from the fan's scan.")
    if not re.fullmatch(r"[0-9a-fA-F]{12}", network.bssid):
        raise ValueError("Select a 2.4 GHz Wi-Fi network from the fan's scan.")
    if not valid_password(network, password):
        raise ValueError("Enter an 8-63 byte Wi-Fi password.")
    name = identity.name.strip()
    if not name or len(name) > 80 or any(ord(c) < 32 or 127 <= ord(c) <= 159 for c in name):
        raise ValueError("The Bluetooth device name must be 1-80 printable characters.")
    encoded = base64.urlsafe_b64encode(name.encode()).decode().rstrip("=")
    return {
        "cmd": 2,
        "type": 1,
        "serial": "LOCAL0" + identity.mac,
        "macAddr": identity.mac,
        "ssid": network.ssid,
        "bssid": network.bssid,
        "pwd": password if network.security else "",
        "caUrl": bundle["caUrl"],
        "certSvrUrl": f"{bundle['certSvrUrl']}/{encoded}",
        "certToken": bundle["certToken"],
    }


def display_name(name: str | None, address: str) -> str:
    """An advertised name, or "" when HA substituted the address for a missing one."""
    if not name or name.replace("-", ":").upper() == address.upper():
        return ""
    return name.strip()


class CowayBleError(Exception):
    """A failure to show the user. `reason` is a translation key for flow errors."""

    def __init__(self, reason: str, message: str) -> None:
        super().__init__(message)
        self.reason = reason
