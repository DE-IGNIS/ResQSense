"""
HLK-LD2410D 24 GHz mmWave Presence Sensor – Parser & Driver
=============================================================

Implements the binary UART protocol documented for the LD2410 family:

Data Report Frame (sensor → host):
  Header   : F4 F3 F2 F1
  Length    : 2 bytes (LE) – payload length
  Payload  : target-state data (see TargetData)
  Footer   : F8 F7 F6 F5

Command Frame (host → sensor):
  Header   : FD FC FB FA
  Length    : 2 bytes (LE)
  Command  : 2 bytes (LE)
  Params   : N bytes
  Footer   : 04 03 02 01

All multi-byte integers are **little-endian**.

Default UART: 256 000 baud, 8N1.
"""

from __future__ import annotations

import logging
import struct
import time
from dataclasses import dataclass, field
from enum import IntEnum
from typing import Callable, Optional

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Constants & frame markers
# ---------------------------------------------------------------------------
DATA_HEADER  = bytes([0xF4, 0xF3, 0xF2, 0xF1])
DATA_FOOTER  = bytes([0xF8, 0xF7, 0xF6, 0xF5])
CMD_HEADER   = bytes([0xFD, 0xFC, 0xFB, 0xFA])
CMD_FOOTER   = bytes([0x04, 0x03, 0x02, 0x01])

MAX_DISTANCE_GATES = 9  # gates 0-8


class TargetState(IntEnum):
    """Target-state byte in the data report."""
    NO_TARGET       = 0x00
    MOVING          = 0x01
    STATIONARY      = 0x02
    MOVING_AND_STAT = 0x03


class CommandWord(IntEnum):
    """Host → sensor command words."""
    ENABLE_CONFIG        = 0x00FF
    END_CONFIG           = 0x00FE
    READ_FIRMWARE        = 0x00A0
    SET_MAX_DISTANCE     = 0x0060
    READ_PARAMETERS      = 0x0061
    ENABLE_ENGINEERING   = 0x0062
    DISABLE_ENGINEERING  = 0x0063
    SET_SENSITIVITY      = 0x0064
    FACTORY_RESET        = 0x00A2
    RESTART              = 0x00A3


# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------
@dataclass
class TargetData:
    """Parsed content of one LD2410D data-report frame."""
    timestamp: float = 0.0
    state: TargetState = TargetState.NO_TARGET

    # Moving target
    moving_distance_cm: int = 0
    moving_energy: int = 0        # 0-100

    # Stationary target
    stationary_distance_cm: int = 0
    stationary_energy: int = 0    # 0-100

    # Detection distance (may appear in some firmware versions)
    detection_distance_cm: int = 0

    # Engineering-mode per-gate energies (populated only in engineering mode)
    moving_gate_energies: list[int] = field(default_factory=list)
    stationary_gate_energies: list[int] = field(default_factory=list)

    @property
    def has_target(self) -> bool:
        return self.state != TargetState.NO_TARGET

    @property
    def has_moving_target(self) -> bool:
        return self.state in (TargetState.MOVING, TargetState.MOVING_AND_STAT)

    @property
    def has_stationary_target(self) -> bool:
        return self.state in (TargetState.STATIONARY, TargetState.MOVING_AND_STAT)

    def to_dict(self) -> dict:
        return {
            "timestamp": self.timestamp,
            "state": self.state.name,
            "has_target": self.has_target,
            "moving_distance_cm": self.moving_distance_cm,
            "moving_energy": self.moving_energy,
            "stationary_distance_cm": self.stationary_distance_cm,
            "stationary_energy": self.stationary_energy,
            "detection_distance_cm": self.detection_distance_cm,
            "moving_gate_energies": self.moving_gate_energies,
            "stationary_gate_energies": self.stationary_gate_energies,
        }


@dataclass
class SensorConfig:
    """Current parameter set read back from the sensor."""
    max_moving_gate: int = 8
    max_stationary_gate: int = 8
    unmanned_duration: int = 0  # seconds (0 = infinite hold)
    moving_sensitivities: list[int] = field(default_factory=list)
    stationary_sensitivities: list[int] = field(default_factory=list)


@dataclass
class AckResponse:
    """Parsed acknowledgement from the sensor."""
    command: int = 0
    success: bool = False
    data: bytes = b""


