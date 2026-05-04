"""
gps_reader.py
=================

Background NMEA reader for the BerryGPS-IMU HAT.

The node keeps separate satellite counts for satellites used in the
navigation solution and satellites merely visible in GSV messages. That
distinction matters: seeing ten satellites is not the same as having ten
satellites in the fix.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from typing import Optional

import pynmea2  # type: ignore
import serial  # type: ignore


@dataclass
class GPSData:
    """Container for the latest GPS state."""

    lat: float
    lon: float
    alt_m: float
    status: int
    sats: int
    sats_used: int = 0
    sats_in_view: int = 0
    hdop: float | None = None
    pdop: float | None = None
    vdop: float | None = None


class GPSReader:
    """Continuously read NMEA sentences from a serial port."""

    def __init__(self, port: str = "/dev/serial0", baudrate: int = 9600, timeout: float = 1.0) -> None:
        self.ser = serial.Serial(port, baudrate=baudrate, timeout=timeout)
        self._lock = threading.Lock()
        self._latest: GPSData = GPSData(lat=0.0, lon=0.0, alt_m=0.0, status=0, sats=0)
        self._fix_dimension = 1
        self._stop = False
        self._thread = threading.Thread(target=self._read_loop, daemon=True)
        self._thread.start()

    def _status_for_valid_position(self) -> int:
        return 2 if self._fix_dimension >= 3 else 1

    def _latest_with(self, **updates: object) -> GPSData:
        data = {
            "lat": self._latest.lat,
            "lon": self._latest.lon,
            "alt_m": self._latest.alt_m,
            "status": self._latest.status,
            "sats": self._latest.sats,
            "sats_used": self._latest.sats_used,
            "sats_in_view": self._latest.sats_in_view,
            "hdop": self._latest.hdop,
            "pdop": self._latest.pdop,
            "vdop": self._latest.vdop,
        }
        data.update(updates)
        data["sats"] = data.get("sats_used") or data.get("sats") or 0
        return GPSData(**data)  # type: ignore[arg-type]

    def _float_attr(self, msg: object, attr: str, fallback: float | None = None) -> float | None:
        value = getattr(msg, attr, None)
        try:
            return float(value) if value not in (None, "") else fallback
        except (TypeError, ValueError):
            return fallback

    def _parse_sentence(self, line: str) -> Optional[GPSData]:
        try:
            msg = pynmea2.parse(line)
        except pynmea2.ParseError:
            return None

        if isinstance(msg, pynmea2.types.talker.GGA):  # type: ignore[attr-defined]
            sats_used = int(getattr(msg, "num_sats", 0) or 0)
            fix_quality = int(getattr(msg, "gps_qual", 0) or 0)
            status = 0 if fix_quality == 0 else self._status_for_valid_position()
            alt = self._float_attr(msg, "altitude", self._latest.alt_m)
            hdop = self._float_attr(msg, "horizontal_dil", self._latest.hdop)
            return self._latest_with(
                lat=msg.latitude,
                lon=msg.longitude,
                alt_m=alt if alt is not None else self._latest.alt_m,
                status=status,
                sats=sats_used,
                sats_used=sats_used,
                hdop=hdop,
            )

        if isinstance(msg, pynmea2.types.talker.RMC):  # type: ignore[attr-defined]
            status = 0 if getattr(msg, "status", "V") != "A" else self._status_for_valid_position()
            return self._latest_with(lat=msg.latitude, lon=msg.longitude, status=status)

        if getattr(msg, "sentence_type", "") == "GSA":
            try:
                self._fix_dimension = int(getattr(msg, "mode_fix_type", self._fix_dimension) or self._fix_dimension)
            except (TypeError, ValueError):
                pass
            updates: dict[str, object] = {}
            for attr in ("pdop", "hdop", "vdop"):
                value = self._float_attr(msg, attr, getattr(self._latest, attr))
                if value is not None:
                    updates[attr] = value
            if self._latest.status > 0:
                updates["status"] = self._status_for_valid_position()
            return self._latest_with(**updates) if updates else None

        if getattr(msg, "sentence_type", "") == "GSV":
            try:
                sats_in_view = int(getattr(msg, "num_sv_in_view", self._latest.sats_in_view) or self._latest.sats_in_view)
            except (TypeError, ValueError):
                sats_in_view = self._latest.sats_in_view
            return self._latest_with(sats_in_view=sats_in_view)

        return None

    def _read_loop(self) -> None:
        while not self._stop:
            try:
                line_bytes = self.ser.readline()
                if not line_bytes:
                    continue
                line = line_bytes.decode("ascii", errors="ignore").strip()
                if not line.startswith("$"):
                    continue
                data = self._parse_sentence(line)
                if data is not None:
                    with self._lock:
                        self._latest = data
            except Exception:
                continue

    def read(self) -> GPSData:
        with self._lock:
            return self._latest

    def close(self) -> None:
        self._stop = True
        try:
            self._thread.join(timeout=1.0)
        except Exception:
            pass
        try:
            self.ser.close()
        except Exception:
            pass
