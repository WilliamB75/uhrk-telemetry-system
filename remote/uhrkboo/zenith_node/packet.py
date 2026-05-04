"""
packet.py
==========

This module defines the binary packet format used by the telemetry
node and provides helper functions for packing and unpacking
telemetry packets. The layout is frozen to ensure that the ground
station can decode packets consistently.

The packet layout (big‑endian)::

    +-------------+-------+------------------------------+
    | Offset (B)  | Type  | Field                        |
    +-------------+-------+------------------------------+
    | 0           | u8    | device_id                   |
    | 1           | u16   | seq                         |
    | 3           | i32   | lat (degrees * 1e7)         |
    | 7           | i32   | lon (degrees * 1e7)         |
    | 11          | i32   | gps_alt_m (metres * 100)    |
    | 15          | i32   | baro_alt_m (metres * 100)   |
    | 19          | i32   | imu_alt_m (metres * 100)    |
    | 23          | u8    | gps_status                  |
    | 24          | u8    | sats used/view nibbles      |
    | 25          | i16   | ax (m/s² * 10)              |
    | 27          | i16   | ay (m/s² * 10)              |
    | 29          | i16   | az (m/s² * 10)              |
    | 31          | i16   | gx (deg/s * 10)             |
    | 33          | i16   | gy (deg/s * 10)             |
    | 35          | i16   | gz (deg/s * 10)             |
    | 37          | u16   | event_flags                 |
    +-------------+-------+------------------------------+

The total packet size is 39 bytes. See :mod:`config` for the
scaling factors used during packing and unpacking.
"""

from __future__ import annotations

import struct
from typing import Dict, Any

from .config import Config


# Precompile the struct for the packet format. '>' denotes big‑endian.
_PACKET_STRUCT = struct.Struct('>B H i i i i i B B h h h h h h H')


def encode_satellite_counts(sats_used: int, sats_in_view: int | None = None) -> int:
    """Pack sats-in-view and sats-used into one byte.

    Upper nibble: satellites in view. Lower nibble: satellites used in the fix.
    Values are capped at 15, which is enough for the compact dashboard signal
    while keeping the LoRa packet length unchanged.
    """
    used = max(0, min(15, int(sats_used or 0)))
    view = max(0, min(15, int(sats_in_view if sats_in_view is not None else used)))
    return (view << 4) | used


def decode_satellite_counts(encoded: int, gps_status: int = 0) -> tuple[int, int]:
    """Decode the compact satellite count byte.

    Legacy packets used the whole byte as a single count. Treat values <= 15 as
    legacy and infer used satellites only when the packet has a fix.
    """
    raw = int(encoded or 0) & 0xFF
    if raw <= 15:
        return (raw if gps_status > 0 else 0), raw
    return raw & 0x0F, (raw >> 4) & 0x0F


def _clamp_int16(value: float) -> int:
    """Clamp a floating point value into the range of a signed 16‑bit integer."""
    ivalue = int(round(value))
    if ivalue > 32767:
        return 32767
    if ivalue < -32768:
        return -32768
    return ivalue


def _clamp_int32(value: float) -> int:
    """Clamp a floating point value into the range of a signed 32‑bit integer."""
    ivalue = int(round(value))
    if ivalue > 0x7FFFFFFF:
        return 0x7FFFFFFF
    if ivalue < -0x80000000:
        return -0x80000000
    return ivalue


def pack_payload(
    *,
    device_id: int,
    seq: int,
    lat: float,
    lon: float,
    gps_alt_m: float,
    baro_alt_m: float,
    imu_alt_m: float,
    gps_status: int,
    sats: int,
    sats_in_view: int,
    ax: float,
    ay: float,
    az: float,
    gx: float,
    gy: float,
    gz: float,
    event_flags: int,
    config: Config = Config(),
) -> bytes:
    """Pack the provided telemetry values into a 39‑byte binary payload.

    All conversions and scaling factors are taken from the provided
    :class:`~config.Config` instance. Integer fields are masked to
    their appropriate bit widths.
    """
    lat_i = _clamp_int32(lat * config.LAT_LON_SCALE)
    lon_i = _clamp_int32(lon * config.LAT_LON_SCALE)
    gps_alt_i = _clamp_int32(gps_alt_m * config.ALT_SCALE)
    baro_alt_i = _clamp_int32(baro_alt_m * config.ALT_SCALE)
    imu_alt_i = _clamp_int32(imu_alt_m * config.ALT_SCALE)
    ax_i = _clamp_int16(ax * config.ACC_SCALE)
    ay_i = _clamp_int16(ay * config.ACC_SCALE)
    az_i = _clamp_int16(az * config.ACC_SCALE)
    gx_i = _clamp_int16(gx * config.GYRO_SCALE)
    gy_i = _clamp_int16(gy * config.GYRO_SCALE)
    gz_i = _clamp_int16(gz * config.GYRO_SCALE)

    return _PACKET_STRUCT.pack(
        device_id & 0xFF,
        seq & 0xFFFF,
        lat_i,
        lon_i,
        gps_alt_i,
        baro_alt_i,
        imu_alt_i,
        gps_status & 0xFF,
        encode_satellite_counts(sats, sats_in_view),
        ax_i,
        ay_i,
        az_i,
        gx_i,
        gy_i,
        gz_i,
        event_flags & 0xFFFF,
    )


def unpack_payload(payload: bytes, config: Config = Config()) -> Dict[str, Any]:
    """Unpack a binary payload and return a dictionary of values.

    The inverse of :func:`pack_payload`. If the payload length is
    incorrect a :class:`ValueError` is raised.
    """
    if len(payload) != _PACKET_STRUCT.size:
        raise ValueError(f"Unexpected payload length {len(payload)} bytes (expected {_PACKET_STRUCT.size})")

    (
        device_id,
        seq,
        lat_i,
        lon_i,
        gps_alt_i,
        baro_alt_i,
        imu_alt_i,
        gps_status,
        sats_encoded,
        ax_i,
        ay_i,
        az_i,
        gx_i,
        gy_i,
        gz_i,
        event_flags,
    ) = _PACKET_STRUCT.unpack(payload)

    return {
        "device_id": device_id,
        "seq": seq,
        "lat": lat_i / config.LAT_LON_SCALE,
        "lon": lon_i / config.LAT_LON_SCALE,
        "gps_alt_m": gps_alt_i / config.ALT_SCALE,
        "baro_alt_m": baro_alt_i / config.ALT_SCALE,
        "imu_alt_m": imu_alt_i / config.ALT_SCALE,
        "gps_status": gps_status,
        "sats": sats_encoded,
        "sats_used": decode_satellite_counts(sats_encoded, gps_status)[0],
        "sats_in_view": decode_satellite_counts(sats_encoded, gps_status)[1],
        "ax": ax_i / config.ACC_SCALE,
        "ay": ay_i / config.ACC_SCALE,
        "az": az_i / config.ACC_SCALE,
        "gx": gx_i / config.GYRO_SCALE,
        "gy": gy_i / config.GYRO_SCALE,
        "gz": gz_i / config.GYRO_SCALE,
        "event_flags": event_flags,
    }
