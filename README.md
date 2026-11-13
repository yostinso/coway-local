# Coway Local

A Home Assistant custom integration for local control of Coway air purifiers, tested with the **AP-1022K**.

Using this integration disconnects your fan from the Coway cloud. The fan is paired with Home Assistant directly: the integration runs its own MQTT broker, and the fan connects to that instead. The Coway mobile app no longer controls a fan set up this way.

Setup gives the fan your Wi-Fi details and the address of your Home Assistant instance over Bluetooth, in one of two ways:

- **Home Assistant Bluetooth:** Home Assistant talks to the fan itself, through a local Bluetooth adapter or an [ESPHome Bluetooth proxy](https://esphome.io/components/bluetooth_proxy/). Fans in setup mode are discovered automatically.
- **Browser setup page:** a web page that uses Web Bluetooth. Chrome only allows Web Bluetooth on HTTPS pages, so the setup page is hosted on GitHub Pages instead of being served by Home Assistant.

This is experimental. It implements the protocol observed on one AP-1022K, and no other model or firmware has been tested. See [Compatibility](#compatibility).

## What you get

- **Fan:** power, three manual speeds, smart and sleep modes.
- **Settings:** lighting level, timer, smart-mode sensitivity, pre-filter cleaning cycle.
- **Sensors:** PM2.5, ambient light, pre-filter remaining, MAX2 filter remaining.
- **Status:** a connection sensor, a refresh button, and a last-seen timestamp (disabled by default).

## Requirements

- Home Assistant Core **2026.10.2** or newer.
- A fixed LAN address for Home Assistant. Reserve it in your router: the address is written into the fan, and the fan must be set up again if it changes.
- For the one-time setup, one of:
  - Home Assistant’s [Bluetooth integration](https://www.home-assistant.io/integrations/bluetooth/) with an adapter on the host or ESPHome Bluetooth proxy in range of the fan. ESPHome proxies need `bluetooth_proxy: active: true`.
  - **Chrome on a Mac or Android device** within Bluetooth range of the fan. Safari and iPhone browsers do not work.
- A 2.4 GHz Wi-Fi network for the fan.

## Install

**With HACS:** open the repository in HACS with the button below, install **Coway Local**, and restart Home Assistant.

[![Open the Coway Local repository in HACS](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?owner=scottjg&repository=coway-local&category=integration)

If the button does not reach your Home Assistant, add `https://github.com/scottjg/coway-local` in HACS as a custom repository with category **Integration**.

**Manually:** copy `custom_components/coway_local` into your Home Assistant configuration directory and restart. The release ZIP has the same layout and can be extracted into `/config`.

After the restart, add the integration with the button below, or find **Coway Local** under **Settings → Devices & services → Add integration**.

[![Add the Coway Local integration to Home Assistant](https://my.home-assistant.io/badges/config_flow_start.svg)](https://my.home-assistant.io/redirect/config_flow_start/?domain=coway_local)

The form shows the LAN address that Home Assistant detected for itself. In most installations you only need to confirm it and submit.

- **Address:** change it only if the fan cannot reach Home Assistant there. With Docker bridge networking, enter the Docker host’s LAN address instead of the detected container address.
- **Advanced settings:** the three TCP ports the fan connects to (HTTP **8080**, HTTPS **9443**, MQTT **9883**), the listen address, and the setup page URL. The defaults suit most installations.

If the integration shows “setup retrying”, a port is probably in use. Choose another under **Configure → Server settings**.

## Add a fan

### With Home Assistant Bluetooth

1. Put the fan in Wi-Fi setup mode by holding down its **Mode** button, or whichever button controls the fan speed.
2. Home Assistant shows a **Discovered** Coway fan under **Settings → Devices & services**. Choose **Add**. Or open **Coway Local → Configure → Add a fan (Home Assistant Bluetooth)** and pick the fan.
3. Home Assistant connects to the fan and asks it to scan for Wi-Fi. Choose a 2.4 GHz network and enter its password.
4. Home Assistant sends the settings and waits for the fan to join Wi-Fi and connect. This can take about 2 minutes. Leave the dialog open until **Fan ready** appears.

Any connectable scanner that can hear the fan is used, so an ESPHome proxy near the fan works even if Home Assistant itself is out of range. A proxy has a few connection slots; if setup fails to connect, check that other devices are not holding them all.

In this mode the Wi-Fi password passes through Home Assistant on its way to the fan. It is held in memory for the setup only and is never stored.

If you **Ignore** a discovered fan, Home Assistant stops offering it, but you can still add it from **Configure**.

### With the browser setup page

1. Open **Coway Local → Configure → Add a fan (web browser Bluetooth)**. The Bluetooth setup page opens in a new tab. If the browser blocks it, use the **Open website** button. Keep the Home Assistant dialog open.
2. Put the fan in Wi-Fi setup mode by holding down its **Mode** button, or whichever button controls the fan speed.
3. Follow the wizard: choose the fan, choose a Wi-Fi network and enter its password, review, and connect.
4. The browser returns to Home Assistant when Bluetooth setup succeeds. Home Assistant then waits for the fan to connect, which can take another 2 minutes. Leave the dialog open until **Fan ready** appears.

The setup link expires after 15 minutes. If it expires, or the fan leaves setup mode, start again from step 1.

With the setup page, the Wi-Fi password is typed into the page and sent to the fan over Bluetooth. Home Assistant never receives or stores it.

## Troubleshooting

| Problem | What to check |
| --- | --- |
| The fan does not appear in the Bluetooth picker | The fan is in setup mode and you are using Chrome on a Mac or Android device. |
| The fan is not discovered by Home Assistant | The fan is in setup mode and a connectable adapter or ESPHome proxy (with `active: true`) is in range. Discovery uses the fan’s advertisements; see [how discovery works](BLE_DISCOVERY.md). |
| Home Assistant cannot connect to the fan | Move a proxy closer, or free one of its connection slots. A fan that left setup mode stops accepting connections. |
| Wi-Fi connection fails | The network is 2.4 GHz and the password is correct. Put the fan back in setup mode and try again. |
| Setup stalls after Wi-Fi connects | The fan cannot reach Home Assistant. Check the configured address, the three ports, and any firewall or VLAN rules between them. |
| The integration shows “setup retrying” | Usually a port conflict, or the credential storage is unreadable. |
| Controls are unavailable | Check the connection sensor, that the fan has power, and that it can still reach Home Assistant. Restarting the integration does not require setting the fan up again. |
| A repair notice says the address changed | Home Assistant no longer has the address the fan was given. Restore the address, or set the new one under **Server settings** and set the fan up again. |

## Networking

The fan connects to Home Assistant on the three configured TCP ports. Changing the address or ports means setting up each fan again.

| Deployment | What to do |
| --- | --- |
| Home Assistant OS | Nothing extra. The ports are opened on the Home Assistant machine itself. |
| Container, host network | Use the Docker host’s LAN address. |
| Container, bridge network | Use the Docker host’s LAN address, and publish all three ports with the same number inside and outside: `8080:8080`, `9443:9443`, `9883:9883`. |
| Fans on a separate VLAN | Allow the fans to reach Home Assistant on the three ports. |

Keep these ports on your trusted LAN. Do not forward them from the Internet.

Home Assistant itself can stay on plain HTTP. The setup page talks to the fan over Bluetooth and then sends the browser back to Home Assistant with a one-use link; it makes no other requests to Home Assistant.

<details>
<summary>What each port carries</summary>

- **HTTP (8080):** the fan downloads the local certificate authority’s public certificate. The fan’s firmware does this over plain HTTP.
- **HTTPS (9443):** the fan enrolls and downloads its own certificate and private key. Each setup link is valid for 15 minutes and for one fan.
- **MQTT over TLS (9883):** the fan’s ongoing connection. It must present its enrolled certificate. The firmware needs TLS 1.2 with `AES128-SHA256`.

Each fan can publish and subscribe only on its own topics. The broker serves these fans only; it does not replace Home Assistant’s MQTT integration.

</details>

## The setup page

The setup page is a static site with no backend, analytics, or third-party scripts. By default the integration uses the copy published from this repository at `https://scottjg.github.io/coway-local/`.

To host your own:

- **On a static HTTPS host:** publish the `web/` directory and enter its URL as the setup page URL in the integration’s advanced settings.
- **On the computer you run setup from:** serve it locally, set the setup page URL to `http://localhost:18765/`, and open the setup link in Chrome on that same computer. This also works without Internet access.

  ```sh
  python3 -m http.server 18765 --bind 127.0.0.1 --directory web
  ```

The page is versioned by setup-link format. The integration appends `v1/` to the configured URL, so each installed release opens a page that understands its links. Enter the URL without the `v1/`.

## Backup and removal

Certificates and keys are stored in `/config/.storage/coway_local/`. Include that directory in your backups. If it is lost, each fan must be set up again.

- **Restarting or reloading** the integration disconnects the fans briefly. They reconnect on their own.
- **Removing a fan** in Home Assistant revokes its access and deletes its credentials. It does not reset the fan itself.
- **Removing the integration** keeps the stored credentials, so reinstalling it restores your fans.

## Compatibility

Tested on one AP-1022K (MCU firmware `V1.0.0.1`, Wi-Fi firmware `000011`), set up from Chrome on macOS into Home Assistant OS 18.3 with Core 2026.10.2. Setup, control, sensors, and reconnecting after a restart work on that fan.

Not tested: other models, other firmware, and Home Assistant Container networking.

Other models may get through Bluetooth setup, because the picker accepts any device that advertises like a Coway fan in setup mode ([how discovery works](BLE_DISCOVERY.md)). That does not make them compatible: the controls and sensors use the AP-1022K’s mappings.

Not implemented: PM10 and other air-quality metrics, filter notification flags, and firmware updates.

### Sensor mappings

The sensors read the fan’s periodic `A1020` reports.

| Sensor | Attribute | Meaning |
| --- | --- | --- |
| PM2.5 | `A1020.0001` | Concentration in μg/m³ |
| Ambient light | `A1020.0007` | Illuminance in lx. The scaling has not been calibrated. |
| Pre-filter remaining | `A1020.0011` | `100 − usage`: percent remaining until cleaning |
| MAX2 filter remaining | `A1020.0012` | `100 − usage`: percent remaining until replacement |

PM2.5 is identified by [Coway’s public sensor definitions](https://iocare2.coway.com/_next/static/chunks/3kv1xb4at9t7g.js). The light and filter mappings agree with [PyCoway’s sensor codes](https://github.com/Antonio112009/pycoway/blob/9d5929bee7f4d3a61e45275bb6486669b4b3474e/src/pycoway/constants.py) and [filter conversion](https://github.com/Antonio112009/pycoway/blob/9d5929bee7f4d3a61e45275bb6486669b4b3474e/src/pycoway/devices/parser.py). The filter percentages have not been compared side by side with the Coway app.

A sensor shows “unknown” until the fan’s first report after connecting, and when the fan reports a value that is out of range.

## Development

```sh
python3.14 -m venv .venv
.venv/bin/pip install -r requirements-test.txt
.venv/bin/pytest -q
.venv/bin/ruff check .
.venv/bin/python scripts/gen_protocol.py --check
node --test tests/web.test.mjs
.venv/bin/python scripts/package.py
```

The Python tests run the service and a real Home Assistant instance against loopback HTTP, TLS, and MQTT sockets with temporary credentials. No test needs a fan.

To try the setup wizard with a simulated fan, run `python tests/browser/serve.py` and open `http://localhost:18765/preview`.

Things to know before changing code:

- **aMQTT is pinned to 0.12.1** because the broker overrides its private hooks. Re-run the tests before changing the pin.
- **The setup page deploys from `main`.** Any change under `web/` goes live through the GitHub Pages workflow, for every installed release on that link format. A change that older releases cannot handle needs a new `web/v2/` directory and a bump to `SETUP_FORMAT` in `const.py`; `web/v1/` stays published.
- **The site root serves the 0.4.2 page**, which releases up to 0.4.2 open. The workflow builds it from the `v0.4.2` tag.
- **The Bluetooth protocol exists twice:** `web/v1/protocol.mjs` for the browser and `custom_components/coway_local/ble_protocol.py` for Home Assistant. Their constants (UUIDs, field numbers, status codes) come from `protocol/ble.json`: edit that, then run `python scripts/gen_protocol.py`; CI fails if the generated files are stale. Both codecs are tested against the same cases in `tests/protocol-vectors.json` (regenerate with `python scripts/gen_vectors.py` only for an intentional protocol change, and review the diff) and `tests/protocol-fixture.json`.
- **`ble_client.py` is imported only when Home Assistant Bluetooth is used**, so the integration still loads without the bluetooth integration. Its tests run against a simulated fan GATT server.

## License

MIT. The broker includes behavior adapted from aMQTT, whose copyright and license are preserved in `LICENSE.amqtt`.

This project is not affiliated with Coway and is not an official Home Assistant integration.
