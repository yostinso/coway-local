"""Native HA Bluetooth provisioning, with a simulated fan behind the BLE session."""

import asyncio
import base64
import sys
import types
from types import SimpleNamespace

import pytest

from custom_components.coway_local.ble_protocol import CowayBleError, Identity, Network
from custom_components.coway_local.const import DOMAIN

from .conftest import enroll, eventually
from .test_homeassistant import SERIAL, ha, ha_fan  # noqa: F401

ADDRESS = "AA:BB:CC:DD:EE:FF"
MAC = "001122334455"
NETWORKS = [
    Network("Home", "aabbccddeeff", -40, 4, 6),
    Network("Guest", "aabbccddee00", -70, 0, 1),
]


class FakeSession:
    """Stands in for the GATT link; acts like the fan once it has the config."""

    instances = []
    fail_with = []

    def __init__(self, hass, address, name):
        self.address, self.name = address, name
        self.identity = None
        self.configs = []
        self.service = None
        self.disconnects = 0
        FakeSession.instances.append(self)

    @property
    def connected(self):
        return self.identity is not None

    async def connect(self):
        await asyncio.sleep(0.05)  # A real GATT connection is never instant.
        self.identity = Identity(self.name or "COWAY123", MAC, "COWAY123")
        return self.identity

    async def scan_networks(self):
        return NETWORKS

    async def provision(self, config, progress):
        self.configs.append(dict(config))
        await asyncio.sleep(0.05)
        if FakeSession.fail_with:
            raise FakeSession.fail_with.pop(0)
        for code in (72, 10, 50):
            progress(code)
        pairing = self.service.pairings[config["certToken"]]
        name = config["certSvrUrl"].rsplit("/", 1)[1]
        name = base64.urlsafe_b64decode(name + "=" * (-len(name) % 4)).decode()
        await enroll(self.service, mac=config["macAddr"], pairing=pairing, bluetooth_name=name)
        progress(61)

    async def disconnect(self):
        self.disconnects += 1
        self.identity = None


@pytest.fixture
def fake_ble(ha, monkeypatch):  # noqa: F811
    FakeSession.instances = []
    FakeSession.fail_with = []
    module = types.ModuleType("custom_components.coway_local.ble_client")
    module.CowayBleSession = lambda *args: _session(ha, *args)
    module.discovered_fans = lambda hass: {ADDRESS: "AP-1022K"}
    module.PROGRESS_STAGES = {10: 0.4, 50: 0.7, 61: 1.0}
    monkeypatch.setitem(sys.modules, module.__name__, module)
    ha.hass.config.components.add("bluetooth")
    yield module
    ha.hass.config.components.remove("bluetooth")


def _session(ha, *args):  # noqa: F811
    session = FakeSession(*args)
    session.service = ha.service
    return session


async def wait_step(manager, flow_id, step_id):
    await eventually(lambda: manager.async_get(flow_id)["step_id"] == step_id)
    return await manager.async_configure(flow_id)


async def reconnect(ha_fan):  # noqa: F811
    await asyncio.to_thread(ha_fan.close)
    await asyncio.to_thread(ha_fan.open)


async def test_options_flow_provisions_over_ha_bluetooth(ha, ha_fan, fake_ble):  # noqa: F811
    flow = await ha.options.async_init(ha.entry.entry_id)
    assert flow["menu_options"][0] == "add_fan_bluetooth"
    flow = await ha.options.async_configure(flow["flow_id"], {"next_step_id": "add_fan_bluetooth"})
    assert flow["step_id"] == "add_fan_bluetooth"
    flow_id = flow["flow_id"]

    flow = await ha.options.async_configure(flow_id, {"address": ADDRESS})
    assert flow["type"] == "progress" and flow["step_id"] == "bluetooth_connect", flow
    flow = await wait_step(ha.options, flow_id, "bluetooth_wifi")
    assert flow["type"] == "form"

    flow = await ha.options.async_configure(flow_id, {"network": "aabbccddeeff", "password": "x"})
    assert flow["errors"] == {"password": "invalid_password"}

    flow = await ha.options.async_configure(
        flow_id, {"network": "aabbccddeeff", "password": "password1"}
    )
    assert flow["type"] == "progress" and flow["step_id"] == "bluetooth_provision"
    (session,) = FakeSession.instances
    await eventually(lambda: session.configs)
    config = session.configs[0]
    assert config["ssid"] == "Home" and config["pwd"] == "password1"
    assert config["serial"] == SERIAL and config["certSvrUrl"].startswith(
        ha.service.setup_bundle(ha.service.pairings[config["certToken"]])["certSvrUrl"] + "/"
    )

    flow = await wait_step(ha.options, flow_id, "bluetooth_sent")
    assert flow["type"] == "progress" and flow["step_id"] == "verify"
    assert session.disconnects == 1
    await reconnect(ha_fan)
    flow = await wait_step(ha.options, flow_id, "finish")
    assert flow["type"] == "abort" and flow["reason"] == "fan_ready"
    assert ha.service.devices[SERIAL]["name"] == "AP-1022K"
    assert not ha.service.pairings


