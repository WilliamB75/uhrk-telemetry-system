"""
main.py
========

Entry point for the Zenith telemetry node. This script ties together
the configuration, sensor readers, event detector, packet packer and
radio transmitter. It runs an infinite loop, gathering sensor data,
deriving events, packing a binary payload and sending it over LoRa on
a fixed cadence. If any individual sensor read fails the last
successful values are reused, ensuring the loop never stalls.

The ``device_id`` used for packets is defined in :mod:`config`. Make
sure to adjust it before deploying additional nodes (0 = Booster,
1 = Sustainer, 2 = Payload).

To run this script on the Pi, install the required packages listed in
the README and invoke it with Python 3. Press Ctrl‑C to exit
gracefully.
"""

from __future__ import annotations

import base64
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import signal
import socket
import struct
import subprocess
import threading
import time

from .config import Config
from .gps_reader import GPSReader
from .imu_baro_reader import IMUBaroReader
from .event_logic import EventDetector
from .packet import pack_payload
from .radio import LoRaRadio

CONTROL_HOST = os.environ.get("UHRK_NODE_CONTROL_HOST", "0.0.0.0")
CONTROL_PORT = int(os.environ.get("UHRK_NODE_CONTROL_PORT", "8091"))
LOG_DIR = Path(os.environ.get("UHRK_FLIGHT_LOG_DIR", str(Path.home() / "flight_logs")))
PAD_IDLE_FLAG = 1 << 6
PAD_LAUNCH_READY_FLAG = 1 << 7
PAD_IDLE = "idle"
PAD_LAUNCH_READY = "launch_ready"
# Compact GC-to-node command packets. These ride over the same RFM9x radio as
# telemetry, so they must stay tiny and must be safe to repeat.
COMMAND_MAGIC = b"UHRKC1"
COMMAND_FORMAT = ">6sBBBH"
COMMAND_LEN = struct.calcsize(COMMAND_FORMAT) + 2
COMMAND_PAD_STATE = 1
COMMAND_SHUTDOWN = 2
COMMAND_BROADCAST_DEVICE = 0xFF


def current_event_flags(flight_flags: int, pad_flags: int) -> int:
    """Return the single event flag that should be transmitted now."""
    return flight_flags if flight_flags else pad_flags


def command_checksum(data: bytes) -> int:
    return sum(data) & 0xFFFF


def decode_lora_command(packet: bytes, device_id: int) -> dict[str, int] | None:
    """Validate and decode one LoRa downlink command for this node."""
    # Adafruit RFM9x can include a 4-byte RadioHead-style header before the
    # payload. The GC includes it for compatibility, so strip it if present.
    if len(packet) >= 4 + COMMAND_LEN and packet[4:10] == COMMAND_MAGIC:
        packet = packet[4:]
    if len(packet) != COMMAND_LEN or packet[:6] != COMMAND_MAGIC:
        return None

    body = packet[:-2]
    received_checksum = struct.unpack(">H", packet[-2:])[0]
    if received_checksum != command_checksum(body):
        return None

    _magic, target, command_id, value, nonce = struct.unpack(COMMAND_FORMAT, body)
    if target not in (COMMAND_BROADCAST_DEVICE, device_id):
        return None
    return {
        "target": target,
        "commandId": command_id,
        "value": value,
        "nonce": nonce,
    }


