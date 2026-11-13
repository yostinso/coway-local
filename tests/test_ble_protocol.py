"""The Python BLE codec must produce the same bytes as the browser page."""

import base64
import json
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import padding
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

from custom_components.coway_local.ble_protocol import (
    FAILURE_CODES,
    STATUS,
    Network,
    Reassembler,
    advertised_product,
    decode_proto,
    display_name,
    encode_proto,
    fragments,
    p001,
    p002,
    parse_scan,
    random_letters,
    read_identity,
    usable_networks,
    wifi_config,
)

FIXTURE = json.loads((Path(__file__).parent / "protocol-fixture.json").read_text())
BUNDLE = {
    "caUrl": "http://192.0.2.1:8080/ca.pem",
    "certSvrUrl": "https://192.0.2.1:9443/n",
    "certToken": "a" * 32,
}
NETWORK = Network(ssid="Example", bssid="aabbccddeeff", rssi=-50, security=4, channel=6)


def decrypt(message, mac):
    raw = base64.b64decode(message[4:])
    key = FIXTURE["key"].encode() + mac.encode()
    decryptor = Cipher(algorithms.AES(key), modes.CBC(raw[:16])).decryptor()
    unpadder = padding.PKCS7(128).unpadder()
    plain = decryptor.update(raw[16:]) + decryptor.finalize()
    return decode_proto(unpadder.update(plain) + unpadder.finalize())


def test_p001_and_p002_match_the_browser_fixture():
    key, iv = FIXTURE["key"].encode(), FIXTURE["iv"].encode()
    assert p001(key).hex() == FIXTURE["p001"]
    assert p002(FIXTURE["config"], key, iv).hex() == FIXTURE["p002"]


def test_p002_round_trips_with_a_random_iv():
    message = p002(FIXTURE["config"], FIXTURE["key"].encode())
    assert message.startswith(b"P002")
    fields = decrypt(message, "AABBCCDDEEFF")
    assert fields[1] == 2 and fields[2] == b"Example Wi-Fi" and fields[5] == 1


def test_fragments_reassemble_and_stay_within_20_bytes():
    message = p002(FIXTURE["config"], FIXTURE["key"].encode(), FIXTURE["iv"].encode())
    packets = fragments(message)
    assert all(len(p) <= 20 for p in packets)
    assembler = Reassembler()
    assert all(assembler.feed(p) is None for p in packets[:-1])
    assert assembler.feed(packets[-1]) == message
    with pytest.raises(ValueError):
        Reassembler().feed(packets[1])


def test_proto_round_trip_and_rejects_duplicates():
    data = encode_proto({1: 300, 2: "text", 3: 0, 4: b""})
    assert decode_proto(data) == {1: 300, 2: b"text"}
    with pytest.raises(ValueError):
        decode_proto(data + data)


def test_scan_results_are_parsed_and_filtered():
    def item(ssid, rssi, security, channel):
        raw = ssid.encode()
        return (
            b"\0\0"
            + len(raw).to_bytes(2, "little")
            + raw.ljust(32, b"\0")
            + bytes.fromhex("001122334455")
            + rssi.to_bytes(4, "little", signed=True)
            + bytes([security, channel])
        )

    data = (
        b"*ICT*SCANRESULT:"
        + item("Weak", -80, 4, 1)
        + item("Strong", -40, 0, 11)
        + item("Five", -30, 4, 36)
        + item("", -20, 0, 1)
        + b"[OK]".ljust(48, b"\0")
    )
    networks = parse_scan(data)
    assert [n.ssid for n in networks] == ["Weak", "Strong", "Five", ""]
    assert networks[1] == Network("Strong", "001122334455", -40, 0, 11)
    assert [n.ssid for n in usable_networks(networks)] == ["Strong", "Weak"]
    with pytest.raises(ValueError):
        parse_scan(b"nope")


def test_advertisement_layouts_from_ble_discovery():
    tested = bytes.fromhex("0016313133343339303030303030303030303030303030c5")
    assert advertised_product({0xFFFF: tested}) == "113439"
    base36 = int("ZZZZZ", 36)
    assert advertised_product({0xFFFF: b"\0\0\x40ZZZZZ"}) == str(base36)
    assert advertised_product({0x0639: b"\0\x4000001"}) == "1"
    assert advertised_product({0x0639: b"\0\x41000001"}) is None
    assert advertised_product({0xFFFF: b"short"}) is None
    assert advertised_product({0x004C: tested}) is None


