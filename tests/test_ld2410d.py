"""
Tests for LD2410D parser and command builder.
"""

import struct
import time
import pytest

from src.hardware.ld2410d import (
    LD2410DParser,
    LD2410DCommandBuilder,
    TargetData,
    TargetState,
    SensorConfig,
    DATA_HEADER,
    DATA_FOOTER,
    CMD_HEADER,
    CMD_FOOTER,
    CommandWord,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def build_data_frame(payload: bytes) -> bytes:
    """Wrap a payload in a valid LD2410D data-report frame."""
    length = struct.pack("<H", len(payload))
    return DATA_HEADER + length + payload + DATA_FOOTER


def build_standard_payload(
    state: int = 0x03,
    mov_dist: int = 150,
    mov_energy: int = 45,
    stat_dist: int = 200,
    stat_energy: int = 60,
    det_dist: int = 200,
) -> bytes:
    """Build a standard-mode target payload (data type 0x02, head 0xAA)."""
    return bytes([0x02, 0xAA, state]) + struct.pack(
        "<HBHbH",  # H=dist, B=energy, H=dist, b=energy, H=det_dist – but we
        mov_dist, mov_energy, stat_dist, stat_energy, det_dist,
    )


# ---------------------------------------------------------------------------
# Parser tests
# ---------------------------------------------------------------------------
class TestLD2410DParser:

    def test_parse_no_target(self):
        payload = bytes([0x02, 0xAA, 0x00]) + b"\x00" * 8
        frame = build_data_frame(payload)
        result = LD2410DParser.parse_data_frame(frame)
        assert result is not None
        assert result.state == TargetState.NO_TARGET
        assert result.has_target is False

    def test_parse_moving_target(self):
        # state=0x01, mov_dist=120cm, mov_energy=55, stat_dist=0, stat_energy=0
        payload = bytes([0x02, 0xAA, 0x01])
        payload += struct.pack("<H", 120)   # moving distance
        payload += bytes([55])              # moving energy
        payload += struct.pack("<H", 0)     # stat distance
        payload += bytes([0])              # stat energy
        payload += struct.pack("<H", 120)   # detection distance

        frame = build_data_frame(payload)
        result = LD2410DParser.parse_data_frame(frame)

        assert result is not None
        assert result.state == TargetState.MOVING
        assert result.has_moving_target is True
        assert result.has_stationary_target is False
        assert result.moving_distance_cm == 120
        assert result.moving_energy == 55

    def test_parse_both_targets(self):
        payload = bytes([0x02, 0xAA, 0x03])
        payload += struct.pack("<H", 100)   # moving distance
        payload += bytes([40])              # moving energy
        payload += struct.pack("<H", 250)   # stat distance
        payload += bytes([70])              # stat energy
        payload += struct.pack("<H", 250)   # detection distance

        frame = build_data_frame(payload)
        result = LD2410DParser.parse_data_frame(frame)

        assert result is not None
        assert result.state == TargetState.MOVING_AND_STAT
        assert result.has_moving_target is True
        assert result.has_stationary_target is True
        assert result.stationary_distance_cm == 250
        assert result.stationary_energy == 70

    def test_parse_malformed_frame(self):
        assert LD2410DParser.parse_data_frame(b"garbage") is None
        assert LD2410DParser.parse_data_frame(DATA_HEADER + b"\x00") is None

    def test_parse_empty_payload(self):
        frame = build_data_frame(b"")
        result = LD2410DParser.parse_data_frame(frame)
        assert result is None

    def test_parse_ack_frame(self):
        # Simulate an ACK for ENABLE_CONFIG (0x00FF → ACK = 0x01FF)
        ack_cmd = struct.pack("<H", 0x01FF)
        status = struct.pack("<H", 0x0000)  # success
        payload = ack_cmd + status
        length = struct.pack("<H", len(payload))
        frame = CMD_HEADER + length + payload + CMD_FOOTER

        result = LD2410DParser.parse_ack_frame(frame)
        assert result is not None
        assert result.success is True
        assert result.command == 0x01FF

    def test_parse_ack_failure(self):
        ack_cmd = struct.pack("<H", 0x01FF)
        status = struct.pack("<H", 0x0001)  # failure
        payload = ack_cmd + status
        length = struct.pack("<H", len(payload))
        frame = CMD_HEADER + length + payload + CMD_FOOTER

        result = LD2410DParser.parse_ack_frame(frame)
        assert result is not None
        assert result.success is False


# ---------------------------------------------------------------------------
# Command builder tests
# ---------------------------------------------------------------------------
class TestLD2410DCommandBuilder:

    def test_enable_config_frame_structure(self):
        frame = LD2410DCommandBuilder.enable_config()
        assert frame.startswith(CMD_HEADER)
        assert frame.endswith(CMD_FOOTER)
        # Command word should be 0x00FF
        inner = frame[4:-4]
        cmd = struct.unpack_from("<H", inner, 2)[0]
        assert cmd == CommandWord.ENABLE_CONFIG

    def test_end_config_frame(self):
        frame = LD2410DCommandBuilder.end_config()
        assert frame.startswith(CMD_HEADER)
        assert frame.endswith(CMD_FOOTER)

    def test_set_max_distance(self):
        frame = LD2410DCommandBuilder.set_max_distance(6, 6, 5)
        assert frame.startswith(CMD_HEADER)
        assert frame.endswith(CMD_FOOTER)
        inner = frame[4:-4]
        cmd = struct.unpack_from("<H", inner, 2)[0]
        assert cmd == CommandWord.SET_MAX_DISTANCE

    def test_set_gate_sensitivity(self):
        frame = LD2410DCommandBuilder.set_gate_sensitivity(3, 50, 40)
        assert frame.startswith(CMD_HEADER)
        assert frame.endswith(CMD_FOOTER)
        inner = frame[4:-4]
        cmd = struct.unpack_from("<H", inner, 2)[0]
        assert cmd == CommandWord.SET_SENSITIVITY

    def test_factory_reset(self):
        frame = LD2410DCommandBuilder.factory_reset()
        assert frame.startswith(CMD_HEADER)
        assert frame.endswith(CMD_FOOTER)

    def test_restart(self):
        frame = LD2410DCommandBuilder.restart()
        assert frame.startswith(CMD_HEADER)
        assert frame.endswith(CMD_FOOTER)
