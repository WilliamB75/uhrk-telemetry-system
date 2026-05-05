#!/usr/bin/env python3
"""UHRK SX1303 packet-forwarder telemetry backend.

The Pi5 ground station uses an SX1303 LoRa gateway HAT, so packets arrive from
the Semtech UDP packet forwarder rather than from a directly attached RFM9x
radio. This backend binds to the local packet-forwarder UDP port, acknowledges
gateway traffic, decodes the 39-byte UHRK telemetry payload, and writes the JSON
file consumed by the web dashboard.
"""

from __future__ import annotations

import base64
import binascii
import copy
import csv
import html
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
import json
import math
import os
import shutil
import socket
import struct
import subprocess
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

try:
    import termios
except ImportError:  # pragma: no cover - only used when helper tests run off-Pi.
    termios = None  # type: ignore[assignment]


UDP_HOST = os.environ.get("UHRK_UDP_HOST", "127.0.0.1")
UDP_PORT = int(os.environ.get("UHRK_UDP_PORT", "1700"))
OUTPUT_FILE = Path(__file__).resolve().parent / "telemetry_latest.json"
APP_VERSION = os.environ.get("UHRK_VERSION", "2026.05.05-observability")
WRITE_INTERVAL_S = 0.5
OFFLINE_TIMEOUT_S = 10.0
HISTORY_LENGTH = 50
GROUND_GPS_PORT = os.environ.get("UHRK_GROUND_GPS_PORT", "/dev/ttyAMA0")
CONTROL_HOST = os.environ.get("UHRK_CONTROL_HOST", "0.0.0.0")
CONTROL_PORT = int(os.environ.get("UHRK_CONTROL_PORT", "8090"))
NODE_TIME_SYNC_URLS = [
    url.strip()
    for url in os.environ.get("UHRK_NODE_TIME_SYNC_URLS", "http://10.42.0.78:8091/api/time-sync").split(",")
    if url.strip()
]
SHUTDOWN_PHRASE = os.environ.get("UHRK_SHUTDOWN_PHRASE", "SHUTDOWN UHRK")
LOG_DIR = Path(os.environ.get("UHRK_FLIGHT_LOG_DIR", str(Path(__file__).resolve().parent / "flight_logs")))
SETTINGS_FILE = Path(os.environ.get("UHRK_SETTINGS_FILE", str(Path(__file__).resolve().parent / "event_settings.json")))
ALTITUDE_ZERO_FILE = Path(os.environ.get("UHRK_ALTITUDE_ZERO_FILE", str(Path(__file__).resolve().parent / "altitude_zero.json")))
GRAVITY_REFERENCE = float(os.environ.get("UHRK_GRAVITY_REFERENCE", "9.80665"))
LINEAR_ACCEL_DEADBAND = float(os.environ.get("UHRK_LINEAR_ACCEL_DEADBAND", "0.25"))
VELOCITY_SMOOTHING_ALPHA = float(os.environ.get("UHRK_VELOCITY_SMOOTHING_ALPHA", "0.22"))
ALTITUDE_NOISE_DEADBAND_M = float(os.environ.get("UHRK_ALTITUDE_NOISE_DEADBAND_M", "0.35"))
STATIONARY_VELOCITY_DEADBAND_MPS = float(os.environ.get("UHRK_STATIONARY_VELOCITY_DEADBAND_MPS", "0.35"))
MAX_BARO_STEP_M = float(os.environ.get("UHRK_MAX_BARO_STEP_M", "25.0"))
MAX_GPS_STEP_M = float(os.environ.get("UHRK_MAX_GPS_STEP_M", "35.0"))
MAX_GPS_ALT_STEP_M = float(os.environ.get("UHRK_MAX_GPS_ALT_STEP_M", "30.0"))
KALMAN_BARO_VARIANCE_M2 = float(os.environ.get("UHRK_KALMAN_BARO_VARIANCE_M2", "2.25"))
KALMAN_ACCEL_VARIANCE_MPS2 = float(os.environ.get("UHRK_KALMAN_ACCEL_VARIANCE_MPS2", "4.0"))
KALMAN_PROCESS_ALTITUDE = float(os.environ.get("UHRK_KALMAN_PROCESS_ALTITUDE", "0.08"))
KALMAN_PROCESS_VELOCITY = float(os.environ.get("UHRK_KALMAN_PROCESS_VELOCITY", "1.0"))
KALMAN_PROCESS_ACCEL = float(os.environ.get("UHRK_KALMAN_PROCESS_ACCEL", "4.0"))

PUSH_DATA = 0x00
PUSH_ACK = 0x01
PULL_DATA = 0x02
PULL_RESP = 0x03
PULL_ACK = 0x04
TX_ACK = 0x05

PACKET_FORMAT = ">B H i i i i i B B h h h h h h H"
PACKET_LEN = struct.calcsize(PACKET_FORMAT)
COMMAND_MAGIC = b"UHRKC1"
COMMAND_FORMAT = ">6sBBBH"
COMMAND_PAD_STATE = 1
COMMAND_SHUTDOWN = 2
COMMAND_BROADCAST_DEVICE = 0xFF
DOWNLINK_FREQ_MHZ = float(os.environ.get("UHRK_LORA_DOWNLINK_FREQ_MHZ", "868.1"))
DOWNLINK_POWER_DBM = int(os.environ.get("UHRK_LORA_DOWNLINK_POWER_DBM", "14"))
DOWNLINK_DATARATE = os.environ.get("UHRK_LORA_DOWNLINK_DATARATE", "SF10BW125")
DOWNLINK_CODING_RATE = os.environ.get("UHRK_LORA_DOWNLINK_CODING_RATE", "4/5")
DOWNLINK_PREAMBLE = int(os.environ.get("UHRK_LORA_DOWNLINK_PREAMBLE", "8"))
DOWNLINK_REPEATS = int(os.environ.get("UHRK_LORA_DOWNLINK_REPEATS", "3"))
DOWNLINK_REPEAT_DELAY_S = float(os.environ.get("UHRK_LORA_DOWNLINK_REPEAT_DELAY_S", "1.0"))
DOWNLINK_READY_TIMEOUT_S = float(os.environ.get("UHRK_LORA_DOWNLINK_READY_TIMEOUT_S", "30"))
PAD_STATE_CONFIRM_TIMEOUT_S = float(os.environ.get("UHRK_PAD_STATE_CONFIRM_TIMEOUT_S", "3.0"))
GC_TIME_SYNC_INTERVAL_S = float(os.environ.get("UHRK_GC_TIME_SYNC_INTERVAL_S", "300"))
NODE_TIME_SYNC_INTERVAL_S = float(os.environ.get("UHRK_NODE_TIME_SYNC_INTERVAL_S", "60"))
GPS_TIME_MAX_AGE_S = float(os.environ.get("UHRK_GPS_TIME_MAX_AGE_S", "120"))

STAGE_NAMES: Dict[int, str] = {
    0: "Booster",
    1: "Sustainer",
    2: "Payload Bay",
}

EVENT_FLAG_NAMES: Dict[int, str] = {
    0: "Burn active",
    1: "Burnout",
    2: "Stage separation",
    3: "Drogue deployed",
    4: "Main deployed",
    5: "Landed",
    6: "On Pad Idle",
    7: "On Pad Launch Ready",
}


def current_event_bit(mask: int) -> Optional[int]:
    """Choose the one event bit that represents the current node state."""
    for bit in (5, 4, 3, 2, 1, 0):
        if mask & (1 << bit):
            return bit
    for bit in (7, 6):
        if mask & (1 << bit):
            return bit
    return None


def command_checksum(data: bytes) -> int:
    return sum(data) & 0xFFFF


def pack_lora_command(target_device: int, command_id: int, value: int, nonce: int) -> bytes:
    body = struct.pack(
        COMMAND_FORMAT,
        COMMAND_MAGIC,
        target_device & 0xFF,
        command_id & 0xFF,
        value & 0xFF,
        nonce & 0xFFFF,
    )
    checksum = struct.pack(">H", command_checksum(body))
    # Adafruit RFM9x receive() expects the RadioHead-compatible 4-byte header.
    return bytes([0xFF, 0x00, nonce & 0xFF, 0x00]) + body + checksum


def pad_mode_from_event(event_name: Optional[str]) -> Optional[str]:
    if event_name == "On Pad Idle":
        return "idle"
    if event_name == "On Pad Launch Ready":
        return "launch_ready"
    return None

DEFAULT_SETTINGS: Dict[str, Any] = {
    "version": 1,
    "sensor": {
        "gravityMps2": GRAVITY_REFERENCE,
        "linearAccelDeadbandMps2": LINEAR_ACCEL_DEADBAND,
        "velocitySmoothingAlpha": VELOCITY_SMOOTHING_ALPHA,
        "altitudeNoiseDeadbandM": ALTITUDE_NOISE_DEADBAND_M,
        "stationaryVelocityDeadbandMps": STATIONARY_VELOCITY_DEADBAND_MPS,
        "maxBaroStepM": MAX_BARO_STEP_M,
        "maxGpsStepM": MAX_GPS_STEP_M,
        "maxGpsAltStepM": MAX_GPS_ALT_STEP_M,
        "kalmanBaroVarianceM2": KALMAN_BARO_VARIANCE_M2,
        "kalmanAccelVarianceMps2": KALMAN_ACCEL_VARIANCE_MPS2,
        "kalmanProcessAltitude": KALMAN_PROCESS_ALTITUDE,
        "kalmanProcessVelocity": KALMAN_PROCESS_VELOCITY,
        "kalmanProcessAccel": KALMAN_PROCESS_ACCEL,
    },
    "events": [
        {"id": "launch", "label": "Launch detect", "stage": "All", "accelAboveG": 2.5, "minDurationMs": 120},
        {"id": "booster_burnout", "label": "Booster burnout", "stage": "Booster", "accelBelowG": 0.35, "minDurationMs": 250},
        {"id": "second_motor_ignition", "label": "Second motor ignition", "stage": "Sustainer", "accelAboveG": 2.0, "minDurationMs": 120},
        {"id": "sustainer_burnout", "label": "Sustainer burnout", "stage": "Sustainer", "accelBelowG": 0.35, "minDurationMs": 250},
        {"id": "apogee", "label": "Apogee", "stage": "All", "verticalVelocityBelowMps": 0.0, "altitudeDropM": 8.0},
        {"id": "landing", "label": "Landing", "stage": "All", "altitudeBelowM": 5.0, "verticalSpeedBelowMps": 1.0},
    ],
    "chutes": [
        {"id": "booster_drogue", "stage": "Booster", "name": "Drogue", "deployAt": "Apogee", "altitudeM": None},
        {"id": "booster_main", "stage": "Booster", "name": "Main", "deployAt": "Altitude", "altitudeM": 500.0},
        {"id": "sustainer_drogue", "stage": "Sustainer", "name": "Drogue", "deployAt": "Apogee", "altitudeM": None},
        {"id": "sustainer_main", "stage": "Sustainer", "name": "Main", "deployAt": "Altitude", "altitudeM": 500.0},
        {"id": "payload_drogue", "stage": "Payload", "name": "Drogue", "deployAt": "Apogee", "altitudeM": None},
        {"id": "payload_main", "stage": "Payload", "name": "Main", "deployAt": "Altitude", "altitudeM": 500.0},
    ],
}