async def test_wifi_failure_returns_to_the_wifi_form(ha, fake_ble):  # noqa: F811
    FakeSession.fail_with = [CowayBleError("wifi_failed", "Wi-Fi association failed")]
    flow = await ha.options.async_init(ha.entry.entry_id)
    flow = await ha.options.async_configure(flow["flow_id"], {"next_step_id": "add_fan_bluetooth"})
    flow_id = flow["flow_id"]
    await ha.options.async_configure(flow_id, {"address": ADDRESS})
    await wait_step(ha.options, flow_id, "bluetooth_wifi")
    await ha.options.async_configure(flow_id, {"network": "aabbccddee00"})
    flow = await wait_step(ha.options, flow_id, "bluetooth_failed")
    assert flow["type"] == "form" and flow["step_id"] == "bluetooth_wifi"
    assert flow["errors"] == {"base": "wifi_failed"}
    assert not ha.service.pairings
    ha.options.async_abort(flow_id)
    assert FakeSession.instances[0].disconnects >= 0


async def test_broker_retry_keeps_waiting_for_enrollment(ha, fake_ble):  # noqa: F811
    FakeSession.fail_with = [CowayBleError("broker_unreachable", "Broker connection failed")]
    flow = await ha.options.async_init(ha.entry.entry_id)
    flow = await ha.options.async_configure(flow["flow_id"], {"next_step_id": "add_fan_bluetooth"})
    flow_id = flow["flow_id"]
    await ha.options.async_configure(flow_id, {"address": ADDRESS})
    await wait_step(ha.options, flow_id, "bluetooth_wifi")
    await ha.options.async_configure(flow_id, {"network": "aabbccddee00"})
    flow = await wait_step(ha.options, flow_id, "bluetooth_failed")
    assert flow["type"] == "progress" and flow["step_id"] == "verify"
    (pairing,) = ha.service.pairings.values()
    ha.service.cancel_pairing(pairing)
    flow = await wait_step(ha.options, flow_id, "finish")
    assert flow["reason"] == "pairing_interrupted"


async def test_connection_failure_aborts_with_the_reason(ha, fake_ble, monkeypatch):  # noqa: F811
    async def fail(self):
        await asyncio.sleep(0.05)
        raise CowayBleError("ble_not_found", "No proxy can see the fan.")

    monkeypatch.setattr(FakeSession, "connect", fail)
    flow = await ha.options.async_init(ha.entry.entry_id)
    flow = await ha.options.async_configure(flow["flow_id"], {"next_step_id": "add_fan_bluetooth"})
    flow_id = flow["flow_id"]
    await ha.options.async_configure(flow_id, {"address": ADDRESS})
    flow = await wait_step(ha.options, flow_id, "bluetooth_failed")
    assert flow["type"] == "abort" and flow["reason"] == "ble_not_found"
    assert flow["description_placeholders"]["error"] == "No proxy can see the fan."


async def test_menu_hides_bluetooth_without_the_bluetooth_integration(ha):  # noqa: F811
    flow = await ha.options.async_init(ha.entry.entry_id)
    assert "add_fan_bluetooth" not in flow["menu_options"]
    ha.options.async_abort(flow["flow_id"])


def discovery(manufacturer_data, address=ADDRESS, name="AP-1022K"):
    return SimpleNamespace(address=address, name=name, manufacturer_data=manufacturer_data)


async def test_bluetooth_discovery_provisions_into_the_existing_hub(ha, ha_fan, fake_ble):  # noqa: F811
    flows = ha.hass.config_entries.flow
    advert = {0xFFFF: bytes.fromhex("0016313133343339303030303030303030303030303030c5")}
    flow = await flows.async_init(DOMAIN, context={"source": "bluetooth"}, data=discovery(advert))
    assert flow["type"] == "form" and flow["step_id"] == "bluetooth_confirm"
    assert flow["description_placeholders"] == {"name": "AP-1022K"}
    flow_id = flow["flow_id"]
    duplicate = await flows.async_init(
        DOMAIN, context={"source": "bluetooth"}, data=discovery(advert)
    )
    assert duplicate["reason"] == "already_in_progress"

    await flows.async_configure(flow_id, {})
    await wait_step(flows, flow_id, "bluetooth_wifi")
    await flows.async_configure(flow_id, {"network": "aabbccddeeff", "password": "password1"})
    flow = await wait_step(flows, flow_id, "bluetooth_sent")
    assert flow["step_id"] == "verify"
    await reconnect(ha_fan)
    flow = await wait_step(flows, flow_id, "finish")
    assert flow["type"] == "abort" and flow["reason"] == "fan_ready"
    assert len(ha.hass.config_entries.async_entries(DOMAIN)) == 1


async def test_bluetooth_discovery_ignores_other_devices(ha):  # noqa: F811
    flow = await ha.hass.config_entries.flow.async_init(
        DOMAIN, context={"source": "bluetooth"}, data=discovery({0x004C: b"\x02\x15"})
    )
    assert flow["type"] == "abort" and flow["reason"] == "not_supported"
