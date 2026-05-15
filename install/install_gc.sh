#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'USAGE'
Usage: sudo ./install/install_gc.sh [options]

Install the UHRK ground-station backend and dashboard on a Raspberry Pi.

Options:
  --user NAME           Linux user to run GC services, default uhrkgc
  --site-dir PATH       Dashboard/backend install dir, default /opt/uhrk/uhrk_site
  --gateway-dir PATH    Optional sx1302/sx1303 HAL root containing packet_forwarder/
  --gps-port PATH       Ground GPS serial port, default /dev/ttyAMA0
  --setup-hotspot       Configure a 2.4 GHz NetworkManager hotspot on 10.42.0.1
  --hotspot-ssid NAME   Hotspot SSID, default UHRK-GC
  --hotspot-pass PASS   Hotspot WPA2 password, default uhrk1234
  --hotspot-iface IFACE WiFi interface, default auto-detect or wlan0
  --hotspot-channel N   2.4 GHz channel, default 6
  --help                Show this help

Example:
  sudo ./install/install_gc.sh --user uhrkgc --gateway-dir /home/uhrkgc/gateway/sx1302_hal_rpi5-master
USAGE
}

require_root() {
  if [[ "$(id -u)" -ne 0 ]]; then
    echo "Run this installer with sudo." >&2
    exit 1
  fi
}

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
user_name="${UHRK_GC_USER:-uhrkgc}"
site_dir="${UHRK_SITE_DIR:-/opt/uhrk/uhrk_site}"
gateway_dir="${UHRK_GATEWAY_DIR:-}"
gps_port="${UHRK_GROUND_GPS_PORT:-/dev/ttyAMA0}"
setup_hotspot=0
hotspot_ssid="${UHRK_GC_HOTSPOT_SSID:-UHRK-GC}"
hotspot_pass="${UHRK_GC_HOTSPOT_PASSWORD:-uhrk1234}"
hotspot_iface="${UHRK_GC_HOTSPOT_IFACE:-}"
hotspot_channel="${UHRK_GC_HOTSPOT_CHANNEL:-6}"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --user) user_name="$2"; shift 2 ;;
    --site-dir) site_dir="$2"; shift 2 ;;
    --gateway-dir) gateway_dir="$2"; shift 2 ;;
    --gps-port) gps_port="$2"; shift 2 ;;
    --setup-hotspot) setup_hotspot=1; shift ;;
    --hotspot-ssid) hotspot_ssid="$2"; shift 2 ;;
    --hotspot-pass) hotspot_pass="$2"; shift 2 ;;
    --hotspot-iface) hotspot_iface="$2"; shift 2 ;;
    --hotspot-channel) hotspot_channel="$2"; shift 2 ;;
    --help) usage; exit 0 ;;
    *) echo "Unknown option: $1" >&2; usage; exit 1 ;;
  esac
done

require_root

if [[ ! -d "${repo_root}/remote/uhrkgc/uhrk_site" ]]; then
  echo "Cannot find GC source under ${repo_root}/remote/uhrkgc/uhrk_site" >&2
  exit 1
fi

if ! id "$user_name" >/dev/null 2>&1; then
  useradd --create-home --shell /bin/bash "$user_name"
fi

for group_name in spi i2c dialout gpio; do
  if getent group "$group_name" >/dev/null 2>&1; then
    usermod -aG "$group_name" "$user_name"
  fi
done

apt-get update
apt-get install -y python3 git network-manager wireless-tools iw

mkdir -p "$(dirname "$site_dir")" /etc/uhrk
state_tmp=""
if [[ -d "$site_dir" ]]; then
  state_tmp="$(mktemp -d)"
  for state_name in flight_logs event_settings.json altitude_zero.json telemetry_latest.json; do
    if [[ -e "${site_dir}/${state_name}" ]]; then
      mv "${site_dir}/${state_name}" "$state_tmp/"
    fi
  done
