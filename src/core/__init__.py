"""
ResQSense – core subpackage.

Detection logic, alert management, sensor processing, and backend service.
"""

from src.core.detection import (
    DetectionEngine,
    DetectionConfig,
    DetectionState,
    PresenceLevel,
    Alert,
    AlertSeverity,
)
from src.core.sensor_processor import (
    SensorProcessingPipeline,
    SensorValidator,
    SensorReading,
    ValidationStatus,
)
from src.core.alerts import AlertManager
from src.core.backend_service import BackendService

__all__ = [
    "DetectionEngine",
    "DetectionConfig",
    "DetectionState",
    "PresenceLevel",
    "Alert",
    "AlertSeverity",
    "SensorProcessingPipeline",
    "SensorValidator",
    "SensorReading",
    "ValidationStatus",
    "AlertManager",
    "BackendService",
]
