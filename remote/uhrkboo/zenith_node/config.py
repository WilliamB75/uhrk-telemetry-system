"""
Configuration for the Zenith Pi Zero 2 W telemetry node.

This module defines a simple dataclass holding all of the configurable
parameters for the transmitter. The values here can be adjusted
before running on the Raspberry Pi to change the device identifier,
radio frequency, transmission cadence, scaling factors and flight
event detection thresholds.
"""

from dataclasses import dataclass


@dataclass
class Config:
    """Container for all node‑side configuration values."""

    # Device ID mapping to stage names (0 = Booster, 1 = Sustainer, 2 = Payload)
    DEVICE_ID: int = 0

    # LoRa radio frequency in MHz. Use 868.0 MHz for the EU/UK 868 band.
    RADIO_FREQ_MHZ: float = 868.1

    # LoRa modem settings. These must match the SX1303 packet forwarder.
    SIGNAL_BANDWIDTH: int = 125000
    CODING_RATE: int = 5
    SPREADING_FACTOR: int = 10
    PREAMBLE_LENGTH: int = 8
    SYNC_WORD: int = 0x34

    # LoRa transmit power (range 5 .. 23). Higher values give longer range
    # but draw more current. For bench testing you can leave this high; for
    # flight you may reduce it to conserve battery.
    TX_POWER: int = 20

    # Cadence in seconds between successive telemetry packets. One packet
    # every second keeps the ground station fed with fresh data without
    # flooding the channel. Increase this value to reduce airtime.
    CADENCE_SECONDS: float = 1.0
    LAUNCH_READY_CADENCE_SECONDS: float = 0.45
    LAUNCH_READY_DEVICE_SLOT_SECONDS: float = 0.12

    # Scaling factors used when converting floating point values to
    # integers for the compact binary packet. These must remain
    # consistent on both the transmitter and receiver.
    LAT_LON_SCALE: float = 1e7  # decimal degrees → scaled integer
    ALT_SCALE: float = 100.0    # metres → centimetres
    ACC_SCALE: float = 10.0     # m/s² → tenths of m/s²
    GYRO_SCALE: float = 10.0    # deg/s → tenths of deg/s

    # Thresholds for simple event detection. These values are
    # intentionally conservative; you may tune them after flight testing.
    BURN_ACTIVE_ACC_THRESHOLD: float = 15.0  # m/s² above gravity for burnActive
    BURN_OUT_ACC_THRESHOLD: float = 3.0       # m/s² threshold indicating burn end
    LAUNCH_CONFIRM_SAMPLES: int = 3
    MIN_LAUNCH_ALTITUDE_DELTA: float = 8.0
    STAGE_SEP_ALTITUDE: float = 3000.0        # m altitude to flag stage separation
    DROGUE_DROP_ALT: float = 50.0             # m drop from apogee to deploy drogue
    MAIN_DEPLOY_ALTITUDE: float = 500.0       # m altitude to deploy main chute
    LANDED_ALTITUDE: float = 5.0              # m altitude considered landed
    LANDED_VSPEED_THRESHOLD: float = 1.0      # m/s vertical speed threshold for landing

    # Standard gravity used for calibrating the accelerometer.
    GRAVITY: float = 9.80665
