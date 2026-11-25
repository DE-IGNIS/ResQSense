#!/usr/bin/env python3
"""
ResQSense – Application Entry Point
=====================================

This file is owned by the UI developer (Himanshu).
The backend provides a minimal bootstrap example here;
the UI developer will replace this with the Flet application.

Usage (backend-only smoke test):
    python main.py [--port /dev/ttyUSB0]
"""

import argparse
import logging
import signal
import sys
import time

# Ensure the project root is on sys.path
sys.path.insert(0, ".")

from src.core.backend_service import BackendService
from src.hardware.ld2410d import TargetData
from src.hardware.max30100 import VitalSigns
from src.core.detection import Alert

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("resqsense")


def main() -> None:
    parser = argparse.ArgumentParser(description="ResQSense backend smoke test")
    parser.add_argument(
        "--port", type=str, default=None,
        help="Serial port for ESP32 (auto-detect if omitted)",
    )
    parser.add_argument(
        "--db", type=str, default="data/resqsense.db",
        help="SQLite database path",
    )
    args = parser.parse_args()

    svc = BackendService(db_path=args.db)

    # --- example callbacks (replace with Flet UI bindings) -----------------
    def on_radar(data: TargetData) -> None:
        if data.has_target:
            logger.info(
                "Radar: %s  mov=%dcm@%d%%  stat=%dcm@%d%%",
                data.state.name,
                data.moving_distance_cm, data.moving_energy,
                data.stationary_distance_cm, data.stationary_energy,
            )

    def on_vitals(vs: VitalSigns) -> None:
        if vs.is_finger_present:
            logger.info(
                "Vitals: HR=%.1f bpm  SpO2=%.1f%%  quality=%.2f [%s]",
                vs.heart_rate_bpm, vs.spo2_percent,
                vs.signal_quality, vs.confidence,
            )

    def on_alert(alert: Alert) -> None:
        logger.warning("⚠ ALERT [%s]: %s", alert.severity.name, alert.message)

    svc.on_radar_data(on_radar)
    svc.on_vitals_data(on_vitals)
    svc.on_alert(on_alert)

    # --- start -------------------------------------------------------------
    svc.start(port=args.port)

    # --- graceful shutdown -------------------------------------------------
    def shutdown(sig, frame):
        logger.info("Shutting down…")
        svc.stop()
        sys.exit(0)

    signal.signal(signal.SIGINT, shutdown)
    signal.signal(signal.SIGTERM, shutdown)

    logger.info("ResQSense backend running. Press Ctrl+C to stop.")
    while svc.is_running:
        time.sleep(1)


if __name__ == "__main__":
    main()