# ---------------------------------------------------------------------------
# Parser
# ---------------------------------------------------------------------------
class LD2410DParser:
    """
    Stateless parser for LD2410D binary frames.

    Both data-report frames and command-ACK frames can be fed into
    :meth:`parse_frame` for demultiplexing.
    """

    # ---- data reports -----------------------------------------------------

    @staticmethod
    def parse_data_frame(raw: bytes) -> Optional[TargetData]:
        """
        Parse a full data-report frame including header/footer.

        Parameters
        ----------
        raw : bytes
            Complete frame starting with ``F4 F3 F2 F1`` and ending with
            ``F8 F7 F6 F5``.

        Returns
        -------
        TargetData or None if the frame is malformed.
        """
        if not raw.startswith(DATA_HEADER) or not raw.endswith(DATA_FOOTER):
            logger.debug("Frame missing header/footer markers")
            return None

        inner = raw[4:-4]  # strip header + footer
        if len(inner) < 2:
            return None

        payload_len = struct.unpack_from("<H", inner, 0)[0]
        payload = inner[2:]

        if len(payload) < payload_len:
            logger.debug("Payload shorter than declared length")
            return None

        return LD2410DParser._parse_target_payload(payload[:payload_len])

    @staticmethod
    def _parse_target_payload(payload: bytes) -> Optional[TargetData]:
        """
        Decode the target-data payload (after length field).

        Standard-mode payload layout (minimum 7 bytes):
          [0]       data type marker (0x02 = standard, 0x01 = engineering)
          [1]       head byte (0xAA)
          [2]       target state
          [3-4]     moving distance  (LE, cm)
          [5]       moving energy
          [6-7]     stationary distance (LE, cm)
          [8]       stationary energy
          [9-10]    detection distance (LE, cm)
          ...       engineering-mode gate data (if present)
        """
        if len(payload) < 7:
            return None

        td = TargetData(timestamp=time.time())

        offset = 0
        data_type = payload[offset]; offset += 1

        # Some firmware versions include a head byte 0xAA
        if offset < len(payload) and payload[offset] == 0xAA:
            offset += 1

        if offset >= len(payload):
            return None

        try:
            td.state = TargetState(payload[offset])
        except ValueError:
            td.state = TargetState.NO_TARGET
        offset += 1

        # Moving target distance (2 bytes LE) + energy (1 byte)
        if offset + 3 <= len(payload):
            td.moving_distance_cm = struct.unpack_from("<H", payload, offset)[0]
            offset += 2
            td.moving_energy = payload[offset]
            offset += 1

        # Stationary target distance (2 bytes LE) + energy (1 byte)
        if offset + 3 <= len(payload):
            td.stationary_distance_cm = struct.unpack_from("<H", payload, offset)[0]
            offset += 2
            td.stationary_energy = payload[offset]
            offset += 1

        # Detection distance (2 bytes LE) – optional
        if offset + 2 <= len(payload):
            td.detection_distance_cm = struct.unpack_from("<H", payload, offset)[0]
            offset += 2

        # Engineering mode: per-gate energies follow
        if data_type == 0x01 and offset < len(payload):
            td = LD2410DParser._parse_engineering_data(payload, offset, td)

        return td

    @staticmethod
    def _parse_engineering_data(
        payload: bytes, offset: int, td: TargetData
    ) -> TargetData:
        """Append per-gate energy values from engineering-mode data."""
        # Max moving gate count (1 byte)
        if offset >= len(payload):
            return td
        max_moving = payload[offset] + 1
        offset += 1

        # Max stationary gate count (1 byte)
        if offset >= len(payload):
            return td
        max_static = payload[offset] + 1
        offset += 1

        # Moving gate energies
        moving_energies = []
        for _ in range(min(max_moving, MAX_DISTANCE_GATES)):
            if offset >= len(payload):
                break
            moving_energies.append(payload[offset])
            offset += 1
        td.moving_gate_energies = moving_energies

        # Stationary gate energies
        static_energies = []
        for _ in range(min(max_static, MAX_DISTANCE_GATES)):
            if offset >= len(payload):
                break
            static_energies.append(payload[offset])
            offset += 1
        td.stationary_gate_energies = static_energies

        return td

    # ---- command ACKs -----------------------------------------------------

    @staticmethod
    def parse_ack_frame(raw: bytes) -> Optional[AckResponse]:
        """
        Parse a command-ACK frame (CMD_HEADER … CMD_FOOTER).

        Returns an AckResponse or None.
        """
        if not raw.startswith(CMD_HEADER) or not raw.endswith(CMD_FOOTER):
            return None

        inner = raw[4:-4]
        if len(inner) < 4:
            return None

        payload_len = struct.unpack_from("<H", inner, 0)[0]
        cmd_word = struct.unpack_from("<H", inner, 2)[0]

        ack = AckResponse(command=cmd_word)
        data_part = inner[4:]

        if len(data_part) >= 2:
            status = struct.unpack_from("<H", data_part, 0)[0]
            ack.success = (status == 0x0000)
            ack.data = data_part[2:]
        else:
            ack.success = False

        return ack

    # ---- parse parameters response ----------------------------------------

    @staticmethod
    def parse_config_response(ack: AckResponse) -> Optional[SensorConfig]:
        """
        Decode the response payload of a READ_PARAMETERS (0x0061) ACK.
        """
        data = ack.data
        if len(data) < 4:
            return None

        cfg = SensorConfig()
        offset = 0

        # Header byte (0xAA typically) – skip
        if data[offset] == 0xAA:
            offset += 1

        if offset + 2 > len(data):
            return cfg

        cfg.max_moving_gate = data[offset]; offset += 1
        cfg.max_stationary_gate = data[offset]; offset += 1

        # Per-gate moving sensitivities
        for i in range(min(cfg.max_moving_gate + 1, MAX_DISTANCE_GATES)):
            if offset >= len(data):
                break
            cfg.moving_sensitivities.append(data[offset])
            offset += 1

        # Per-gate stationary sensitivities
        for i in range(min(cfg.max_stationary_gate + 1, MAX_DISTANCE_GATES)):
            if offset >= len(data):
                break
            cfg.stationary_sensitivities.append(data[offset])
            offset += 1

        # Unmanned duration (2 bytes LE) – optional
        if offset + 2 <= len(data):
            cfg.unmanned_duration = struct.unpack_from("<H", data, offset)[0]

        return cfg