def deep_merge(default: Dict[str, Any], loaded: Dict[str, Any]) -> Dict[str, Any]:
    merged = copy.deepcopy(default)
    for key, value in loaded.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = deep_merge(merged[key], value)
        else:
            merged[key] = value
    return merged


def load_settings() -> Dict[str, Any]:
    if not SETTINGS_FILE.exists():
        return copy.deepcopy(DEFAULT_SETTINGS)
    try:
        with SETTINGS_FILE.open("r", encoding="utf-8") as f:
            loaded = json.load(f)
        if isinstance(loaded, dict):
            return deep_merge(DEFAULT_SETTINGS, loaded)
    except (OSError, json.JSONDecodeError):
        pass
    return copy.deepcopy(DEFAULT_SETTINGS)


def save_settings(settings: Dict[str, Any]) -> Dict[str, Any]:
    settings = copy.deepcopy(settings)
    settings["version"] = int(settings.get("version") or DEFAULT_SETTINGS["version"])
    SETTINGS_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = SETTINGS_FILE.with_suffix(".tmp")
    with tmp_path.open("w", encoding="utf-8") as f:
        json.dump(settings, f, indent=2, ensure_ascii=True)
        f.write("\n")
    os.replace(tmp_path, SETTINGS_FILE)
    return settings


def load_altitude_zero() -> Dict[str, Any]:
    if not ALTITUDE_ZERO_FILE.exists():
        return {"version": 1, "stages": {}}
    try:
        with ALTITUDE_ZERO_FILE.open("r", encoding="utf-8") as f:
            loaded = json.load(f)
        if isinstance(loaded, dict):
            loaded.setdefault("version", 1)
            loaded.setdefault("stages", {})
            return loaded
    except (OSError, json.JSONDecodeError):
        pass
    return {"version": 1, "stages": {}}


