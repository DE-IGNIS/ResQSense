"""
ResQSense – hardware subpackage.

Sensor communication modules for ESP32-C6, HLK-LD2410D, and MAX30100.
"""

from src.hardware.esp32 import ESP32Connection, Channel, ConnectionStatus
from src.hardware.ld2410d import (
    LD2410DParser,
    LD2410DCommandBuilder,
    TargetData,
    TargetState,
    SensorConfig,
)
from src.hardware.max30100 import (
    MAX30100Parser,
    MAX30100Processor,
    VitalSigns,
    RawPPGSample,
)

__all__ = [
    "ESP32Connection",
    "Channel",
    "ConnectionStatus",
    "LD2410DParser",
    "LD2410DCommandBuilder",
    "TargetData",
    "TargetState",
    "SensorConfig",
    "MAX30100Parser",
    "MAX30100Processor",
    "VitalSigns",
    "RawPPGSample",
]