def handle_lora_command(
    command: dict[str, int],
    pad_state: "PadState",
    flight_log: FlightLogger,
    stop_event: threading.Event,
) -> bool:
    """Apply a validated LoRa command.

    Returns True only when the command was understood. The caller uses that to
    remember the nonce and ignore repeated downlink attempts from the GC.
    """
    if command["commandId"] == COMMAND_PAD_STATE:
        if command["value"] == 0:
            mode = PAD_IDLE
        elif command["value"] == 1:
            mode = PAD_LAUNCH_READY
        else:
            return False
        pad_state.set_mode(mode)
        flight_log.append("lora_command", {
            "command": "pad_state",
            "mode": mode,
            "target": command["target"],
            "nonce": command["nonce"],
        })
        return True
    if command["commandId"] == COMMAND_SHUTDOWN:
        if command["value"] not in (0, 1):
            return False
        dry_run = command["value"] == 0
        response = {
            "command": "shutdown",
            "dryRun": dry_run,
            "target": command["target"],
            "nonce": command["nonce"],
            "hostname": socket.gethostname(),
            "logPath": str(flight_log.path),
            "shutdownScheduled": not dry_run,
        }
        flight_log.append("lora_command", response)
        flight_log.append("shutdown_request", {
            "dryRun": dry_run,
            "remote": "lora",
            "nonce": command["nonce"],
        })
        if not dry_run:
            # Log before stopping so recovery crews can confirm why the node
            # shut down even if power is removed moments later.
            flight_log.append("shutdown_commit", response)
            stop_event.set()
            run_shutdown_after_delay(4.0)
        return True
    return False


class FlightLogger:
    def __init__(self, log_dir: Path) -> None:
        log_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        hostname = socket.gethostname()
        self.path = log_dir / f"node_{hostname}_{stamp}.jsonl"
        self._lock = threading.Lock()
        self._file = self.path.open("a", encoding="utf-8")
        self.append("system", {"event": "logger_started", "path": str(self.path)})

    def append(self, kind: str, data: dict[str, object]) -> None:
        record = {
            "type": kind,
            "loggedUtc": datetime.now(timezone.utc).isoformat(),
            "data": data,
        }
        line = json.dumps(record, separators=(",", ":"), ensure_ascii=True)
        with self._lock:
            self._file.write(line + "\n")
            self._file.flush()
            # Flight logs matter more than write speed; force the current line
            # to disk so a later power-down does not lose the latest packet.
            os.fsync(self._file.fileno())

    def close(self) -> None:
        with self._lock:
            self._file.flush()
            os.fsync(self._file.fileno())
            self._file.close()


class PadState:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._mode = PAD_IDLE

    def set_mode(self, mode: str) -> str:
        if mode not in (PAD_IDLE, PAD_LAUNCH_READY):
            raise ValueError("mode must be idle or launch_ready")
        with self._lock:
            self._mode = mode
            return self._mode

    def snapshot(self) -> dict[str, object]:
        with self._lock:
            mode = self._mode
        return {
            "mode": mode,
            "onPadIdle": mode == PAD_IDLE,
            "onPadLaunchReady": mode == PAD_LAUNCH_READY,
        }

    def flags(self) -> int:
        with self._lock:
            return PAD_LAUNCH_READY_FLAG if self._mode == PAD_LAUNCH_READY else PAD_IDLE_FLAG


def run_shutdown_after_delay(delay_s: float) -> None:
    def worker() -> None:
        time.sleep(delay_s)
        for command in (["sudo", "/usr/sbin/shutdown", "-h", "now"], ["sudo", "/sbin/shutdown", "-h", "now"]):
            try:
                subprocess.Popen(command)
                return
            except OSError:
                continue

    threading.Thread(target=worker, daemon=True).start()


def set_system_time(epoch: float) -> dict[str, object]:
    if epoch < 1700000000 or epoch > 2200000000:
        return {"ok": False, "error": "epoch outside expected range"}
    for command in (
        ["sudo", "/usr/bin/date", "-u", "-s", f"@{epoch:.3f}"],
        ["sudo", "/bin/date", "-u", "-s", f"@{epoch:.3f}"],
    ):
        try:
            result = subprocess.run(command, check=False, capture_output=True, text=True, timeout=5)
        except (OSError, subprocess.SubprocessError) as exc:
            last_error = str(exc)
            continue
        if result.returncode == 0:
            return {
                "ok": True,
                "epoch": epoch,
                "systemUtc": datetime.now(timezone.utc).isoformat(),
                "stdout": result.stdout.strip(),
            }
        last_error = result.stderr.strip() or result.stdout.strip() or f"date exited {result.returncode}"
    return {"ok": False, "error": last_error}


