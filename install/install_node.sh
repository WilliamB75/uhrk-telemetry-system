#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'USAGE'
Usage: sudo ./install/install_node.sh [options]

Install the UHRK telemetry node service on a Raspberry Pi.

Options:
  --device-id N          Node/stage ID: 0=Booster, 1=Sustainer, 2=Payload
  --frequency MHz       LoRa frequency in MHz, default 868.1
  --user NAME           Linux user to run the node service, default uhrk
  --install-dir PATH    Install root, default /opt/uhrk
  --log-dir PATH        Flight log directory, default /home/<user>/flight_logs
  --help                Show this help

Example:
  sudo ./install/install_node.sh --device-id 0 --frequency 868.1 --user uhrkboo
USAGE
}

require_root() {
  if [[ "$(id -u)" -ne 0 ]]; then
    echo "Run this installer with sudo." >&2
    exit 1
  fi
}

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
user_name="${UHRK_NODE_USER:-uhrk}"
device_id="${UHRK_DEVICE_ID:-0}"
frequency="${UHRK_RADIO_FREQ_MHZ:-868.1}"
install_dir="${UHRK_INSTALL_DIR:-/opt/uhrk}"
log_dir=""

while [[ $# -gt 0 ]]; do
  case "$1" in
    --device-id) device_id="$2"; shift 2 ;;
    --frequency) frequency="$2"; shift 2 ;;
    --user) user_name="$2"; shift 2 ;;
    --install-dir) install_dir="$2"; shift 2 ;;
    --log-dir) log_dir="$2"; shift 2 ;;
    --help) usage; exit 0 ;;
    *) echo "Unknown option: $1" >&2; usage; exit 1 ;;
  esac
done

require_root

if [[ -z "$log_dir" ]]; then
  log_dir="/home/${user_name}/flight_logs"
fi

if [[ ! -d "${repo_root}/remote/uhrkboo/zenith_node" ]]; then
  echo "Cannot find node source under ${repo_root}/remote/uhrkboo/zenith_node" >&2
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
apt-get install -y python3 python3-venv python3-pip python3-dev git i2c-tools

mkdir -p "$install_dir" /etc/uhrk "$log_dir"
rm -rf "${install_dir}/zenith_node"
cp -a "${repo_root}/remote/uhrkboo/zenith_node" "${install_dir}/zenith_node"

python3 -m venv "${install_dir}/node-venv"
"${install_dir}/node-venv/bin/python" -m pip install --upgrade pip wheel
"${install_dir}/node-venv/bin/python" -m pip install \
  pyserial \
  pynmea2 \
  smbus2 \
  adafruit-blinka \
  adafruit-circuitpython-rfm9x \
  adafruit-circuitpython-bmp3xx

cat >/etc/uhrk/node.env <<EOF
UHRK_DEVICE_ID=${device_id}
UHRK_RADIO_FREQ_MHZ=${frequency}
UHRK_SIGNAL_BANDWIDTH=${UHRK_SIGNAL_BANDWIDTH:-125000}
UHRK_CODING_RATE=${UHRK_CODING_RATE:-5}
UHRK_SPREADING_FACTOR=${UHRK_SPREADING_FACTOR:-10}
UHRK_PREAMBLE_LENGTH=${UHRK_PREAMBLE_LENGTH:-8}
UHRK_SYNC_WORD=${UHRK_SYNC_WORD:-52}
UHRK_TX_POWER=${UHRK_TX_POWER:-20}
UHRK_CADENCE_SECONDS=${UHRK_CADENCE_SECONDS:-1.0}
UHRK_LAUNCH_READY_CADENCE_SECONDS=${UHRK_LAUNCH_READY_CADENCE_SECONDS:-0.45}
UHRK_LAUNCH_READY_DEVICE_SLOT_SECONDS=${UHRK_LAUNCH_READY_DEVICE_SLOT_SECONDS:-0.12}
UHRK_NODE_CONTROL_HOST=${UHRK_NODE_CONTROL_HOST:-0.0.0.0}
UHRK_NODE_CONTROL_PORT=${UHRK_NODE_CONTROL_PORT:-8091}
UHRK_FLIGHT_LOG_DIR=${log_dir}
UHRK_GRAVITY=${UHRK_GRAVITY:-9.80665}
UHRK_BURN_ACTIVE_ACC_THRESHOLD=${UHRK_BURN_ACTIVE_ACC_THRESHOLD:-15.0}
UHRK_BURN_OUT_ACC_THRESHOLD=${UHRK_BURN_OUT_ACC_THRESHOLD:-3.0}
UHRK_LAUNCH_CONFIRM_SAMPLES=${UHRK_LAUNCH_CONFIRM_SAMPLES:-3}
UHRK_MIN_LAUNCH_ALTITUDE_DELTA=${UHRK_MIN_LAUNCH_ALTITUDE_DELTA:-8.0}
UHRK_STAGE_SEP_ALTITUDE=${UHRK_STAGE_SEP_ALTITUDE:-3000.0}
UHRK_DROGUE_DROP_ALT=${UHRK_DROGUE_DROP_ALT:-50.0}
UHRK_MAIN_DEPLOY_ALTITUDE=${UHRK_MAIN_DEPLOY_ALTITUDE:-500.0}
UHRK_LANDED_ALTITUDE=${UHRK_LANDED_ALTITUDE:-5.0}
UHRK_LANDED_VSPEED_THRESHOLD=${UHRK_LANDED_VSPEED_THRESHOLD:-1.0}
EOF

cat >/etc/systemd/system/uhrk-node.service <<EOF
[Unit]
Description=UHRK telemetry node
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=${user_name}
WorkingDirectory=${install_dir}
Environment=PYTHONUNBUFFERED=1
Environment=PYTHONPATH=${install_dir}
EnvironmentFile=-/etc/uhrk/node.env
ExecStart=${install_dir}/node-venv/bin/python -m zenith_node.main
Restart=always
RestartSec=2

[Install]
WantedBy=multi-user.target
EOF

cat >/etc/sudoers.d/uhrk-node <<EOF
${user_name} ALL=(root) NOPASSWD: /usr/sbin/shutdown, /sbin/shutdown, /usr/bin/date, /bin/date
EOF
chmod 0440 /etc/sudoers.d/uhrk-node
visudo -cf /etc/sudoers.d/uhrk-node

chown -R "${user_name}:${user_name}" "$install_dir" "$log_dir"

systemctl daemon-reload
systemctl enable uhrk-node.service
systemctl restart uhrk-node.service

echo "UHRK node installed."
echo "Service: systemctl status uhrk-node"
echo "Environment: /etc/uhrk/node.env"
echo "Logs: ${log_dir}"