def save_altitude_zero(data: Dict[str, Any]) -> Dict[str, Any]:
    data = copy.deepcopy(data)
    data["version"] = int(data.get("version") or 1)
    data.setdefault("stages", {})
    ALTITUDE_ZERO_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = ALTITUDE_ZERO_FILE.with_suffix(".tmp")
    with tmp_path.open("w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=True)
        f.write("\n")
    os.replace(tmp_path, ALTITUDE_ZERO_FILE)
    return data


def sensor_float(key: str, default: float) -> float:
    try:
        value = load_settings().get("sensor", {}).get(key, default)
        return float(value)
    except (TypeError, ValueError):
        return default


class FlightLogger:
    def __init__(self, log_dir: Path, prefix: str) -> None:
        log_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        self.path = log_dir / f"{prefix}_{stamp}.jsonl"
        self._lock = threading.Lock()
        self._file = self.path.open("a", encoding="utf-8")
        self.append("system", {"event": "logger_started", "path": str(self.path)})

    def append(self, kind: str, data: Dict[str, object]) -> None:
        record = {
            "type": kind,
            "loggedUtc": datetime.now(timezone.utc).isoformat(),
            "data": data,
        }
        line = json.dumps(record, separators=(",", ":"), ensure_ascii=True)
        with self._lock:
            self._file.write(line + "\n")
            self._file.flush()
            os.fsync(self._file.fileno())

    def close(self) -> None:
        with self._lock:
            self._file.flush()
            os.fsync(self._file.fileno())
            self._file.close()


class LoRaDownlink:
    """Send small GC-to-node commands through the SX1303 packet forwarder."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._sock: Optional[socket.socket] = None
        self._addr: Optional[tuple[str, int]] = None
        self._gateway_id: Optional[str] = None
        self._last_pull_mono = 0.0
        self._nonce = int.from_bytes(os.urandom(2), "big")
        self._last_tx_ack: Optional[Dict[str, object]] = None
        self._last_command: Optional[Dict[str, object]] = None

    def attach_socket(self, sock: socket.socket) -> None:
        with self._lock:
            self._sock = sock

    def record_pull_data(self, data: bytes, addr: tuple[str, int]) -> None:
        # The packet forwarder periodically sends PULL_DATA to tell us where
        # PULL_RESP downlinks should be sent. Without a recent keepalive,
        # downlink commands are refused rather than disappearing silently.
        gateway_id = gateway_id_from_push(data)
        with self._lock:
            self._addr = addr
            self._gateway_id = gateway_id or self._gateway_id
            self._last_pull_mono = time.monotonic()

    def record_tx_ack(self, data: bytes) -> None:
        payload: Dict[str, object] = {}
        if len(data) > 12:
            try:
                parsed = json.loads(data[12:].decode("utf-8"))
                if isinstance(parsed, dict):
                    payload = parsed
            except (json.JSONDecodeError, UnicodeDecodeError):
                payload = {}
        with self._lock:
            self._last_tx_ack = {
                "receivedUtc": datetime.now(timezone.utc).isoformat(),
                "payload": payload,
            }

    def snapshot(self) -> Dict[str, object]:
        with self._lock:
            age_ms = int((time.monotonic() - self._last_pull_mono) * 1000) if self._last_pull_mono else None
            return {
                "transport": "lora",
                "ready": self._sock is not None and self._addr is not None and (
                    time.monotonic() - self._last_pull_mono
                ) <= DOWNLINK_READY_TIMEOUT_S,
                "gatewayId": self._gateway_id,
                "lastPullDataAgeMs": age_ms,
                "lastTxAck": copy.deepcopy(self._last_tx_ack),
                "lastCommand": copy.deepcopy(self._last_command),
            }

    def _send_command(self, command_id: int, value: int, label: str, details: Dict[str, object]) -> Dict[str, object]:
        with self._lock:
            if self._sock is None or self._addr is None:
                return {"ok": False, "transport": "lora", "error": "gateway downlink is not ready yet"}
            if (time.monotonic() - self._last_pull_mono) > DOWNLINK_READY_TIMEOUT_S:
                return {"ok": False, "transport": "lora", "error": "gateway downlink keepalive is stale"}
            sock = self._sock
            addr = self._addr
            self._nonce = (self._nonce + 1) & 0xFFFF
            nonce = self._nonce

        # Repeating the same nonce improves odds of reception while letting
        # nodes safely ignore duplicates.
        payload = pack_lora_command(COMMAND_BROADCAST_DEVICE, command_id, value, nonce)
        txpk = {
            "imme": True,
            "freq": DOWNLINK_FREQ_MHZ,
            "rfch": 0,
            "powe": DOWNLINK_POWER_DBM,
            "modu": "LORA",
            "datr": DOWNLINK_DATARATE,
            "codr": DOWNLINK_CODING_RATE,
            "ipol": False,
            "prea": DOWNLINK_PREAMBLE,
            "size": len(payload),
            "data": base64.b64encode(payload).decode("ascii"),
        }
        body = json.dumps({"txpk": txpk}, separators=(",", ":")).encode("utf-8")
        attempts: List[Dict[str, object]] = []
        for attempt in range(max(1, DOWNLINK_REPEATS)):
            token = os.urandom(2)
            packet = bytes([2]) + token + bytes([PULL_RESP]) + body
            try:
                sent = sock.sendto(packet, addr)
                attempts.append({"attempt": attempt + 1, "ok": True, "bytes": sent})
            except OSError as exc:
                attempts.append({"attempt": attempt + 1, "ok": False, "error": str(exc)})
            if attempt < max(1, DOWNLINK_REPEATS) - 1:
                time.sleep(DOWNLINK_REPEAT_DELAY_S)

        result = {
            "ok": any(attempt.get("ok") for attempt in attempts),
            "transport": "lora",
            "command": label,
            "commandId": command_id,
            "value": value,
            "targetDevice": "broadcast",
            "nonce": nonce,
            "attempts": attempts,
        }
        result.update(details)
        with self._lock:
            self._last_command = copy.deepcopy(result)
        return result

    def send_pad_state(self, mode: str) -> Dict[str, object]:
        value = 1 if mode == "launch_ready" else 0
        return self._send_command(COMMAND_PAD_STATE, value, "pad_state", {"mode": mode})

    def send_shutdown(self, dry_run: bool) -> Dict[str, object]:
        value = 0 if dry_run else 1
        return self._send_command(COMMAND_SHUTDOWN, value, "shutdown", {"dryRun": dry_run})


@dataclass
class StageState:
    id: int
    name: str
    deviceId: int
    seq: int = 0
    lat: Optional[float] = None
    lon: Optional[float] = None
    gpsAlt: Optional[float] = None
    baroAlt: Optional[float] = None
    imuAlt: Optional[float] = None
    fusedAlt: Optional[float] = None
    gpsRelAlt: Optional[float] = None
    baroRelAlt: Optional[float] = None
    imuRelAlt: Optional[float] = None
    fusedRelAlt: Optional[float] = None
    verticalVelocity: Optional[float] = None
    imuVelocity: Optional[float] = None
    accelMagnitude: Optional[float] = None
    linearAccel: Optional[float] = None
    rawLinearAccel: Optional[float] = None
    kalmanAlt: Optional[float] = None
    kalmanRelAlt: Optional[float] = None
    kalmanVelocity: Optional[float] = None
    kalmanAccel: Optional[float] = None
    gpsStatus: Optional[str] = None
    sats: Optional[int] = None
    satsUsed: Optional[int] = None
    satsInView: Optional[int] = None
    gpsQuality: Optional[str] = None
    ax: Optional[float] = None
    ay: Optional[float] = None
    az: Optional[float] = None
    gx: Optional[float] = None
    gy: Optional[float] = None
    gz: Optional[float] = None
    rssi: Optional[float] = None
    snr: Optional[float] = None
    frequencyMHz: Optional[float] = None
    dataRate: Optional[str] = None
    currentEvent: Optional[str] = None
    eventFlags: Dict[str, bool] = field(default_factory=dict)
    events: List[str] = field(default_factory=list)
    altitudeZero: Dict[str, Optional[float] | str] = field(default_factory=dict)
    readiness: Dict[str, object] = field(default_factory=dict)
    warnings: List[Dict[str, str]] = field(default_factory=list)
    history: List[Dict[str, Optional[float] | str]] = field(default_factory=list)
    packetsReceived: int = 0
    packetRateHz: Optional[float] = None
    last_update_mono: float = 0.0
    last_update_utc: Optional[str] = None
    prev_event_mask: int = 0
    _accepted_baro_alt: Optional[float] = None
    _accepted_gps_alt: Optional[float] = None
    _accepted_lat: Optional[float] = None
    _accepted_lon: Optional[float] = None
    _last_rejection: Optional[str] = None
    _last_gps_rejection: Optional[str] = None
    _kalman_x: Optional[List[float]] = None
    _kalman_p: Optional[List[List[float]]] = None
    _packet_times: List[float] = field(default_factory=list)

    def apply_altitude_zero(self, zero: Dict[str, Any]) -> None:
        self.altitudeZero = {
            "gpsAlt": _maybe_float(zero.get("gpsAlt")),
            "baroAlt": _maybe_float(zero.get("baroAlt")),
            "imuAlt": _maybe_float(zero.get("imuAlt")),
            "fusedAlt": _maybe_float(zero.get("fusedAlt")),
            "setUtc": str(zero.get("setUtc")) if zero.get("setUtc") else None,
        }

    def set_current_altitude_zero(self) -> Dict[str, object]:
        zero = {
            "gpsAlt": self.gpsAlt,
            "baroAlt": self.baroAlt,
            "imuAlt": self.imuAlt,
            "fusedAlt": self.fusedAlt,
            "setUtc": datetime.now(timezone.utc).isoformat(),
        }
        self.apply_altitude_zero(zero)
        self.gpsRelAlt = self._relative_altitude("gpsAlt", self.gpsAlt)
        self.baroRelAlt = self._relative_altitude("baroAlt", self.baroAlt)
        self.imuRelAlt = self._relative_altitude("imuAlt", self.imuAlt)
        self.fusedRelAlt = self._relative_altitude("fusedAlt", self.fusedAlt)
        self.kalmanRelAlt = self._relative_altitude("fusedAlt", self.kalmanAlt)
        self.verticalVelocity = 0.0
        self.kalmanVelocity = 0.0
        self.imuVelocity = 0.0
        self.history = []
        return zero

    def clear_altitude_zero(self) -> None:
        self.altitudeZero = {}
        self.gpsRelAlt = self.gpsAlt
        self.baroRelAlt = self.baroAlt
        self.imuRelAlt = self.imuAlt
        self.fusedRelAlt = self.fusedAlt
        self.kalmanRelAlt = self.kalmanAlt
        self.verticalVelocity = 0.0
        self.kalmanVelocity = 0.0
        self.imuVelocity = 0.0
        self.history = []

    def _relative_altitude(self, key: str, value: Optional[float]) -> Optional[float]:
        if value is None:
            return None
        zero = _maybe_float(self.altitudeZero.get(key)) if self.altitudeZero else None
        return value - zero if zero is not None else value

    def _accept_baro_altitude(self, value: float, max_step_m: float) -> float:
        if self._accepted_baro_alt is None or max_step_m <= 0:
            self._accepted_baro_alt = value
            return value
        if abs(value - self._accepted_baro_alt) <= max_step_m:
            self._accepted_baro_alt = value
            return value
        self._last_rejection = f"baro jump {value - self._accepted_baro_alt:.1f} m"
        return self._accepted_baro_alt

    def _accept_gps(self, lat: float, lon: float, alt: float, gps_status_code: int, sats_used: int, max_step_m: float, max_alt_step_m: float) -> bool:
        if gps_status_code < 2 or sats_used < 4 or abs(lat) < 0.000001 or abs(lon) < 0.000001:
            self._last_gps_rejection = "not enough GPS quality"
            return False
        if self._accepted_lat is not None and self._accepted_lon is not None and max_step_m > 0:
            if haversine_m(self._accepted_lat, self._accepted_lon, lat, lon) > max_step_m:
                self._last_gps_rejection = "gps position jump"
                return False
        if self._accepted_gps_alt is not None and max_alt_step_m > 0:
            if abs(alt - self._accepted_gps_alt) > max_alt_step_m:
                self._last_gps_rejection = f"gps altitude jump {alt - self._accepted_gps_alt:.1f} m"
                return False
        self._accepted_lat = lat
        self._accepted_lon = lon
        self._accepted_gps_alt = alt
        self._last_gps_rejection = None
        return True

    def _reset_vertical_kalman(self, altitude_m: float, acceleration_mps2: float, baro_variance: float, accel_variance: float) -> None:
        """Seed the vertical estimator from the latest trusted measurements."""
        self._kalman_x = [altitude_m, 0.0, acceleration_mps2]
        self._kalman_p = [
            [max(0.001, baro_variance), 0.0, 0.0],
            [0.0, 4.0, 0.0],
            [0.0, 0.0, max(0.001, accel_variance)],
        ]

    def _kalman_update_scalar(self, measurement: float, h: List[float], variance: float) -> None:
        """Apply one scalar Kalman measurement update.

        The state vector is [altitude, velocity, acceleration]. The h vector
        selects which state component this measurement observes.
        """
        if self._kalman_x is None or self._kalman_p is None:
            return
        p = self._kalman_p
        ph = [sum(p[row][col] * h[col] for col in range(3)) for row in range(3)]
        innovation_variance = sum(h[row] * ph[row] for row in range(3)) + max(0.001, variance)
        if innovation_variance <= 0:
            return
        innovation = measurement - sum(h[row] * self._kalman_x[row] for row in range(3))
        gain = [value / innovation_variance for value in ph]
        self._kalman_x = [self._kalman_x[row] + gain[row] * innovation for row in range(3)]
        hp = [sum(h[row] * p[row][col] for row in range(3)) for col in range(3)]
        next_p = [
            [p[row][col] - gain[row] * hp[col] for col in range(3)]
            for row in range(3)
        ]
        self._kalman_p = [
            [(next_p[row][col] + next_p[col][row]) * 0.5 for col in range(3)]
            for row in range(3)
        ]

    def _run_vertical_kalman(
        self,
        altitude_m: float,
        acceleration_mps2: float,
        dt: Optional[float],
        baro_variance: float,
        accel_variance: float,
        process_altitude: float,
        process_velocity: float,
        process_accel: float,
    ) -> None:
        """Fuse baro altitude and gravity-corrected acceleration.

        GPS altitude intentionally stays out of this estimator. It remains a
        separate positioning signal until the GPS behavior is flight-proven.
        """
        if self._kalman_x is None or self._kalman_p is None or dt is None or dt > 5.0:
            self._reset_vertical_kalman(altitude_m, acceleration_mps2, baro_variance, accel_variance)
        else:
            dt = max(0.001, min(2.5, dt))
            x = self._kalman_x
            p = self._kalman_p
            transition = [
                [1.0, dt, 0.5 * dt * dt],
                [0.0, 1.0, dt],
                [0.0, 0.0, 1.0],
            ]
            predicted_x = [
                x[0] + x[1] * dt + 0.5 * x[2] * dt * dt,
                x[1] + x[2] * dt,
                x[2],
            ]
            fp = [
                [sum(transition[row][k] * p[k][col] for k in range(3)) for col in range(3)]
                for row in range(3)
            ]
            predicted_p = [
                [sum(fp[row][k] * transition[col][k] for k in range(3)) for col in range(3)]
                for row in range(3)
            ]
            predicted_p[0][0] += max(0.0, process_altitude) * dt
            predicted_p[1][1] += max(0.0, process_velocity) * dt
            predicted_p[2][2] += max(0.0, process_accel) * dt
            self._kalman_x = predicted_x
            self._kalman_p = predicted_p

        self._kalman_update_scalar(acceleration_mps2, [0.0, 0.0, 1.0], accel_variance)
        self._kalman_update_scalar(altitude_m, [1.0, 0.0, 0.0], baro_variance)
        if self._kalman_x is None:
            return
        self.kalmanAlt = self._kalman_x[0]
        self.kalmanVelocity = self._kalman_x[1]
        self.kalmanAccel = self._kalman_x[2]

    def _update_readiness(self, now: float) -> None:
        gps_ready = self.gpsStatus == "3D fix" and (self.satsUsed or 0) >= 4
        link_ready = self.rssi is not None and self.snr is not None and self.snr > -5
        zero_ready = bool(self.altitudeZero.get("setUtc")) if self.altitudeZero else False
        velocity_ready = self.verticalVelocity is not None and abs(self.verticalVelocity) < 1.5
        accel_ready = self.linearAccel is not None and abs(self.linearAccel) < 1.0
        self.readiness = {
            "readyForDroneTest": bool(self.last_update_mono > 0 and gps_ready and link_ready and zero_ready),
            "gps3d": gps_ready,
            "link": link_ready,
            "altitudeZero": zero_ready,
            "stationary": bool(velocity_ready and accel_ready),
            "lastSeenOk": self.last_update_mono > 0 and (now - self.last_update_mono) <= OFFLINE_TIMEOUT_S,
        }

    def _update_packet_rate(self, now: float) -> None:
        self.packetsReceived += 1
        self._packet_times.append(now)
        window_start = now - 30.0
        self._packet_times = [stamp for stamp in self._packet_times if stamp >= window_start]
        if len(self._packet_times) >= 2:
            span = max(0.001, self._packet_times[-1] - self._packet_times[0])
            self.packetRateHz = (len(self._packet_times) - 1) / span
        else:
            self.packetRateHz = 0.0

    def _update_warnings(self, now: float) -> None:
        warnings: List[Dict[str, str]] = []
        if self.last_update_mono <= 0:
            warnings.append({"severity": "warning", "code": "NO_TELEMETRY", "message": "No telemetry received"})
        elif (now - self.last_update_mono) > OFFLINE_TIMEOUT_S:
            warnings.append({"severity": "critical", "code": "STALE_TELEMETRY", "message": "Telemetry is stale"})
        if self.gpsStatus != "3D fix":
            in_view = self.satsInView if self.satsInView is not None else self.sats
            used = self.satsUsed if self.satsUsed is not None else 0
            if in_view and in_view >= 4:
                warnings.append({
                    "severity": "warning",
                    "code": "GPS_NO_FIX",
                    "message": f"GPS sees {in_view} sats but is using {used}",
                })
            else:
                warnings.append({"severity": "info", "code": "GPS_WAITING", "message": "GPS has no 3D fix"})
        if self.gpsQuality and self.gpsQuality not in ("accepted", "not used"):
            warnings.append({"severity": "info", "code": "GPS_REJECTED", "message": self.gpsQuality})
        if self._last_rejection:
            warnings.append({"severity": "warning", "code": "BARO_REJECTED", "message": self._last_rejection})
        if not (self.altitudeZero and self.altitudeZero.get("setUtc")):
            warnings.append({"severity": "warning", "code": "ALTITUDE_ZERO_MISSING", "message": "Altitude zero is not set"})
        if self.snr is not None and self.snr <= -5:
            warnings.append({"severity": "critical", "code": "LORA_SNR_LOW", "message": f"SNR is {self.snr:.1f} dB"})
        elif self.snr is not None and self.snr < 2:
            warnings.append({"severity": "warning", "code": "LORA_SNR_WEAK", "message": f"SNR is {self.snr:.1f} dB"})
        gyro = [value for value in (self.gx, self.gy, self.gz) if value is not None]
        if gyro and max(abs(value) for value in gyro) > 1.0 and self.readiness.get("stationary"):
            warnings.append({"severity": "warning", "code": "GYRO_BIAS", "message": "Stationary gyro bias exceeds 1 deg/s"})
        self.warnings = warnings

    def update_from_payload(self, decoded: Dict[str, object], rx_meta: Dict[str, object], now: float) -> None:
        """Update one stage from a decoded telemetry packet and RX metadata."""
        self._update_packet_rate(now)
        previous_fused_alt = self.fusedRelAlt
        previous_update_mono = self.last_update_mono
        smoothing_alpha = max(0.0, min(1.0, sensor_float("velocitySmoothingAlpha", VELOCITY_SMOOTHING_ALPHA)))
        gravity_reference = sensor_float("gravityMps2", GRAVITY_REFERENCE)
        linear_accel_deadband = sensor_float("linearAccelDeadbandMps2", LINEAR_ACCEL_DEADBAND)
        altitude_noise_deadband = max(0.0, sensor_float("altitudeNoiseDeadbandM", ALTITUDE_NOISE_DEADBAND_M))
        stationary_velocity_deadband = max(0.0, sensor_float("stationaryVelocityDeadbandMps", STATIONARY_VELOCITY_DEADBAND_MPS))
        max_baro_step_m = max(0.0, sensor_float("maxBaroStepM", MAX_BARO_STEP_M))
        max_gps_step_m = max(0.0, sensor_float("maxGpsStepM", MAX_GPS_STEP_M))
        max_gps_alt_step_m = max(0.0, sensor_float("maxGpsAltStepM", MAX_GPS_ALT_STEP_M))
        kalman_baro_variance = max(0.001, sensor_float("kalmanBaroVarianceM2", KALMAN_BARO_VARIANCE_M2))
        kalman_accel_variance = max(0.001, sensor_float("kalmanAccelVarianceMps2", KALMAN_ACCEL_VARIANCE_MPS2))
        kalman_process_altitude = max(0.0, sensor_float("kalmanProcessAltitude", KALMAN_PROCESS_ALTITUDE))
        kalman_process_velocity = max(0.0, sensor_float("kalmanProcessVelocity", KALMAN_PROCESS_VELOCITY))
        kalman_process_accel = max(0.0, sensor_float("kalmanProcessAccel", KALMAN_PROCESS_ACCEL))
        self.seq = int(decoded["seq"])
        self._last_rejection = None
        self._last_gps_rejection = None
        raw_lat = float(decoded["lat"])
        raw_lon = float(decoded["lon"])
        raw_gps_alt = float(decoded["gps_alt_m"])
        raw_baro_alt = float(decoded["baro_alt_m"])
        self.gpsAlt = raw_gps_alt
        self.baroAlt = self._accept_baro_altitude(raw_baro_alt, max_baro_step_m)
        self.imuAlt = float(decoded["imu_alt_m"])
        gps_status_code = int(decoded["gps_status"])
        self.sats = int(decoded.get("sats", 0))
        self.satsUsed = int(decoded.get("sats_used", self.sats if gps_status_code > 0 and self.sats <= 15 else 0))
        self.satsInView = int(decoded.get("sats_in_view", self.sats))
        # Accept GPS only after quality checks. Bad fixes still remain visible
        # in GPS-specific fields, but they do not move the displayed position.
        gps_accepted = self._accept_gps(raw_lat, raw_lon, raw_gps_alt, gps_status_code, self.satsUsed or 0, max_gps_step_m, max_gps_alt_step_m)
        self.gpsQuality = "accepted" if gps_accepted else (self._last_gps_rejection or "not used")
        self.lat = self._accepted_lat if self._accepted_lat is not None else raw_lat
        self.lon = self._accepted_lon if self._accepted_lon is not None else raw_lon
        if gps_status_code == 0:
            self.gpsStatus = "No fix"
        elif gps_status_code == 1:
            self.gpsStatus = "GPS fix"
        elif gps_status_code == 2:
            self.gpsStatus = "3D fix"
        else:
            self.gpsStatus = f"Unknown ({gps_status_code})"
        self.ax = float(decoded["ax"])
        self.ay = float(decoded["ay"])
        self.az = float(decoded["az"])
        self.accelMagnitude = math.sqrt(self.ax * self.ax + self.ay * self.ay + self.az * self.az)
        # Current vertical acceleration is a practical approximation:
        # magnitude minus gravity, not full attitude-resolved acceleration.
        self.rawLinearAccel = self.accelMagnitude - gravity_reference
        accel_measurement = self.rawLinearAccel
        if abs(accel_measurement) < linear_accel_deadband:
            accel_measurement = 0.0
        dt = now - previous_update_mono if previous_update_mono > 0 and now > previous_update_mono else None
        self._run_vertical_kalman(
            self.baroAlt,
            accel_measurement,
            dt,
            kalman_baro_variance,
            kalman_accel_variance,
            kalman_process_altitude,
            kalman_process_velocity,
            kalman_process_accel,
        )
        # GPS stays out of the vertical estimator. The fused altitude is now the
        # baro-plus-IMU Kalman altitude; GPS altitude remains a separate display.
        self.fusedAlt = self.kalmanAlt if self.kalmanAlt is not None else self.baroAlt
        self.gpsRelAlt = self._relative_altitude("gpsAlt", self.gpsAlt)
        self.baroRelAlt = self._relative_altitude("baroAlt", self.baroAlt)
        self.imuRelAlt = self._relative_altitude("imuAlt", self.imuAlt)
        self.fusedRelAlt = self._relative_altitude("fusedAlt", self.fusedAlt)
        self.kalmanRelAlt = self.fusedRelAlt
        self.verticalVelocity = self.kalmanVelocity
        self.linearAccel = self.kalmanAccel
        if self.linearAccel is not None and abs(self.linearAccel) < linear_accel_deadband:
            self.linearAccel = 0.0
            self.kalmanAccel = 0.0
        stationary_candidate = False
        if previous_fused_alt is not None and self.fusedRelAlt is not None and dt is not None:
            alt_delta = self.fusedRelAlt - previous_fused_alt
            stationary_candidate = accel_measurement == 0.0 and abs(alt_delta) < altitude_noise_deadband
        if stationary_candidate and self.verticalVelocity is not None and abs(self.verticalVelocity) < stationary_velocity_deadband:
            self.verticalVelocity = 0.0
            self.kalmanVelocity = 0.0
            if self._kalman_x is not None:
                self._kalman_x[1] = 0.0
                if self.linearAccel == 0.0:
                    self._kalman_x[2] = 0.0
        if previous_update_mono > 0 and now > previous_update_mono:
            dt = now - previous_update_mono
            previous_imu_velocity = self.imuVelocity or 0.0
            raw_imu_velocity = previous_imu_velocity + accel_measurement * dt
            if accel_measurement == 0.0:
                raw_imu_velocity *= 0.35
            self.imuVelocity = previous_imu_velocity + (raw_imu_velocity - previous_imu_velocity) * smoothing_alpha
            if accel_measurement == 0.0 and abs(self.imuVelocity) < 0.08:
                self.imuVelocity = 0.0
        else:
            self.imuVelocity = None
        self.gx = float(decoded["gx"])
        self.gy = float(decoded["gy"])
        self.gz = float(decoded["gz"])
        self.rssi = _maybe_float(rx_meta.get("rssi"))
        self.snr = _maybe_float(rx_meta.get("lsnr"))
        self.frequencyMHz = _maybe_float(rx_meta.get("freq"))
        self.dataRate = str(rx_meta.get("datr")) if rx_meta.get("datr") is not None else None

        mask = int(decoded["event_flags"])
        current_bit = current_event_bit(mask)
        current_mask = (1 << current_bit) if current_bit is not None else 0
        self.currentEvent = EVENT_FLAG_NAMES[current_bit] if current_bit is not None else None
        self.eventFlags = {EVENT_FLAG_NAMES[b]: bool(current_mask & (1 << b)) for b in EVENT_FLAG_NAMES}
        self.events = [self.currentEvent] if self.currentEvent else []
        self.prev_event_mask = current_mask

        label = datetime.now(timezone.utc).strftime("%H:%M:%S")
        self.history.append({
            "t": label,
            "gpsAlt": self.gpsAlt,
            "baroAlt": self.baroAlt,
            "imuAlt": self.imuAlt,
            "fusedAlt": self.fusedAlt,
            "gpsRelAlt": self.gpsRelAlt,
            "baroRelAlt": self.baroRelAlt,
            "imuRelAlt": self.imuRelAlt,
            "fusedRelAlt": self.fusedRelAlt,
            "verticalVelocity": self.verticalVelocity,
            "imuVelocity": self.imuVelocity,
            "kalmanAlt": self.kalmanAlt,
            "kalmanRelAlt": self.kalmanRelAlt,
            "kalmanVelocity": self.kalmanVelocity,
            "kalmanAccel": self.kalmanAccel,
            "accelMagnitude": self.accelMagnitude,
            "linearAccel": self.linearAccel,
            "rawLinearAccel": self.rawLinearAccel,
            "gpsQuality": self.gpsQuality,
            "ax": self.ax,
            "ay": self.ay,
            "az": self.az,
            "gx": self.gx,
            "gy": self.gy,
            "gz": self.gz,
        })
        if len(self.history) > HISTORY_LENGTH:
            self.history = self.history[-HISTORY_LENGTH:]

        self.last_update_mono = now
        self.last_update_utc = datetime.now(timezone.utc).isoformat()
        self._update_readiness(now)
        self._update_warnings(now)

    def to_dict(self, now: float) -> Dict[str, object]:
        last_seen_ms: Optional[int]
        if self.last_update_mono > 0:
            last_seen_ms = int((now - self.last_update_mono) * 1000)
        else:
            last_seen_ms = None
        self._update_readiness(now)
        self._update_warnings(now)
        return {
            "id": self.id,
            "name": self.name,
            "deviceId": self.deviceId,
            "seq": self.seq,
            "lat": self.lat,
            "lon": self.lon,
            "gpsAlt": self.gpsAlt,
            "baroAlt": self.baroAlt,
            "imuAlt": self.imuAlt,
            "fusedAlt": self.fusedAlt,
            "gpsRelAlt": self.gpsRelAlt,
            "baroRelAlt": self.baroRelAlt,
            "imuRelAlt": self.imuRelAlt,
            "fusedRelAlt": self.fusedRelAlt,
            "verticalVelocity": self.verticalVelocity,
            "imuVelocity": self.imuVelocity,
            "kalmanAlt": self.kalmanAlt,
            "kalmanRelAlt": self.kalmanRelAlt,
            "kalmanVelocity": self.kalmanVelocity,
            "kalmanAccel": self.kalmanAccel,
            "accelMagnitude": self.accelMagnitude,
            "linearAccel": self.linearAccel,
            "rawLinearAccel": self.rawLinearAccel,
            "gpsStatus": self.gpsStatus,
            "sats": self.sats,
            "satsUsed": self.satsUsed,
            "satsInView": self.satsInView,
            "gpsQuality": self.gpsQuality,
            "ax": self.ax,
            "ay": self.ay,
            "az": self.az,
            "gx": self.gx,
            "gy": self.gy,
            "gz": self.gz,
            "rssi": self.rssi,
            "snr": self.snr,
            "frequencyMHz": self.frequencyMHz,
            "dataRate": self.dataRate,
            "currentEvent": self.currentEvent,
            "eventFlags": self.eventFlags,
            "events": self.events,
            "altitudeZero": self.altitudeZero,
            "readiness": self.readiness,
            "warnings": self.warnings,
            "history": self.history,
            "packetsReceived": self.packetsReceived,
            "packetRateHz": self.packetRateHz,
            "lastSeenMs": last_seen_ms,
            "lastUpdateUtc": self.last_update_utc,
        }


def _maybe_float(value: object) -> Optional[float]:
    try:
        if value is None:
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _nmea_coord(value: str, hemisphere: str) -> Optional[float]:
    if not value or not hemisphere:
        return None
    try:
        dot = value.index(".")
        degree_digits = dot - 2
        degrees = float(value[:degree_digits])
        minutes = float(value[degree_digits:])
        coord = degrees + minutes / 60.0
        if hemisphere in ("S", "W"):
            coord = -coord
        return coord
    except (ValueError, IndexError):
        return None


def haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    radius_m = 6371000.0
    p1 = math.radians(lat1)
    p2 = math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2.0) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2.0) ** 2
    return 2.0 * radius_m * math.asin(math.sqrt(a))


def _parse_nmea_datetime(time_field: str, date_field: str) -> Optional[datetime]:
    if not time_field or not date_field or len(date_field) != 6:
        return None
    try:
        hour = int(time_field[0:2])
        minute = int(time_field[2:4])
        second = int(float(time_field[4:]))
        day = int(date_field[0:2])
        month = int(date_field[2:4])
        year_2 = int(date_field[4:6])
        year = 2000 + year_2 if year_2 < 80 else 1900 + year_2
        stamp = datetime(year, month, day, hour, minute, second, tzinfo=timezone.utc)
    except (TypeError, ValueError, IndexError):
        return None
    if not (2024 <= stamp.year <= 2035):
        return None
    return stamp


def _parse_nmea_zda(parts: List[str]) -> Optional[datetime]:
    if len(parts) < 5 or not parts[1] or not parts[2] or not parts[3] or not parts[4]:
        return None
    try:
        hour = int(parts[1][0:2])
        minute = int(parts[1][2:4])
        second = int(float(parts[1][4:]))
        day = int(parts[2])
        month = int(parts[3])
        year = int(parts[4])
        stamp = datetime(year, month, day, hour, minute, second, tzinfo=timezone.utc)
    except (TypeError, ValueError, IndexError):
        return None
    if not (2024 <= stamp.year <= 2035):
        return None
    return stamp


def set_system_time_from_epoch(epoch: float) -> Dict[str, object]:
    if epoch < 1700000000 or epoch > 2200000000:
        return {"ok": False, "error": "epoch outside expected range"}
    last_error = "date command unavailable"
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


class GroundGpsReader:
    def __init__(self, port: str, baud: int = 9600) -> None:
        self.port = port
        self.baud = baud
        self._lock = threading.Lock()
        self._latest: Dict[str, object] = {
            "gpsStatus": "No fix",
            "sats": None,
            "lat": None,
            "lon": None,
            "altitudeM": None,
            "lastGpsUtc": None,
            "gpsTimeUtc": None,
            "gpsTimeEpoch": None,
            "gpsTimeMono": None,
            "gpsTimeSource": None,
        }
        self._thread = threading.Thread(target=self._read_loop, daemon=True)
        self._thread.start()

    def snapshot(self) -> Dict[str, object]:
        with self._lock:
            return dict(self._latest)

    def _configure_port(self, fd: int) -> None:
        if termios is None:
            return
        baud_const = getattr(termios, f"B{self.baud}", termios.B9600)
        attrs = termios.tcgetattr(fd)
        attrs[0] = termios.IGNPAR
        attrs[1] = 0
        attrs[2] = termios.CS8 | termios.CREAD | termios.CLOCAL
        attrs[3] = 0
        attrs[4] = baud_const
        attrs[5] = baud_const
        attrs[6][termios.VMIN] = 1
        attrs[6][termios.VTIME] = 0
        termios.tcsetattr(fd, termios.TCSANOW, attrs)

    def _read_loop(self) -> None:
        while True:
            try:
                fd = os.open(self.port, os.O_RDONLY | os.O_NOCTTY)
                self._configure_port(fd)
                with os.fdopen(fd, "rb", buffering=0) as stream:
                    buffer = b""
                    while True:
                        chunk = stream.read(1)
                        if not chunk:
                            time.sleep(0.05)
                            continue
                        if chunk in (b"\n", b"\r"):
                            if buffer:
                                self._parse_line(buffer.decode("ascii", errors="ignore").strip())
                                buffer = b""
                        elif len(buffer) < 120:
                            buffer += chunk
                        else:
                            buffer = b""
            except Exception as exc:
                with self._lock:
                    self._latest["gpsStatus"] = f"GPS serial unavailable: {exc}"
                time.sleep(2.0)

    def _parse_line(self, line: str) -> None:
        start = line.find("$")
        if start < 0:
            return
        line = line[start:]
        parts = line.split(",")
        sentence = parts[0][-3:]
        if sentence == "RMC" and len(parts) >= 10:
            stamp = _parse_nmea_datetime(parts[1], parts[9])
            if stamp is not None:
                with self._lock:
                    self._latest.update({
                        "gpsTimeUtc": stamp.isoformat(),
                        "gpsTimeEpoch": stamp.timestamp(),
                        "gpsTimeMono": time.monotonic(),
                        "gpsTimeSource": "RMC",
                    })
            lat = _nmea_coord(parts[3], parts[4]) if len(parts) > 5 else None
            lon = _nmea_coord(parts[5], parts[6]) if len(parts) > 6 else None
            if parts[2] == "A" and lat is not None and lon is not None:
                with self._lock:
                    self._latest.update({
                        "gpsStatus": "Fix",
                        "lat": lat,
                        "lon": lon,
                        "lastGpsUtc": datetime.now(timezone.utc).isoformat(),
                    })
            return
        if sentence == "ZDA":
            stamp = _parse_nmea_zda(parts)
            if stamp is not None:
                with self._lock:
                    self._latest.update({
                        "gpsTimeUtc": stamp.isoformat(),
                        "gpsTimeEpoch": stamp.timestamp(),
                        "gpsTimeMono": time.monotonic(),
                        "gpsTimeSource": "ZDA",
                    })
            return
        if sentence == "GGA" and len(parts) >= 10:
            lat = _nmea_coord(parts[2], parts[3])
            lon = _nmea_coord(parts[4], parts[5])
            try:
                quality = int(parts[6] or "0")
            except ValueError:
                quality = 0
            try:
                sats = int(parts[7] or "0")
            except ValueError:
                sats = None
            altitude = _maybe_float(parts[9])
            if quality > 0 and lat is not None and lon is not None:
                with self._lock:
                    self._latest.update({
                        "gpsStatus": "Fix",
                        "sats": sats,
                        "lat": lat,
                        "lon": lon,
                        "altitudeM": altitude,
                        "lastGpsUtc": datetime.now(timezone.utc).isoformat(),
                    })
            else:
                with self._lock:
                    self._latest.update({
                        "gpsStatus": "No fix",
                        "sats": sats,
                        "lastGpsUtc": datetime.now(timezone.utc).isoformat(),
                    })


def decode_payload(payload: bytes) -> Optional[Dict[str, object]]:
    # Adafruit RFM9x sends a 4-byte RadioHead-compatible header by default:
    # destination, source, identifier, flags. The UHRK telemetry payload follows.
    if len(payload) == PACKET_LEN + 4:
        payload = payload[4:]
    if len(payload) != PACKET_LEN:
        return None
    try:
        fields = struct.unpack(PACKET_FORMAT, payload)
    except struct.error:
        return None
    (
        device_id,
        seq,
        lat_i,
        lon_i,
        gps_i,
        baro_i,
        imu_i,
        gps_status,
        sats_encoded,
        ax_i,
        ay_i,
        az_i,
        gx_i,
        gy_i,
        gz_i,
        event_flags,
    ) = fields
    if sats_encoded <= 15:
        sats_used = sats_encoded if gps_status > 0 else 0
        sats_in_view = sats_encoded
    else:
        sats_used = sats_encoded & 0x0F
        sats_in_view = (sats_encoded >> 4) & 0x0F
    return {
        "device_id": device_id,
        "seq": seq,
        "lat": lat_i / 1e7,
        "lon": lon_i / 1e7,
        "gps_alt_m": gps_i / 100.0,
        "baro_alt_m": baro_i / 100.0,
        "imu_alt_m": imu_i / 100.0,
        "gps_status": gps_status,
        "sats": sats_encoded,
        "sats_used": sats_used,
        "sats_in_view": sats_in_view,
        "ax": ax_i / 10.0,
        "ay": ay_i / 10.0,
        "az": az_i / 10.0,
        "gx": gx_i / 10.0,
        "gy": gy_i / 10.0,
        "gz": gz_i / 10.0,
        "event_flags": event_flags,
    }


def ack(data: bytes, ack_type: int) -> bytes:
    return data[:3] + bytes([ack_type])


def gateway_id_from_push(data: bytes) -> Optional[str]:
    if len(data) < 12:
        return None
    return data[4:12].hex().upper()


def system_warnings(stages: Dict[int, StageState], ground_station: Dict[str, object], now: float) -> List[Dict[str, str]]:
    warnings: List[Dict[str, str]] = []
    active_count = sum(1 for stage in stages.values() if stage.last_update_mono > 0 and (now - stage.last_update_mono) <= OFFLINE_TIMEOUT_S)
    if active_count == 0:
        warnings.append({"severity": "critical", "code": "NO_NODES", "message": "No active node telemetry"})
    elif active_count < len(stages):
        warnings.append({"severity": "warning", "code": "NODES_MISSING", "message": f"{active_count}/{len(stages)} nodes are active"})
    if ground_station.get("gpsStatus") != "Fix":
        warnings.append({"severity": "warning", "code": "GC_GPS_NO_FIX", "message": "Ground station GPS has no fix"})
    last_gps = ground_station.get("lastGpsUtc")
    if not last_gps:
        warnings.append({"severity": "info", "code": "GC_GPS_WAITING", "message": "No ground GPS sentence received yet"})
    try:
        usage = shutil.disk_usage(LOG_DIR)
        free_mb = usage.free / (1024 * 1024)
        if free_mb < 250:
            warnings.append({"severity": "critical", "code": "LOG_STORAGE_LOW", "message": f"Only {free_mb:.0f} MB free for logs"})
        elif free_mb < 1000:
            warnings.append({"severity": "warning", "code": "LOG_STORAGE_WARN", "message": f"{free_mb:.0f} MB free for logs"})
    except OSError:
        warnings.append({"severity": "warning", "code": "LOG_STORAGE_UNKNOWN", "message": "Could not check log storage"})
    return warnings


def telemetry_snapshot(stages: Dict[int, StageState], ground_station: Dict[str, object], now: float) -> Dict[str, object]:
    stage_snapshots = [stages[dev_id].to_dict(now) for dev_id in sorted(stages)]
    return {
        "ground_station": ground_station,
        "stages": stage_snapshots,
        "pad_compare": {},
        "system": {
            "version": APP_VERSION,
            "backendUtc": datetime.now(timezone.utc).isoformat(),
            "outputFile": str(OUTPUT_FILE),
        },
        "warnings": system_warnings(stages, ground_station, now),
        "updatedUtc": datetime.now(timezone.utc).isoformat(),
    }


def write_json(stages: Dict[int, StageState], ground_station: Dict[str, object], now: float) -> None:
    output = telemetry_snapshot(stages, ground_station, now)
    tmp_path = OUTPUT_FILE.with_suffix(".tmp")
    with tmp_path.open("w", encoding="utf-8") as f:
        json.dump(output, f, separators=(",", ":"))
    os.replace(tmp_path, OUTPUT_FILE)


def _service_state(name: str) -> str:
    try:
        result = subprocess.run(["systemctl", "is-active", name], check=False, capture_output=True, text=True, timeout=2)
    except (OSError, subprocess.SubprocessError):
        return "unknown"
    return (result.stdout.strip() or result.stderr.strip() or f"exit {result.returncode}")[:80]


def _file_info(path: Path) -> Dict[str, object]:
    try:
        stat = path.stat()
    except OSError:
        return {"path": str(path), "exists": False}
    return {
        "path": str(path),
        "exists": True,
        "sizeBytes": stat.st_size,
        "modifiedUtc": datetime.fromtimestamp(stat.st_mtime, timezone.utc).isoformat(),
    }


def _storage_info(path: Path) -> Dict[str, object]:
    try:
        usage = shutil.disk_usage(path)
    except OSError as exc:
        return {"path": str(path), "ok": False, "error": str(exc)}
    return {
        "path": str(path),
        "ok": True,
        "totalBytes": usage.total,
        "usedBytes": usage.used,
        "freeBytes": usage.free,
    }


def health_snapshot(
    stages: Dict[int, StageState],
    ground_station: Dict[str, object],
    flight_log: FlightLogger,
    downlink: LoRaDownlink,
    started_mono: float,
) -> Dict[str, object]:
    now = time.monotonic()
    stage_snapshots = [stages[dev_id].to_dict(now) for dev_id in sorted(stages)]
    return {
        "ok": True,
        "version": APP_VERSION,
        "backendUtc": datetime.now(timezone.utc).isoformat(),
        "uptimeS": round(now - started_mono, 1),
        "services": {
            "uhrk-backend": _service_state("uhrk-backend"),
            "uhrk-web": _service_state("uhrk-web"),
            "lora-pkt-fwd": _service_state("lora-pkt-fwd"),
        },
        "files": {
            "telemetry": _file_info(OUTPUT_FILE),
            "gcLog": _file_info(flight_log.path),
            "settings": _file_info(SETTINGS_FILE),
            "altitudeZero": _file_info(ALTITUDE_ZERO_FILE),
        },
        "storage": _storage_info(LOG_DIR),
        "downlink": downlink.snapshot(),
        "groundStation": ground_station,
        "stages": stage_snapshots,
        "warnings": system_warnings(stages, ground_station, now),
    }


def log_list_snapshot(flight_log: FlightLogger) -> Dict[str, object]:
    logs: List[Dict[str, object]] = []
    try:
        candidates = sorted(LOG_DIR.glob("*.jsonl"), key=lambda item: item.stat().st_mtime, reverse=True)
    except OSError:
        candidates = []
    for path in candidates[:50]:
        logs.append(_file_info(path))
    return {"ok": True, "current": str(flight_log.path), "logs": logs}


def _iter_log_records(path: Path) -> List[Dict[str, object]]:
    records: List[Dict[str, object]] = []
    try:
        with path.open("r", encoding="utf-8") as f:
            for line in f:
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(record, dict):
                    records.append(record)
    except OSError:
        pass
    return records


def _valid_coord(lat: object, lon: object) -> bool:
    lat_f = _maybe_float(lat)
    lon_f = _maybe_float(lon)
    if lat_f is None or lon_f is None:
        return False
    if not (-90.0 <= lat_f <= 90.0 and -180.0 <= lon_f <= 180.0):
        return False
    return not (abs(lat_f) < 0.000001 and abs(lon_f) < 0.000001)


def export_csv_from_log(path: Path) -> bytes:
    output = io.StringIO()
    writer = csv.DictWriter(output, fieldnames=[
        "source",
        "stage",
        "deviceId",
        "loggedUtc",
        "seq",
        "lat",
        "lon",
        "altitudeM",
        "gpsStatus",
        "satsUsed",
        "satsInView",
        "rssi",
        "snr",
        "baroAltM",
        "imuAltM",
        "eventFlags",
    ])
    writer.writeheader()
    for record in _iter_log_records(path):
        logged_utc = record.get("loggedUtc")
        data = record.get("data") if isinstance(record.get("data"), dict) else {}
        if record.get("type") == "packet":
            decoded = data.get("decoded") if isinstance(data.get("decoded"), dict) else {}
            rx_meta = data.get("rxMeta") if isinstance(data.get("rxMeta"), dict) else {}
            writer.writerow({
                "source": "node",
                "stage": data.get("stage"),
                "deviceId": data.get("deviceId"),
                "loggedUtc": logged_utc,
                "seq": decoded.get("seq"),
                "lat": decoded.get("lat"),
                "lon": decoded.get("lon"),
                "altitudeM": decoded.get("gps_alt_m"),
                "gpsStatus": decoded.get("gps_status"),
                "satsUsed": decoded.get("sats_used"),
                "satsInView": decoded.get("sats_in_view"),
                "rssi": rx_meta.get("rssi"),
                "snr": rx_meta.get("lsnr"),
                "baroAltM": decoded.get("baro_alt_m"),
                "imuAltM": decoded.get("imu_alt_m"),
                "eventFlags": decoded.get("event_flags"),
            })
        elif record.get("type") == "ground_snapshot":
            ground = data.get("groundStation") if isinstance(data.get("groundStation"), dict) else {}
            writer.writerow({
                "source": "ground_station",
                "stage": "Ground Station",
                "deviceId": "",
                "loggedUtc": logged_utc,
                "seq": "",
                "lat": ground.get("lat"),
                "lon": ground.get("lon"),
                "altitudeM": ground.get("altitudeM"),
                "gpsStatus": ground.get("gpsStatus"),
                "satsUsed": ground.get("sats"),
                "satsInView": ground.get("sats"),
                "rssi": "",
                "snr": "",
                "baroAltM": "",
                "imuAltM": "",
                "eventFlags": "",
            })
    return output.getvalue().encode("utf-8")


def export_kml_from_log(path: Path) -> bytes:
    tracks: Dict[str, List[tuple[float, float, float, str]]] = {}
    for record in _iter_log_records(path):
        logged_utc = str(record.get("loggedUtc") or "")
        data = record.get("data") if isinstance(record.get("data"), dict) else {}
        if record.get("type") == "packet":
            decoded = data.get("decoded") if isinstance(data.get("decoded"), dict) else {}
            if not _valid_coord(decoded.get("lat"), decoded.get("lon")):
                continue
            stage = str(data.get("stage") or f"Node {data.get('deviceId', '')}").strip()
            lat = float(decoded.get("lat"))
            lon = float(decoded.get("lon"))
            alt = float(_maybe_float(decoded.get("gps_alt_m")) or 0.0)
            tracks.setdefault(stage, []).append((lon, lat, alt, logged_utc))
        elif record.get("type") == "ground_snapshot":
            ground = data.get("groundStation") if isinstance(data.get("groundStation"), dict) else {}
            if not _valid_coord(ground.get("lat"), ground.get("lon")):
                continue
            lat = float(ground.get("lat"))
            lon = float(ground.get("lon"))
            alt = float(_maybe_float(ground.get("altitudeM")) or 0.0)
            tracks.setdefault("Ground Station", []).append((lon, lat, alt, logged_utc))

    parts = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<kml xmlns="http://www.opengis.net/kml/2.2">',
        "<Document>",
        "<name>UHRK telemetry tracks</name>",
    ]
    for name, points in sorted(tracks.items()):
        if not points:
            continue
        safe_name = html.escape(name)
        coords = " ".join(f"{lon:.7f},{lat:.7f},{alt:.2f}" for lon, lat, alt, _stamp in points)
        parts.extend([
            "<Placemark>",
            f"<name>{safe_name}</name>",
            "<LineString><tessellate>1</tessellate>",
            f"<coordinates>{coords}</coordinates>",
            "</LineString>",
            "</Placemark>",
        ])
        lon, lat, alt, stamp = points[-1]
        parts.extend([
            "<Placemark>",
            f"<name>{safe_name} latest</name>",
            f"<description>{html.escape(stamp)}</description>",
            f"<Point><coordinates>{lon:.7f},{lat:.7f},{alt:.2f}</coordinates></Point>",
            "</Placemark>",
        ])
    parts.extend(["</Document>", "</kml>"])
    return ("\n".join(parts) + "\n").encode("utf-8")


def pad_state_nodes(stages: Dict[int, StageState], now: float) -> List[Dict[str, object]]:
    nodes: List[Dict[str, object]] = []
    for dev_id in sorted(stages):
        stage = stages[dev_id]
        last_seen_ms = int((now - stage.last_update_mono) * 1000) if stage.last_update_mono > 0 else None
        mode = pad_mode_from_event(stage.currentEvent)
        nodes.append({
            "deviceId": stage.deviceId,
            "name": stage.name,
            "ok": last_seen_ms is not None and last_seen_ms <= OFFLINE_TIMEOUT_S * 1000,
            "lastSeenMs": last_seen_ms,
            "currentEvent": stage.currentEvent,
            "padState": {
                "mode": mode,
                "onPadIdle": mode == "idle",
                "onPadLaunchReady": mode == "launch_ready",
            },
        })
    return nodes


def wait_for_pad_mode(stages: Dict[int, StageState], mode: str, timeout_s: float) -> None:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        now = time.monotonic()
        active_nodes = [
            node for node in pad_state_nodes(stages, now)
            if node["ok"] and node["padState"]["mode"] is not None
        ]
        if active_nodes and all(node["padState"]["mode"] == mode for node in active_nodes):
            return
        time.sleep(0.1)


def altitude_zero_snapshot(stages: Dict[int, StageState], now: float) -> Dict[str, object]:
    return {
        "ok": True,
        "stages": [
            {
                "deviceId": stage.deviceId,
                "name": stage.name,
                "lastSeenMs": int((now - stage.last_update_mono) * 1000) if stage.last_update_mono > 0 else None,
                "altitudeZero": stage.altitudeZero,
                "current": {
                    "gpsAlt": stage.gpsAlt,
                    "baroAlt": stage.baroAlt,
                    "imuAlt": stage.imuAlt,
                    "fusedAlt": stage.fusedAlt,
                    "gpsRelAlt": stage.gpsRelAlt,
                    "baroRelAlt": stage.baroRelAlt,
                    "imuRelAlt": stage.imuRelAlt,
                    "fusedRelAlt": stage.fusedRelAlt,
                },
            }
            for stage in (stages[dev_id] for dev_id in sorted(stages))
        ],
    }


def persist_altitude_zero(stages: Dict[int, StageState]) -> Dict[str, Any]:
    data = {
        "version": 1,
        "updatedUtc": datetime.now(timezone.utc).isoformat(),
        "stages": {
            str(dev_id): stage.altitudeZero
            for dev_id, stage in stages.items()
            if stage.altitudeZero.get("setUtc")
        },
    }
    return save_altitude_zero(data)


def update_ground_station_from_stat(ground_station: Dict[str, object], stat: Dict[str, object]) -> None:
    ground_station["lastStatUtc"] = datetime.now(timezone.utc).isoformat()
    lat = _maybe_float(stat.get("lati"))
    lon = _maybe_float(stat.get("long"))
    alt = _maybe_float(stat.get("alti"))
    if lat is not None and lon is not None:
        ground_station["lat"] = lat
        ground_station["lon"] = lon
        ground_station["altitudeM"] = alt
        ground_station["gpsStatus"] = "Fix"


def handle_rxpk(rxpk: Dict[str, object], stages: Dict[int, StageState], now: float, flight_log: FlightLogger) -> bool:
    encoded = rxpk.get("data")
    if not isinstance(encoded, str):
        return False
    try:
        payload = base64.b64decode(encoded, validate=True)
    except (binascii.Error, ValueError):
        print("Ignoring rxpk with invalid base64 payload", flush=True)
        return False

    decoded = decode_payload(payload)
    if decoded is None:
        print(f"Ignoring non-UHRK payload of {len(payload)} bytes", flush=True)
        return False

    device_id = int(decoded["device_id"])
    if device_id not in stages:
        print(f"Ignoring packet with unknown device_id={device_id}", flush=True)
        return False

    stage = stages[device_id]
    if stage.last_update_mono > 0 and stage.seq == int(decoded["seq"]):
        return False

    stage.update_from_payload(decoded, rxpk, now)
    flight_log.append("packet", {
        "stage": stage.name,
        "deviceId": device_id,
        "decoded": decoded,
        "rxMeta": {
            "rssi": rxpk.get("rssi"),
            "lsnr": rxpk.get("lsnr"),
            "freq": rxpk.get("freq"),
            "datr": rxpk.get("datr"),
            "tmst": rxpk.get("tmst"),
        },
    })
    print(
        f"RX {stage.name}: seq={decoded['seq']} "
        f"rssi={rxpk.get('rssi')} snr={rxpk.get('lsnr')} datr={rxpk.get('datr')}",
        flush=True,
    )
    return True


def request_node_time_sync(url: str, epoch: float, source: str = "gc_gps") -> Dict[str, object]:
    body = json.dumps({
        "epoch": epoch,
        "utc": datetime.fromtimestamp(epoch, timezone.utc).isoformat(),
        "source": source,
    }).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            return {
                "url": url,
                "ok": 200 <= response.status < 300,
                "status": response.status,
                "response": json.loads(response.read().decode("utf-8") or "{}"),
            }
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
        return {"url": url, "ok": False, "error": str(exc)}


def current_gps_epoch(ground_station: Dict[str, object], now: float) -> Optional[float]:
    epoch = _maybe_float(ground_station.get("gpsTimeEpoch"))
    mono = _maybe_float(ground_station.get("gpsTimeMono"))
    if epoch is None or mono is None:
        return None
    age = now - mono
    if age < 0 or age > GPS_TIME_MAX_AGE_S:
        return None
    return epoch + age


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


def start_control_server(
    stages: Dict[int, StageState],
    ground_station: Dict[str, object],
    flight_log: FlightLogger,
    downlink: LoRaDownlink,
    started_mono: float,
) -> ThreadingHTTPServer:
    class ControlHandler(BaseHTTPRequestHandler):
        server_version = "UHRKControl/1.0"

        def log_message(self, fmt: str, *args: object) -> None:
            print(f"control: {fmt % args}", flush=True)

        def _send_json(self, status: int, payload: Dict[str, object]) -> None:
            data = json.dumps(payload, separators=(",", ":"), ensure_ascii=True).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Access-Control-Allow-Methods", "GET,POST,OPTIONS")
            self.send_header("Access-Control-Allow-Headers", "Content-Type")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def _send_bytes(self, status: int, body: bytes, content_type: str, filename: Optional[str] = None) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Cache-Control", "no-store")
            if filename:
                self.send_header("Content-Disposition", f'attachment; filename="{filename}"')
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_OPTIONS(self) -> None:
            self._send_json(204, {})

        def do_GET(self) -> None:
            parsed = urllib.parse.urlparse(self.path)
            path = parsed.path
            if path == "/api/settings":
                self._send_json(200, {"ok": True, "settings": load_settings()})
                return
            if path == "/api/version":
                self._send_json(200, {"ok": True, "version": APP_VERSION})
                return
            if path == "/api/health":
                self._send_json(200, health_snapshot(stages, ground_station, flight_log, downlink, started_mono))
                return
            if path == "/api/logs":
                self._send_json(200, log_list_snapshot(flight_log))
                return
            if path == "/api/logs/current":
                try:
                    body = flight_log.path.read_bytes()
                except OSError as exc:
                    self._send_json(500, {"ok": False, "error": str(exc)})
                    return
                self._send_bytes(200, body, "application/x-ndjson; charset=utf-8", flight_log.path.name)
                return
            if path == "/api/export/csv":
                body = export_csv_from_log(flight_log.path)
                self._send_bytes(200, body, "text/csv; charset=utf-8", f"{flight_log.path.stem}.csv")
                return
            if path == "/api/export/kml":
                body = export_kml_from_log(flight_log.path)
                self._send_bytes(200, body, "application/vnd.google-earth.kml+xml; charset=utf-8", f"{flight_log.path.stem}.kml")
                return
            if path == "/api/altitude-zero":
                self._send_json(200, altitude_zero_snapshot(stages, time.monotonic()))
                return
            if path == "/api/time-sync/status":
                self._send_json(200, {
                    "ok": True,
                    "systemUtc": datetime.now(timezone.utc).isoformat(),
                    "gpsTimeUtc": ground_station.get("gpsTimeUtc"),
                    "gpsTimeSource": ground_station.get("gpsTimeSource"),
                    "nodeTimeSyncUrls": NODE_TIME_SYNC_URLS,
                    "groundStation": ground_station,
                })
                return
            if path == "/api/pad-state":
                now = time.monotonic()
                self._send_json(200, {
                    "ok": True,
                    "transport": "lora",
                    "downlink": downlink.snapshot(),
                    "nodes": pad_state_nodes(stages, now),
                })
                return
            if path != "/api/shutdown/status":
                self._send_json(404, {"ok": False, "error": "not found"})
                return
            self._send_json(200, {
                "ok": True,
                "armed": False,
                "confirmationPhrase": SHUTDOWN_PHRASE,
                "holdMsRequired": 3000,
                "gcLogPath": str(flight_log.path),
                "nodeShutdownTransport": "lora",
                "padStateTransport": "lora",
                "downlink": downlink.snapshot(),
                "groundStation": ground_station,
                "stages": [stages[dev_id].to_dict(time.monotonic()) for dev_id in sorted(stages)],
            })

        def do_POST(self) -> None:
            path = self.path.split("?", 1)[0]
            if path == "/api/time-sync":
                length = int(self.headers.get("Content-Length", "0") or "0")
                try:
                    payload = json.loads(self.rfile.read(length).decode("utf-8") or "{}")
                except json.JSONDecodeError:
                    self._send_json(400, {"ok": False, "error": "invalid JSON"})
                    return
                epoch = _maybe_float(payload.get("epoch"))
                source = str(payload.get("source") or "manual")
                if epoch is None:
                    epoch = current_gps_epoch(ground_station, time.monotonic())
                    source = "gc_gps"
                if epoch is None:
                    self._send_json(400, {"ok": False, "error": "no valid time source available"})
                    return
                before = datetime.now(timezone.utc).isoformat()
                gc_result = set_system_time_from_epoch(epoch)
                node_results = [request_node_time_sync(url, epoch, source) for url in NODE_TIME_SYNC_URLS]
                response = {
                    "ok": bool(gc_result.get("ok")) and (all(result.get("ok") for result in node_results) if node_results else True),
                    "source": source,
                    "epoch": epoch,
                    "utc": datetime.fromtimestamp(epoch, timezone.utc).isoformat(),
                    "gc": gc_result,
                    "gcBeforeUtc": before,
                    "nodes": node_results,
                }
                flight_log.append("time_sync", {
                    "remote": self.client_address[0],
                    "manual": True,
                    "response": response,
                })
                self._send_json(200 if response["ok"] else 500, response)
                return
            if path == "/api/settings":
                length = int(self.headers.get("Content-Length", "0") or "0")
                try:
                    payload = json.loads(self.rfile.read(length).decode("utf-8") or "{}")
                except json.JSONDecodeError:
                    self._send_json(400, {"ok": False, "error": "invalid JSON"})
                    return
                settings = payload.get("settings")
                if not isinstance(settings, dict):
                    self._send_json(400, {"ok": False, "error": "settings object required"})
                    return
                saved = save_settings(settings)
                flight_log.append("settings_update", {"remote": self.client_address[0], "settings": saved})
                self._send_json(200, {"ok": True, "settings": saved})
                return
            if path == "/api/altitude-zero":
                length = int(self.headers.get("Content-Length", "0") or "0")
                try:
                    payload = json.loads(self.rfile.read(length).decode("utf-8") or "{}")
                except json.JSONDecodeError:
                    self._send_json(400, {"ok": False, "error": "invalid JSON"})
                    return
                action = str(payload.get("action") or "set")
                device_id_raw = payload.get("deviceId")
                targets = stages.values()
                if device_id_raw is not None:
                    try:
                        device_id = int(device_id_raw)
                    except (TypeError, ValueError):
                        self._send_json(400, {"ok": False, "error": "deviceId must be an integer"})
                        return
                    if device_id not in stages:
                        self._send_json(404, {"ok": False, "error": "unknown deviceId"})
                        return
                    targets = [stages[device_id]]
                changed: List[Dict[str, object]] = []
                if action == "set":
                    for stage in targets:
                        if stage.last_update_mono <= 0:
                            continue
                        changed.append({"deviceId": stage.deviceId, "zero": stage.set_current_altitude_zero()})
                elif action == "clear":
                    for stage in targets:
                        stage.clear_altitude_zero()
                        changed.append({"deviceId": stage.deviceId, "zero": None})
                else:
                    self._send_json(400, {"ok": False, "error": "action must be set or clear"})
                    return
                persisted = persist_altitude_zero(stages)
                snapshot = altitude_zero_snapshot(stages, time.monotonic())
                snapshot.update({"action": action, "changed": changed, "persisted": persisted})
                flight_log.append("altitude_zero", {
                    "remote": self.client_address[0],
                    "action": action,
                    "changed": changed,
                })
                self._send_json(200, snapshot)
                return
            if path == "/api/pad-state":
                length = int(self.headers.get("Content-Length", "0") or "0")
                try:
                    payload = json.loads(self.rfile.read(length).decode("utf-8") or "{}")
                except json.JSONDecodeError:
                    self._send_json(400, {"ok": False, "error": "invalid JSON"})
                    return
                mode = str(payload.get("mode") or "")
                if mode not in ("idle", "launch_ready"):
                    self._send_json(400, {"ok": False, "error": "mode must be idle or launch_ready"})
                    return
                command_result = downlink.send_pad_state(mode)
                if command_result.get("ok"):
                    wait_for_pad_mode(stages, mode, PAD_STATE_CONFIRM_TIMEOUT_S)
                now = time.monotonic()
                response = {
                    "ok": bool(command_result.get("ok")),
                    "mode": mode,
                    "transport": "lora",
                    "command": command_result,
                    "downlink": downlink.snapshot(),
                    "nodes": pad_state_nodes(stages, now),
                }
                flight_log.append("pad_state", {
                    "remote": self.client_address[0],
                    "mode": mode,
                    "transport": "lora",
                    "command": command_result,
                    "nodes": response["nodes"],
                })
                self._send_json(200, response)
                return
            if path != "/api/shutdown":
                self._send_json(404, {"ok": False, "error": "not found"})
                return
            length = int(self.headers.get("Content-Length", "0") or "0")
            try:
                payload = json.loads(self.rfile.read(length).decode("utf-8") or "{}")
            except json.JSONDecodeError:
                self._send_json(400, {"ok": False, "error": "invalid JSON"})
                return

            dry_run = bool(payload.get("dryRun"))
            if payload.get("armed") is not True:
                self._send_json(400, {"ok": False, "error": "shutdown is not armed"})
                return
            if payload.get("confirmation") != SHUTDOWN_PHRASE:
                self._send_json(400, {"ok": False, "error": "confirmation phrase mismatch"})
                return
            if int(payload.get("holdMs") or 0) < 3000:
                self._send_json(400, {"ok": False, "error": "hold duration too short"})
                return

            flight_log.append("shutdown_request", {
                "dryRun": dry_run,
                "remote": self.client_address[0],
                "groundStation": dict(ground_station),
                "stages": [stages[dev_id].to_dict(time.monotonic()) for dev_id in sorted(stages)],
            })
            shutdown_command = downlink.send_shutdown(dry_run)
            response = {
                "ok": bool(shutdown_command.get("ok")),
                "dryRun": dry_run,
                "gcLogPath": str(flight_log.path),
                "transport": "lora",
                "command": shutdown_command,
                "downlink": downlink.snapshot(),
                "gcShutdownScheduled": bool(shutdown_command.get("ok")) and not dry_run,
            }
            if shutdown_command.get("ok") and not dry_run:
                flight_log.append("shutdown_commit", response)
                run_shutdown_after_delay(8.0)
            self._send_json(200, response)

    server = ThreadingHTTPServer((CONTROL_HOST, CONTROL_PORT), ControlHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    print(f"UHRK control API listening on http://{CONTROL_HOST}:{CONTROL_PORT}", flush=True)
    return server


def main() -> None:
    started_mono = time.monotonic()
    flight_log = FlightLogger(LOG_DIR, "gc")
    stages = {
        dev_id: StageState(id=dev_id, name=STAGE_NAMES[dev_id], deviceId=dev_id)
        for dev_id in STAGE_NAMES
    }
    altitude_zero = load_altitude_zero()
    for dev_id, stage in stages.items():
        zero = altitude_zero.get("stages", {}).get(str(dev_id), {})
        if isinstance(zero, dict):
            stage.apply_altitude_zero(zero)
    ground_station: Dict[str, object] = {
        "name": "UHRK Ground Station",
        "radioPath": "SX1303 packet forwarder",
        "gatewayId": None,
        "gpsStatus": "No fix",
        "sats": None,
        "lat": None,
        "lon": None,
        "altitudeM": None,
        "lastStatUtc": None,
        "gpsTimeUtc": None,
        "gpsTimeEpoch": None,
        "gpsTimeMono": None,
        "gpsTimeSource": None,
    }
    ground_gps = GroundGpsReader(GROUND_GPS_PORT)
    downlink = LoRaDownlink()
    control_server = start_control_server(stages, ground_station, flight_log, downlink, started_mono)

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind((UDP_HOST, UDP_PORT))
    sock.settimeout(0.5)
    downlink.attach_socket(sock)
    print(f"UHRK backend listening on udp://{UDP_HOST}:{UDP_PORT}", flush=True)

    last_write = 0.0
    last_gc_time_sync = 0.0
    last_node_time_sync = 0.0
    write_json(stages, ground_station, time.monotonic())

    try:
        while True:
            now = time.monotonic()
            changed = False
            ground_station.update(ground_gps.snapshot())
            gps_epoch = current_gps_epoch(ground_station, now)
            if gps_epoch is not None and (now - last_gc_time_sync) >= GC_TIME_SYNC_INTERVAL_S:
                result = set_system_time_from_epoch(gps_epoch)
                flight_log.append("time_sync", {
                    "target": "ground_station",
                    "source": "gps",
                    "epoch": gps_epoch,
                    "result": result,
                })
                last_gc_time_sync = now
            if gps_epoch is not None and NODE_TIME_SYNC_URLS and (now - last_node_time_sync) >= NODE_TIME_SYNC_INTERVAL_S:
                node_results = [request_node_time_sync(url, gps_epoch) for url in NODE_TIME_SYNC_URLS]
                flight_log.append("time_sync", {
                    "target": "nodes",
                    "source": "gc_gps",
                    "epoch": gps_epoch,
                    "results": node_results,
                })
                last_node_time_sync = now
            try:
                data, addr = sock.recvfrom(65535)
            except socket.timeout:
                data = b""
                addr = ("", 0)

            if data and len(data) >= 4:
                identifier = data[3]
                if identifier == PUSH_DATA:
                    sock.sendto(ack(data, PUSH_ACK), addr)
                    gateway_id = gateway_id_from_push(data)
                    if gateway_id:
                        ground_station["gatewayId"] = gateway_id
                    if len(data) > 12:
                        try:
                            body = json.loads(data[12:].decode("utf-8"))
                        except (json.JSONDecodeError, UnicodeDecodeError):
                            body = {}
                        if isinstance(body.get("stat"), dict):
                            update_ground_station_from_stat(ground_station, body["stat"])
                        for rxpk in body.get("rxpk", []):
                            if isinstance(rxpk, dict):
                                changed = handle_rxpk(rxpk, stages, now, flight_log) or changed
                elif identifier == PULL_DATA:
                    downlink.record_pull_data(data, addr)
                    sock.sendto(ack(data, PULL_ACK), addr)
                elif identifier == TX_ACK:
                    downlink.record_tx_ack(data)

            if changed or (now - last_write) >= WRITE_INTERVAL_S:
                write_json(stages, ground_station, now)
                flight_log.append("ground_snapshot", {
                    "groundStation": dict(ground_station),
                    "stages": [stages[dev_id].to_dict(now) for dev_id in sorted(stages)],
                })
                last_write = now
    except KeyboardInterrupt:
        print("Exiting UHRK backend", flush=True)
    finally:
        flight_log.append("system", {"event": "backend_stopping"})
        flight_log.close()
        control_server.shutdown()
        sock.close()


if __name__ == "__main__":
    main()
