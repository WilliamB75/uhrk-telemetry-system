"""
Conservative finite-state flight event detector.

Walking, handling, and sensor spikes should not be able to produce recovery
events. The detector therefore requires the ground station to put the node into
Launch Ready and then requires sustained boost-like acceleration before any
flight event can progress.
"""

from __future__ import annotations

from typing import Optional

from .config import Config
from .gps_reader import GPSData
from .imu_baro_reader import IMUData


class EventDetector:
    """Derive one current flight event from sensor data."""

    def __init__(self, config: Optional[Config] = None) -> None:
        self.config = config if config is not None else Config()
        self._burn_active = False
        self._burnout = False
        self._stage_sep = False
        self._drogue = False
        self._main = False
        self._landed = False
        self._max_alt = float("-inf")
        self._prev_alt: Optional[float] = None
        self._pad_alt: Optional[float] = None
        self._boost_samples = 0

    def _current_flight_flag(self) -> int:
        if self._landed:
            return 1 << 5
        if self._main:
            return 1 << 4
        if self._drogue:
            return 1 << 3
        if self._stage_sep:
            return 1 << 2
        if self._burnout:
            return 1 << 1
        if self._burn_active:
            return 1 << 0
        return 0

    def update(self, gps: GPSData, imu: IMUData, launch_ready: bool = False) -> int:
        alt = imu.baro_alt_m if imu.baro_alt_m is not None else gps.alt_m
        if self._pad_alt is None or not launch_ready and not self._burn_active:
            self._pad_alt = alt

        vert_acc = imu.az - self.config.GRAVITY
        acc_mag = abs(vert_acc)

        if alt > self._max_alt:
            self._max_alt = alt

        if not launch_ready and not self._burn_active:
            self._boost_samples = 0
            self._prev_alt = alt
            return 0

        if not self._burn_active:
            if acc_mag > self.config.BURN_ACTIVE_ACC_THRESHOLD:
                self._boost_samples += 1
            else:
                self._boost_samples = 0
            if self._boost_samples >= self.config.LAUNCH_CONFIRM_SAMPLES:
                self._burn_active = True

        if self._burn_active and not self._burnout and acc_mag < self.config.BURN_OUT_ACC_THRESHOLD:
            self._burnout = True

        altitude_delta = alt - (self._pad_alt if self._pad_alt is not None else alt)
        recovery_armed = self._burnout and altitude_delta >= self.config.MIN_LAUNCH_ALTITUDE_DELTA

        if recovery_armed and not self._stage_sep and alt > self.config.STAGE_SEP_ALTITUDE:
            self._stage_sep = True

        if recovery_armed and not self._drogue and (self._max_alt - alt) > self.config.DROGUE_DROP_ALT:
            self._drogue = True

        main_armed = self._drogue or (recovery_armed and self._max_alt > (self.config.MAIN_DEPLOY_ALTITUDE + self.config.DROGUE_DROP_ALT))
        if not self._main and main_armed and alt < self.config.MAIN_DEPLOY_ALTITUDE:
            self._main = True

        if self._prev_alt is not None:
            vertical_speed = alt - self._prev_alt
            landing_armed = self._main or self._drogue
            if (
                not self._landed
                and landing_armed
                and alt < self.config.LANDED_ALTITUDE
                and abs(vertical_speed) < self.config.LANDED_VSPEED_THRESHOLD
            ):
                self._landed = True
        self._prev_alt = alt

        return self._current_flight_flag()
