// Generated from protocol/ble.json by scripts/gen_protocol.py. Do not edit.
export const SERVICE_MAIN = '0000ffc0-0000-1000-8000-00805f9b34fb';
export const SERVICE_EXTRA = '0000ffc1-0000-1000-8000-00805f9b34fb';
export const SERVICE_OTHER = '0000ffc3-0000-1000-8000-00805f9b34fb';
export const CHAR_INFO = '0000ffa0-0000-1000-8000-00805f9b34fb';
export const CHAR_SCAN = '0000ffa1-0000-1000-8000-00805f9b34fb';
export const CHAR_CONFIG = '0000ffa2-0000-1000-8000-00805f9b34fb';
export const CHAR_DATA = '0000ff00-0000-1000-8000-00805f9b34fb';
export const MANUFACTURER_COWAY = 1593;
export const MANUFACTURER_GENERIC = 65535;
export const WIFI_CONNECTED = 10;
export const CREDENTIALS_DOWNLOADED = 50;
export const BROKER_RETRY = 52;
export const BROKER_CONNECTED = 61;
export const KEY_ACCEPTED = 72;
export const FIELDS = { 1: 'cmd', 2: 'ssid', 3: 'pwd', 4: 'bssid', 5: 'type', 6: 'ip', 7: 'gateway', 8: 'subnet', 9: 'dns', 10: 'macAddr', 11: 'caUrl', 12: 'certSvrUrl', 13: 'certToken', 14: 'serial', 15: 'caFile' };
export const FAILURE_CODES = new Set([1, 2, 3, 4, 5, 6, 11, 12, 21, 22, 23, 24, 25, 26, 27, 28, 29, 30, 31, 41, 42, 43, 44, 51, 73]);
export const STATUS = { 1: "Wi-Fi association failed", 2: "Wi-Fi association failed", 3: "Wi-Fi association failed", 4: "Wi-Fi association failed", 5: "DHCP failed", 6: "Configuration rejected", 10: "Wi-Fi connected", 11: "CA server unreachable", 12: "CA download failed", 20: "CA downloaded", 21: "Certificate server unreachable", 30: "Certificate token rejected", 40: "Certificate server accepted setup", 41: "Certificate download unreachable", 42: "Certificate download failed", 43: "Private-key download unreachable", 44: "Private-key download failed", 50: "Device credentials downloaded", 51: "Broker DNS failed", 52: "Broker connection failed", 61: "Local MQTT broker connected", 71: "Setup initialized", 72: "Bluetooth key accepted", 73: "Bluetooth key rejected" };