# ---------------------------------------------------------------------------
# Command Builder
# ---------------------------------------------------------------------------
class LD2410DCommandBuilder:
    """
    Constructs binary command frames to send *to* the LD2410D sensor
    (via the ESP32).

    Every configuration transaction must be wrapped with
    :meth:`enable_config` … :meth:`end_config`.
    """

    @staticmethod
    def _build_frame(command: int, value: bytes = b"") -> bytes:
        """Wrap a command word + value in the LD2410D command frame."""
        cmd_bytes = struct.pack("<H", command)
        length = len(cmd_bytes) + len(value)
        return CMD_HEADER + struct.pack("<H", length) + cmd_bytes + value + CMD_FOOTER

    # ---- configuration lifecycle ------------------------------------------

    @staticmethod
    def enable_config() -> bytes:
        return LD2410DCommandBuilder._build_frame(
            CommandWord.ENABLE_CONFIG,
            struct.pack("<H", 0x0001),
        )

    @staticmethod
    def end_config() -> bytes:
        return LD2410DCommandBuilder._build_frame(CommandWord.END_CONFIG)

    # ---- queries ----------------------------------------------------------

    @staticmethod
    def read_firmware() -> bytes:
        return LD2410DCommandBuilder._build_frame(CommandWord.READ_FIRMWARE)

    @staticmethod
    def read_parameters() -> bytes:
        return LD2410DCommandBuilder._build_frame(CommandWord.READ_PARAMETERS)

    # ---- mode control -----------------------------------------------------

    @staticmethod
    def enable_engineering_mode() -> bytes:
        return LD2410DCommandBuilder._build_frame(CommandWord.ENABLE_ENGINEERING)

    @staticmethod
    def disable_engineering_mode() -> bytes:
        return LD2410DCommandBuilder._build_frame(CommandWord.DISABLE_ENGINEERING)

    # ---- parameter writes -------------------------------------------------

    @staticmethod
    def set_max_distance(
        max_moving_gate: int,
        max_stationary_gate: int,
        unmanned_duration: int = 0,
    ) -> bytes:
        """
        Set maximum detection distance gates and no-person duration.

        Parameters
        ----------
        max_moving_gate : int
            1-8, farthest moving-target gate.
        max_stationary_gate : int
            1-8, farthest stationary-target gate.
        unmanned_duration : int
            Seconds to hold "no target" before reporting (0 = instant).
        """
        value = struct.pack(
            "<HH HH HH",
            0x0000, max_moving_gate,
            0x0001, max_stationary_gate,
            0x0002, unmanned_duration,
        )
        return LD2410DCommandBuilder._build_frame(
            CommandWord.SET_MAX_DISTANCE, value
        )

    @staticmethod
    def set_gate_sensitivity(
        gate: int,
        moving_sensitivity: int,
        stationary_sensitivity: int,
    ) -> bytes:
        """
        Set per-gate sensitivity thresholds.

        Parameters
        ----------
        gate : int
            Gate index (0-8).
        moving_sensitivity : int
            0-100 threshold for moving targets.
        stationary_sensitivity : int
            0-100 threshold for stationary targets.
        """
        value = struct.pack(
            "<HH HH HH",
            0x0000, gate,
            0x0001, moving_sensitivity,
            0x0002, stationary_sensitivity,
        )
        return LD2410DCommandBuilder._build_frame(
            CommandWord.SET_SENSITIVITY, value
        )

    # ---- maintenance ------------------------------------------------------

    @staticmethod
    def factory_reset() -> bytes:
        return LD2410DCommandBuilder._build_frame(CommandWord.FACTORY_RESET)

    @staticmethod
    def restart() -> bytes:
        return LD2410DCommandBuilder._build_frame(CommandWord.RESTART)
