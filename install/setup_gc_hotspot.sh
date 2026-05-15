#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'USAGE'
Usage: sudo ./install/setup_gc_hotspot.sh [options]

Configure the ground-station Pi as a WiFi access point on 10.42.0.1.

Options:
  --ssid NAME          Hotspot SSID, default UHRK-GC
  --password PASS      WPA2 password, default uhrk1234
  --interface IFACE    WiFi interface, default auto-detect or wlan0
  --address CIDR       Static AP address, default 10.42.0.1/24
  --channel NUMBER     2.4 GHz channel, default 6
  --connection NAME    NetworkManager connection name, default uhrk-gc-hotspot
  --no-up              Configure connection but do not bring it up now
  --help               Show this help

Example:
  sudo ./install/setup_gc_hotspot.sh --ssid UHRK-GC --password uhrk1234
USAGE
}

require_root() {
  if [[ "$(id -u)" -ne 0 ]]; then
    echo "Run this script with sudo." >&2
    exit 1
  fi
}

require_command() {
  if ! command -v "$1" >/dev/null 2>&1; then
    echo "Required command not found: $1" >&2
    exit 1
  fi
}

detect_wifi_iface() {
  local iface
  iface="$(iw dev 2>/dev/null | awk '$1 == "Interface" { print $2; exit }')"
  if [[ -n "$iface" ]]; then
    printf '%s\n' "$iface"
    return
  fi
  if ip link show wlan0 >/dev/null 2>&1; then
    printf '%s\n' "wlan0"
    return
  fi
  echo "Could not auto-detect a WiFi interface. Pass --interface wlan0 or similar." >&2
  exit 1
}

ssid="${UHRK_GC_HOTSPOT_SSID:-UHRK-GC}"
password="${UHRK_GC_HOTSPOT_PASSWORD:-uhrk1234}"
iface="${UHRK_GC_HOTSPOT_IFACE:-}"
address="${UHRK_GC_HOTSPOT_ADDRESS:-10.42.0.1/24}"
channel="${UHRK_GC_HOTSPOT_CHANNEL:-6}"
connection="${UHRK_GC_HOTSPOT_CONNECTION:-uhrk-gc-hotspot}"
bring_up=1

while [[ $# -gt 0 ]]; do
  case "$1" in
    --ssid) ssid="$2"; shift 2 ;;
    --password) password="$2"; shift 2 ;;
    --interface) iface="$2"; shift 2 ;;
    --address) address="$2"; shift 2 ;;
    --channel) channel="$2"; shift 2 ;;
    --connection) connection="$2"; shift 2 ;;
    --no-up) bring_up=0; shift ;;
    --help) usage; exit 0 ;;
    *) echo "Unknown option: $1" >&2; usage; exit 1 ;;
  esac
done

require_root
require_command nmcli
require_command iw
require_command ip

if [[ ${#password} -lt 8 ]]; then
  echo "Hotspot password must be at least 8 characters for WPA2." >&2
  exit 1
fi

if [[ -z "$iface" ]]; then
  iface="$(detect_wifi_iface)"
fi

if ! ip link show "$iface" >/dev/null 2>&1; then
  echo "WiFi interface does not exist: $iface" >&2
  exit 1
fi

if ! iw list 2>/dev/null | grep -qi '\* AP'; then
  echo "This WiFi adapter does not report AP mode support." >&2
  exit 1
fi

nmcli radio wifi on
nmcli device set "$iface" managed yes

if nmcli -t -f NAME connection show | grep -Fxq "$connection"; then
  nmcli connection delete "$connection" >/dev/null
fi

nmcli connection add \
  type wifi \
  ifname "$iface" \
  con-name "$connection" \
  autoconnect yes \
  ssid "$ssid" >/dev/null

nmcli connection modify "$connection" \
  802-11-wireless.mode ap \
  802-11-wireless.band bg \
  802-11-wireless.channel "$channel" \
  wifi-sec.key-mgmt wpa-psk \
  wifi-sec.psk "$password" \
  ipv4.method shared \
  ipv4.addresses "$address" \
  ipv6.method ignore \
  connection.autoconnect yes

if [[ "$bring_up" -eq 1 ]]; then
  nmcli connection up "$connection"
fi

cat <<EOF
UHRK GC hotspot configured.
Connection: ${connection}
SSID: ${ssid}
Interface: ${iface}
Address: ${address}
Channel: ${channel}

Dashboard URL after joining the hotspot:
http://10.42.0.1:8000/
EOF
