"""
ResQSense Backend Service
==========================

The single entry-point for the Flet frontend (or any other consumer).

This module wires together all backend components and exposes a clean,
high-level Python API that the UI developer can call without knowing
anything about serial protocols, sensor parsing, or threading.

Design goals
------------
- **Thread-safe** – every public method can be called from any thread.
- **Callback-driven** – the UI registers callbacks for live data and
  alerts; the backend pushes data as it arrives.
- **No Flet dependency** – this module has zero knowledge of the UI
  framework.

Typical integration (by the UI developer)
------------------------------------------
>>> from src.core.backend_service import BackendService
>>>
>>> svc = BackendService()
>>>
>>> # Register callbacks
>>> svc.on_radar_data(update_radar_widget)
>>> svc.on_vitals_data(update_vitals_widget)
>>> svc.on_alert(show_alert_banner)
>>> svc.on_connection_status(update_status_bar)
>>>
>>> # Start
>>> svc.start(port="/dev/ttyUSB0")   # or svc.start()  to auto-detect
>>>
>>> # Query helpers (called on demand by the UI)
>>> state   = svc.get_detection_state()
>>> zones   = svc.list_zones()
>>> history = svc.get_alert_history(limit=20)
>>>
>>> # Shutdown
>>> svc.stop()
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Callable, Optional

from src.hardware.esp32 import ESP32Connection, Channel, ConnectionStatus
from src.hardware.ld2410d import (
    LD2410DParser,
    LD2410DCommandBuilder,
    TargetData,
    SensorConfig,
)
from src.hardware.max30100 import MAX30100Parser, MAX30100Processor, VitalSigns
from src.core.detection import (
    DetectionEngine,
    DetectionConfig,
    DetectionState,
    PresenceLevel,
    Alert,
)
from src.core.sensor_processor import (
    SensorProcessingPipeline,
    SensorReading,
)
from src.core.alerts import AlertManager
from src.database.storage import StorageManager, SearchZone

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Backend Service
# ---------------------------------------------------------------------------
class BackendService:
    """
    Façade that exposes every backend capability to the frontend.

    All public methods are thread-safe.
    """

    def __init__(
        self,
        db_path: str = "data/resqsense.db",
        detection_config: Optional[DetectionConfig] = None,
    ):
        # ---- components ---------------------------------------------------
        self._esp32 = ESP32Connection()
        self._ld_parser = LD2410DParser()
        self._ld_cmd = LD2410DCommandBuilder()
        self._max_parser = MAX30100Parser()
        self._max_processor = MAX30100Processor()
        self._pipeline = SensorProcessingPipeline()
        self._detection = DetectionEngine(config=detection_config)
        self._alert_mgr = AlertManager()
        self._db = StorageManager(db_path)

        # ---- callback registries ------------------------------------------
        self._radar_callbacks: list[Callable[[TargetData], None]] = []
        self._vitals_callbacks: list[Callable[[VitalSigns], None]] = []
        self._alert_callbacks: list[Callable[[Alert], None]] = []
        self._status_callbacks: list[Callable[[ConnectionStatus], None]] = []

        # ---- state --------------------------------------------------------
        self._running = False
        self._active_zone_id: Optional[int] = None
        self._status_thread: Optional[threading.Thread] = None

        # ---- internal wiring -----------------------------------------------
        self._detection.on_alert(self._alert_mgr.handle_alert)
        self._alert_mgr.subscribe(self._fan_out_alert)

    # =====================================================================
    # Lifecycle
    # =====================================================================

    def start(self, port: Optional[str] = None) -> None:
        """
        Initialise the database, connect to the ESP32, and begin
        processing sensor data.

        Parameters
        ----------
        port : str or None
            Serial port path (e.g. ``"/dev/ttyUSB0"``).  If ``None``,
            auto-detection is attempted.
        """
        # DB
        self._db.initialize()
        self._db.log_event("system", "Backend service starting")
        self._alert_mgr.set_db_writer(self._persist_alert)

        # ESP32
        if port is None:
            port = ESP32Connection.find_esp32_port()
            if port is None:
                logger.warning(
                    "No ESP32 detected – starting in offline / simulation mode"
                )
                self._running = True
                self._db.log_event("system", "Started in offline mode (no ESP32)")
                return

        self._esp32.on_frame(Channel.LD2410D, self._handle_ld2410d_frame)
        self._esp32.on_frame(Channel.MAX30100, self._handle_max30100_frame)
        self._esp32.on_frame(Channel.STATUS, self._handle_status_frame)
        self._esp32.connect(port)
        self._running = True

        # Periodic status push
        self._status_thread = threading.Thread(
            target=self._status_loop, daemon=True, name="status-push"
        )
        self._status_thread.start()

        self._db.log_event("system", f"Connected to ESP32 on {port}")
        logger.info("BackendService started on %s", port)

    def stop(self) -> None:
        """Cleanly shut down all backend components."""
        self._running = False
        self._esp32.disconnect()
        self._db.log_event("system", "Backend service stopped")
        self._db.close()
        logger.info("BackendService stopped.")

    @property
    def is_running(self) -> bool:
        return self._running

    # =====================================================================
    # Callback registration  (for the UI)
    # =====================================================================

    def on_radar_data(self, callback: Callable[[TargetData], None]) -> None:
        """Subscribe to live LD2410D radar readings."""
        self._radar_callbacks.append(callback)

    def on_vitals_data(self, callback: Callable[[VitalSigns], None]) -> None:
        """Subscribe to live MAX30100 vital signs."""
        self._vitals_callbacks.append(callback)

    def on_alert(self, callback: Callable[[Alert], None]) -> None:
        """Subscribe to detection alerts."""
        self._alert_callbacks.append(callback)

    def on_connection_status(self, callback: Callable[[ConnectionStatus], None]) -> None:
        """Subscribe to ESP32 connection status changes."""
        self._status_callbacks.append(callback)

    # =====================================================================
    # Queries  (pull-based, for the UI)
    # =====================================================================

    def get_detection_state(self) -> DetectionState:
        """Current detection engine state."""
        return self._detection.get_state()

    def get_connection_status(self) -> ConnectionStatus:
        return self._esp32.get_status()

    def get_last_vitals(self) -> VitalSigns:
        return self._max_processor.last_vitals

    # ---- zones ------------------------------------------------------------

    def create_zone(self, name: str, description: str = "",
                    lat: Optional[float] = None, lon: Optional[float] = None) -> int:
        zone = SearchZone(name=name, description=description,
                          latitude=lat, longitude=lon)
        zone_id = self._db.create_zone(zone)
        self._db.log_event("zone", f"Created zone '{name}'", {"zone_id": zone_id})
        return zone_id

    def set_active_zone(self, zone_id: int) -> None:
        self._active_zone_id = zone_id
        self._detection.set_zone(str(zone_id))

    def list_zones(self, status: Optional[str] = None) -> list[dict]:
        return [z.to_dict() for z in self._db.list_zones(status)]

    def complete_zone(self, zone_id: int) -> None:
        self._db.update_zone_status(zone_id, "completed")
        self._db.log_event("zone", f"Zone {zone_id} marked completed")

    def delete_zone(self, zone_id: int) -> None:
        self._db.delete_zone(zone_id)

    # ---- history ----------------------------------------------------------

    def get_alert_history(self, limit: int = 20) -> list[Alert]:
        return self._alert_mgr.get_recent(limit)

    def get_alert_history_from_db(
        self,
        zone_id: Optional[int] = None,
        severity: Optional[str] = None,
        since: Optional[float] = None,
        limit: int = 50,
    ) -> list[dict]:
        return self._db.get_alerts(zone_id=zone_id, severity=severity,
                                   since=since, limit=limit)

    def get_sensor_history(
        self,
        source: Optional[str] = None,
        zone_id: Optional[int] = None,
        since: Optional[float] = None,
        limit: int = 100,
    ) -> list[dict]:
        return self._db.get_readings(source=source, zone_id=zone_id,
                                     since=since, limit=limit)

    def get_system_events(self, limit: int = 50) -> list[dict]:
        return self._db.get_events(limit=limit)

    def get_db_stats(self) -> dict:
        return self._db.get_stats()

    # ---- sensor commands --------------------------------------------------

    def list_serial_ports(self) -> list[dict]:
        return ESP32Connection.list_ports()

    def send_radar_command(self, command_bytes: bytes) -> bool:
        """Send a raw LD2410D command via the ESP32."""
        return self._esp32.send_command(Channel.LD2410D, command_bytes)

    def enable_engineering_mode(self) -> bool:
        cmd = self._ld_cmd.enable_config()
        cmd += self._ld_cmd.enable_engineering_mode()
        cmd += self._ld_cmd.end_config()
        return self._esp32.send_raw(cmd)

    def disable_engineering_mode(self) -> bool:
        cmd = self._ld_cmd.enable_config()
        cmd += self._ld_cmd.disable_engineering_mode()
        cmd += self._ld_cmd.end_config()
        return self._esp32.send_raw(cmd)

    def set_radar_sensitivity(
        self, gate: int, moving: int, stationary: int
    ) -> bool:
        cmd = self._ld_cmd.enable_config()
        cmd += self._ld_cmd.set_gate_sensitivity(gate, moving, stationary)
        cmd += self._ld_cmd.end_config()
        return self._esp32.send_raw(cmd)

    def restart_radar(self) -> bool:
        cmd = self._ld_cmd.enable_config()
        cmd += self._ld_cmd.restart()
        cmd += self._ld_cmd.end_config()
        return self._esp32.send_raw(cmd)

    # =====================================================================
    # Internal handlers (wired to ESP32Connection callbacks)
    # =====================================================================

    def _handle_ld2410d_frame(self, raw: bytes) -> None:
        """Parse an LD2410D frame, validate, detect, persist, push."""
        target = self._ld_parser.parse_data_frame(raw)
        if target is None:
            return

        # Validate key fields through the pipeline
        for fname, val in [
            ("moving_energy", target.moving_energy),
            ("stationary_energy", target.stationary_energy),
            ("moving_distance_cm", target.moving_distance_cm),
            ("stationary_distance_cm", target.stationary_distance_cm),
        ]:
            reading = SensorReading(
                source="ld2410d",
                field_name=fname,
                raw_value=float(val),
                timestamp=target.timestamp,
            )
            self._pipeline.process(reading)

        # Feed detection engine
        self._detection.update_radar(target)

        # Persist (down-sampled: every reading)
        self._db.insert_sensor_reading(
            source="ld2410d",
            data=target.to_dict(),
            zone_id=self._active_zone_id,
            timestamp=target.timestamp,
        )

        # Push to UI
        for cb in self._radar_callbacks:
            try:
                cb(target)
            except Exception:
                logger.exception("Radar callback error")

    def _handle_max30100_frame(self, raw: bytes) -> None:
        """Parse MAX30100 JSON, compute vitals, detect, persist, push."""
        samples = self._max_parser.parse_line(raw)
        if not samples:
            return

        self._max_processor.add_samples(samples)
        vitals = self._max_processor.compute_vitals()

        # Validate
        for fname, val in [
            ("heart_rate_bpm", vitals.heart_rate_bpm),
            ("spo2_percent", vitals.spo2_percent),
        ]:
            reading = SensorReading(
                source="max30100",
                field_name=fname,
                raw_value=val,
                timestamp=vitals.timestamp,
            )
            self._pipeline.process(reading)

        # Feed detection engine
        self._detection.update_vitals(vitals)

        # Persist
        self._db.insert_sensor_reading(
            source="max30100",
            data=vitals.to_dict(),
            zone_id=self._active_zone_id,
            timestamp=vitals.timestamp,
        )

        # Push to UI
        for cb in self._vitals_callbacks:
            try:
                cb(vitals)
            except Exception:
                logger.exception("Vitals callback error")

    def _handle_status_frame(self, raw: bytes) -> None:
        """Handle ESP32 heartbeat / status messages."""
        try:
            text = raw.decode("utf-8", errors="replace").strip()
            logger.debug("ESP32 status: %s", text)
        except Exception:
            pass

    def _fan_out_alert(self, alert: Alert) -> None:
        """Push an alert to all registered UI callbacks."""
        for cb in self._alert_callbacks:
            try:
                cb(alert)
            except Exception:
                logger.exception("Alert UI callback error")

    def _persist_alert(self, alert: Alert) -> None:
        """Write an alert to the database."""
        zone_int = None
        if alert.zone_id is not None:
            try:
                zone_int = int(alert.zone_id)
            except (ValueError, TypeError):
                pass

        self._db.insert_alert(
            severity=alert.severity.name,
            level=alert.level.name,
            message=alert.message,
            zone_id=zone_int,
            timestamp=alert.timestamp,
            radar_data=alert.radar_data,
            vitals_data=alert.vitals_data,
        )

    def _status_loop(self) -> None:
        """Periodically push connection status to subscribers."""
        while self._running:
            status = self._esp32.get_status()
            for cb in self._status_callbacks:
                try:
                    cb(status)
                except Exception:
                    logger.exception("Status callback error")
            time.sleep(2.0)