def test_identity_and_wifi_config():
    identity = read_identity("AP-1022K", {1: b"COWAY123", 2: b"00:11:22:33:44:55"})
    assert identity.mac == "001122334455" and identity.name == "AP-1022K"
    assert read_identity("", {2: b"001122334455", 1: b"COWAY123"}).name == "COWAY123"
    with pytest.raises(ValueError):
        read_identity("x", {2: b"not a mac"})

    config = wifi_config(BUNDLE, identity, NETWORK, "password1")
    assert config["serial"] == "LOCAL0001122334455"
    assert config["certSvrUrl"] == BUNDLE["certSvrUrl"] + "/" + "QVAtMTAyMks"
    assert config["certToken"] == BUNDLE["certToken"]
    with pytest.raises(ValueError):
        wifi_config(BUNDLE, identity, NETWORK, "short")
    open_network = Network("Open", "aabbccddeeff", -50, 0, 1)
    assert wifi_config(BUNDLE, identity, open_network, "ignored")["pwd"] == ""


def test_helpers():
    assert len(random_letters(20)) == 20 and random_letters(20).isalpha()
    assert display_name("AA-BB-CC-DD-EE-FF", "AA:BB:CC:DD:EE:FF") == ""
    assert display_name(" AP-1022K ", "AA:BB:CC:DD:EE:FF") == "AP-1022K"


# Shared vectors: tests/web.test.mjs checks the browser codec against the same file.
VECTORS = json.loads((Path(__file__).parent / "protocol-vectors.json").read_text("utf-8"))


def _fields(case):
    return {int(k): v.encode() if isinstance(v, str) else v for k, v in case.items()}


@pytest.mark.parametrize("case", VECTORS["proto"])
def test_vector_proto(case):
    assert encode_proto(_fields(case["fields"])).hex() == case["hex"]
    decoded = decode_proto(bytes.fromhex(case["hex"]))
    assert {str(k): v if isinstance(v, int) else v.hex() for k, v in decoded.items()} == case[
        "decoded"
    ]


@pytest.mark.parametrize("data", VECTORS["proto_invalid"])
def test_vector_proto_invalid(data):
    with pytest.raises(ValueError):
        decode_proto(bytes.fromhex(data))


@pytest.mark.parametrize("case", VECTORS["fragments"])
def test_vector_fragments(case):
    message = bytes.fromhex(case["message"])
    packets = fragments(message, case["value_size"])
    assert [p.hex() for p in packets] == case["packets"]
    assembler = Reassembler()
    results = [assembler.feed(p) for p in packets]
    assert results[:-1] == [None] * (len(packets) - 1) and results[-1] == message


@pytest.mark.parametrize("packets", VECTORS["reassembly_invalid"])
def test_vector_reassembly_invalid(packets):
    assembler = Reassembler()
    with pytest.raises(ValueError):
        for packet in packets:
            assembler.feed(bytes.fromhex(packet))


def test_vector_scan():
    networks = parse_scan(bytes.fromhex(VECTORS["scan"]["hex"]))
    assert [vars(n) for n in networks] == VECTORS["scan"]["networks"]
    assert [n.ssid for n in usable_networks(networks)] == VECTORS["scan"]["usable"]
    for data in VECTORS["scan_invalid"]:
        with pytest.raises(ValueError):
            parse_scan(bytes.fromhex(data))


@pytest.mark.parametrize("case", VECTORS["advertisements"], ids=lambda c: c["note"])
def test_vector_advertisement(case):
    product = advertised_product({case["company"]: bytes.fromhex(case["payload"])})
    assert product == case["product"]
    # Anything Python accepts, the browser's coarser filter must also show.
    assert product is None or case["browser_filter"]


@pytest.mark.parametrize("case", VECTORS["identity"])
def test_vector_identity(case):
    identity = read_identity(case["advertised"], _fields(case["fields"]))
    expect = case["expect"]
    assert (identity.name, identity.mac, identity.module_name) == (
        expect["name"],
        expect["mac"],
        expect["module"],
    )


@pytest.mark.parametrize("fields", VECTORS["identity_invalid"])
def test_vector_identity_invalid(fields):
    with pytest.raises(ValueError):
        read_identity("AP-1022K", _fields(fields))


@pytest.mark.parametrize("case", VECTORS["status"], ids=lambda c: str(c["code"]))
def test_vector_status(case):
    assert (case["code"] in FAILURE_CODES) == case["failure"]
    assert STATUS.get(case["code"]) == case["description"]