def start_control_server(flight_log: FlightLogger, pad_state: PadState) -> ThreadingHTTPServer:
    class ControlHandler(BaseHTTPRequestHandler):
        server_version = "UHRKNodeControl/1.0"

        def log_message(self, fmt: str, *args: object) -> None:
            print(f"control: {fmt % args}", flush=True)

        def _send_json(self, status: int, payload: dict[str, object]) -> None:
            data = json.dumps(payload, separators=(",", ":"), ensure_ascii=True).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Access-Control-Allow-Methods", "GET,POST,OPTIONS")
            self.send_header("Access-Control-Allow-Headers", "Content-Type")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_OPTIONS(self) -> None:
            self._send_json(204, {})

        def do_GET(self) -> None:
            path = self.path.split("?", 1)[0]
            if path == "/api/pad-state":
                self._send_json(200, {"ok": True, "padState": pad_state.snapshot()})
                return
            if path == "/api/time-sync/status":
                self._send_json(200, {
                    "ok": True,
                    "hostname": socket.gethostname(),
                    "systemUtc": datetime.now(timezone.utc).isoformat(),
                    "logPath": str(flight_log.path),
                })
                return
            # Shutdown is intentionally not exposed over this WiFi API. The
            # operational shutdown path is the guarded GC dashboard over LoRa.
            self._send_json(404, {"ok": False, "error": "not found"})

        def do_POST(self) -> None:
            path = self.path.split("?", 1)[0]
            if path == "/api/pad-state":
                length = int(self.headers.get("Content-Length", "0") or "0")
                try:
                    payload = json.loads(self.rfile.read(length).decode("utf-8") or "{}")
                    mode = str(payload.get("mode") or "")
                    current_mode = pad_state.set_mode(mode)
                except (json.JSONDecodeError, ValueError) as exc:
                    self._send_json(400, {"ok": False, "error": str(exc)})
                    return
                response = {"ok": True, "hostname": socket.gethostname(), "padState": pad_state.snapshot()}
                flight_log.append("pad_state", {"remote": self.client_address[0], "mode": current_mode})
                self._send_json(200, response)
                return
            if path == "/api/time-sync":
                length = int(self.headers.get("Content-Length", "0") or "0")
                try:
                    payload = json.loads(self.rfile.read(length).decode("utf-8") or "{}")
                    epoch = float(payload.get("epoch"))
                except (json.JSONDecodeError, TypeError, ValueError):
                    self._send_json(400, {"ok": False, "error": "valid epoch required"})
                    return
                before = datetime.now(timezone.utc).isoformat()
                result = set_system_time(epoch)
                result.update({
                    "hostname": socket.gethostname(),
                    "beforeUtc": before,
                    "source": payload.get("source") or "gc",
                })
                flight_log.append("time_sync", {
                    "remote": self.client_address[0],
                    "request": payload,
                    "result": result,
                })
                self._send_json(200 if result.get("ok") else 500, result)
                return
            # Keep the node HTTP API limited to bench functions. A flight or
            # recovery shutdown should always go through the LoRa command path.
            self._send_json(404, {"ok": False, "error": "not found"})

    server = ThreadingHTTPServer((CONTROL_HOST, CONTROL_PORT), ControlHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    print(f"UHRK node control API listening on http://{CONTROL_HOST}:{CONTROL_PORT}", flush=True)
    return server


def main() -> None:
    """Run the telemetry loop until interrupted."""
    cfg = Config()
    stop_event = threading.Event()
    flight_log = FlightLogger(LOG_DIR)
    pad_state = PadState()
    control_server = start_control_server(flight_log, pad_state)

    def handle_signal(signum: int, _frame: object) -> None:
        flight_log.append("system", {"event": "signal", "signum": signum})
        stop_event.set()

    signal.signal(signal.SIGTERM, handle_signal)
    signal.signal(signal.SIGINT, handle_signal)

    # Instantiate modules
    gps_reader = GPSReader()
    imu_reader = IMUBaroReader(cfg)
    event_detector = EventDetector(cfg)
    radio = LoRaRadio(cfg)

    seq = 0
    last_command_nonce: int | None = None
    try:
        while not stop_event.is_set():
            # Read sensors
            gps_data = gps_reader.read()
            imu_data = imu_reader.read()
            # Derive events
            current_pad_state = pad_state.snapshot()
            flight_flags = event_detector.update(gps_data, imu_data, bool(current_pad_state["onPadLaunchReady"]))
            flags = current_event_flags(flight_flags, pad_state.flags())
            # Pack payload
            payload = pack_payload(
                device_id=cfg.DEVICE_ID,
                seq=seq,
                lat=gps_data.lat,
                lon=gps_data.lon,
                gps_alt_m=gps_data.alt_m,
                baro_alt_m=imu_data.baro_alt_m,
                imu_alt_m=imu_data.imu_alt_m,
                gps_status=gps_data.status,
                sats=gps_data.sats_used,
                sats_in_view=gps_data.sats_in_view,
                ax=imu_data.ax,
                ay=imu_data.ay,
                az=imu_data.az,
                gx=imu_data.gx,
                gy=imu_data.gy,
                gz=imu_data.gz,
                event_flags=flags,
                config=cfg,
            )
            # Transmit over LoRa
            radio.send(payload)
            flight_log.append("packet", {
                "deviceId": cfg.DEVICE_ID,
                "seq": seq,
                "lat": gps_data.lat,
                "lon": gps_data.lon,
                "gpsAltM": gps_data.alt_m,
                "baroAltM": imu_data.baro_alt_m,
                "imuAltM": imu_data.imu_alt_m,
                "gpsStatus": gps_data.status,
                "sats": gps_data.sats,
                "satsUsed": gps_data.sats_used,
                "satsInView": gps_data.sats_in_view,
                "gpsHdop": gps_data.hdop,
                "gpsPdop": gps_data.pdop,
                "gpsVdop": gps_data.vdop,
                "ax": imu_data.ax,
                "ay": imu_data.ay,
                "az": imu_data.az,
                "gx": imu_data.gx,
                "gy": imu_data.gy,
                "gz": imu_data.gz,
                "eventFlags": flags,
                "padState": current_pad_state,
                "payloadBase64": base64.b64encode(payload).decode("ascii"),
            })
            # Advance sequence counter (wrap at 16 bits)
            seq = (seq + 1) & 0xFFFF
            # Listen for GC commands over LoRa until the next telemetry slot.
            # This keeps command handling responsive without adding a second
            # radio or thread that could fight the transmit path for SPI.
            if current_pad_state["onPadLaunchReady"]:
                cadence = cfg.LAUNCH_READY_CADENCE_SECONDS + cfg.DEVICE_ID * cfg.LAUNCH_READY_DEVICE_SLOT_SECONDS
            else:
                cadence = cfg.CADENCE_SECONDS
            deadline = time.monotonic() + cadence
            while not stop_event.is_set():
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                packet = radio.receive(remaining)
                if packet is None:
                    continue
                command = decode_lora_command(packet, cfg.DEVICE_ID)
                if command is None:
                    continue
                if command["nonce"] == last_command_nonce:
                    continue
                if handle_lora_command(command, pad_state, flight_log, stop_event):
                    last_command_nonce = command["nonce"]
    except KeyboardInterrupt:
        # Graceful exit
        pass
    finally:
        flight_log.append("system", {"event": "node_stopping"})
        flight_log.close()
        control_server.shutdown()
        # Stop the GPS reader thread and close the serial port
        try:
            gps_reader.close()
        except Exception:
            pass


if __name__ == '__main__':
    main()