fi
rm -rf "$site_dir"
cp -a "${repo_root}/remote/uhrkgc/uhrk_site" "$site_dir"
if [[ -n "$state_tmp" ]]; then
  for state_path in "$state_tmp"/*; do
    if [[ -e "$state_path" ]]; then
      mv "$state_path" "$site_dir/"
    fi
  done
  rmdir "$state_tmp"
fi
mkdir -p "${site_dir}/flight_logs"

cat >/etc/uhrk/gc.env <<EOF
UHRK_UDP_HOST=${UHRK_UDP_HOST:-127.0.0.1}
UHRK_UDP_PORT=${UHRK_UDP_PORT:-1700}
UHRK_GROUND_GPS_PORT=${gps_port}
UHRK_CONTROL_HOST=${UHRK_CONTROL_HOST:-0.0.0.0}
UHRK_CONTROL_PORT=${UHRK_CONTROL_PORT:-8090}
UHRK_FLIGHT_LOG_DIR=${UHRK_FLIGHT_LOG_DIR:-${site_dir}/flight_logs}
UHRK_SETTINGS_FILE=${UHRK_SETTINGS_FILE:-${site_dir}/event_settings.json}
UHRK_ALTITUDE_ZERO_FILE=${UHRK_ALTITUDE_ZERO_FILE:-${site_dir}/altitude_zero.json}
UHRK_SHUTDOWN_PHRASE="${UHRK_SHUTDOWN_PHRASE:-SHUTDOWN UHRK}"
UHRK_LORA_DOWNLINK_FREQ_MHZ=${UHRK_LORA_DOWNLINK_FREQ_MHZ:-868.1}
UHRK_LORA_DOWNLINK_POWER_DBM=${UHRK_LORA_DOWNLINK_POWER_DBM:-14}
UHRK_LORA_DOWNLINK_DATARATE=${UHRK_LORA_DOWNLINK_DATARATE:-SF10BW125}
UHRK_LORA_DOWNLINK_CODING_RATE=${UHRK_LORA_DOWNLINK_CODING_RATE:-4/5}
UHRK_LORA_DOWNLINK_PREAMBLE=${UHRK_LORA_DOWNLINK_PREAMBLE:-8}
UHRK_LORA_DOWNLINK_REPEATS=${UHRK_LORA_DOWNLINK_REPEATS:-3}
UHRK_LORA_DOWNLINK_REPEAT_DELAY_S=${UHRK_LORA_DOWNLINK_REPEAT_DELAY_S:-1.0}
UHRK_NODE_TIME_SYNC_URLS=${UHRK_NODE_TIME_SYNC_URLS:-http://10.42.0.78:8091/api/time-sync}
EOF

cat >/etc/systemd/system/uhrk-backend.service <<EOF
[Unit]
Description=UHRK SX1303 telemetry backend
After=network-online.target
Wants=network-online.target
Conflicts=chirpstack-gateway-bridge.service

[Service]
Type=simple
User=${user_name}
WorkingDirectory=${site_dir}
Environment=PYTHONUNBUFFERED=1
EnvironmentFile=-/etc/uhrk/gc.env
ExecStart=/usr/bin/python3 ${site_dir}/uhrk_backend.py
Restart=always
RestartSec=2

[Install]
WantedBy=multi-user.target
EOF

cat >/etc/systemd/system/uhrk-web.service <<EOF
[Unit]
Description=UHRK flight monitor web server
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=${user_name}
WorkingDirectory=${site_dir}
Environment=PYTHONUNBUFFERED=1
ExecStart=/usr/bin/python3 -m http.server 8000 --bind 0.0.0.0
Restart=always
RestartSec=2

[Install]
WantedBy=multi-user.target
EOF

if [[ -n "$gateway_dir" ]]; then
  packet_forwarder_dir="${gateway_dir}/packet_forwarder"
  packet_forwarder_bin="${packet_forwarder_dir}/lora_pkt_fwd"
  packet_forwarder_conf="${packet_forwarder_dir}/test_conf.json"
  if [[ ! -x "$packet_forwarder_bin" ]]; then
    echo "Gateway binary not found or not executable: $packet_forwarder_bin" >&2
    echo "Install/build the SX1302/SX1303 HAL first, then rerun with --gateway-dir." >&2
    exit 1
  fi
  if [[ -f "${repo_root}/remote/uhrkgc/test_conf.json" ]]; then
    cp "${repo_root}/remote/uhrkgc/test_conf.json" "$packet_forwarder_conf"
  fi
  cat >/etc/systemd/system/lora-pkt-fwd.service <<EOF
[Unit]
Description=SX1303 LoRa Packet Forwarder
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=${user_name}
WorkingDirectory=${packet_forwarder_dir}
ExecStart=${packet_forwarder_bin} -c ${packet_forwarder_conf}
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF
fi

cat >/etc/sudoers.d/uhrk-gc <<EOF
${user_name} ALL=(root) NOPASSWD: /usr/sbin/shutdown, /sbin/shutdown, /usr/bin/date, /bin/date
EOF
chmod 0440 /etc/sudoers.d/uhrk-gc
visudo -cf /etc/sudoers.d/uhrk-gc

chown -R "${user_name}:${user_name}" "$site_dir"

if [[ "$setup_hotspot" -eq 1 ]]; then
  hotspot_args=(--ssid "$hotspot_ssid" --password "$hotspot_pass" --channel "$hotspot_channel")
  if [[ -n "$hotspot_iface" ]]; then
    hotspot_args+=(--interface "$hotspot_iface")
  fi
  "${repo_root}/install/setup_gc_hotspot.sh" "${hotspot_args[@]}"
fi

systemctl daemon-reload
systemctl enable uhrk-backend.service uhrk-web.service
systemctl restart uhrk-backend.service uhrk-web.service
if [[ -n "$gateway_dir" ]]; then
  systemctl enable lora-pkt-fwd.service
  systemctl restart lora-pkt-fwd.service
fi

echo "UHRK ground station installed."
echo "Dashboard: http://<ground-station-ip>:8000/"
if [[ "$setup_hotspot" -eq 1 ]]; then
  echo "Hotspot dashboard: http://10.42.0.1:8000/"
fi
echo "Backend service: systemctl status uhrk-backend"
echo "Web service: systemctl status uhrk-web"
echo "Environment: /etc/uhrk/gc.env"
