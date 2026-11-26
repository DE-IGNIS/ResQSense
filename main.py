#!/usr/bin/env python3
"""ResQSense desktop application entry point."""

from __future__ import annotations

import argparse
import logging

import flet as ft

from src.core.backend_service import BackendService
from src.ui.app import build_app


def main() -> None:
    parser = argparse.ArgumentParser(description="ResQSense rescue operations console")
    parser.add_argument("--port", default=None, help="ESP32 serial port (auto-detect by default)")
    parser.add_argument("--db", default="data/resqsense.db", help="SQLite database path")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    service = BackendService(db_path=args.db)
    # Flet 1.x uses ft.run(main); ft.app is now a module, not a callable.
    ft.run(lambda page: build_app(page, service, port=args.port))


if __name__ == "__main__":
    main()
