"""
SQLite Database Management
===========================

Provides persistent storage for:
  - Search zones (areas being scanned).
  - Sensor readings (time-series data for replay / analytics).
  - Detection alerts.
  - System events / audit log.

Design decisions:
  - Uses Python's built-in ``sqlite3`` module (no ORM overhead).
  - Thread-safe via ``check_same_thread=False`` and a module-level lock.
  - WAL journal mode for concurrent read/write performance.
  - Automatic schema migration via a ``schema_version`` pragma.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Generator, Optional

logger = logging.getLogger(__name__)

SCHEMA_VERSION = 1
DEFAULT_DB_PATH = "data/resqsense.db"


# ---------------------------------------------------------------------------
# Data models (DB rows)
# ---------------------------------------------------------------------------
@dataclass
class SearchZone:
    """A named rescue search zone."""
    id: Optional[int] = None
    name: str = ""
    description: str = ""
    latitude: Optional[float] = None
    longitude: Optional[float] = None
    status: str = "active"           # active | completed | abandoned
    created_at: float = 0.0
    updated_at: float = 0.0

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "name": self.name,
            "description": self.description,
            "latitude": self.latitude,
            "longitude": self.longitude,
            "status": self.status,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }


@dataclass
class SensorRecord:
    """One sensor reading persisted for historical analysis."""
    id: Optional[int] = None
    zone_id: Optional[int] = None
    source: str = ""                  # "ld2410d" | "max30100"
    timestamp: float = 0.0
    data_json: str = "{}"             # serialised reading


@dataclass
class AlertRecord:
    """Persisted alert."""
    id: Optional[int] = None
    zone_id: Optional[int] = None
    timestamp: float = 0.0
    severity: str = ""
    level: str = ""
    message: str = ""
    radar_json: str = "{}"
    vitals_json: str = "{}"


# ---------------------------------------------------------------------------
# Storage Manager
# ---------------------------------------------------------------------------
class StorageManager:
    """
    Thread-safe SQLite wrapper for ResQSense.

    Usage
    -----
    >>> db = StorageManager("data/resqsense.db")
    >>> db.initialize()
    >>> zone_id = db.create_zone(SearchZone(name="Site Alpha"))
    >>> db.insert_sensor_reading(...)
    >>> db.close()
    """

    def __init__(self, db_path: str = DEFAULT_DB_PATH):
        self._db_path = db_path
        self._conn: Optional[sqlite3.Connection] = None
        self._lock = threading.Lock()

    # ---- lifecycle --------------------------------------------------------

    def initialize(self) -> None:
        """Open the database and ensure the schema is up to date."""
        path = Path(self._db_path)
        path.parent.mkdir(parents=True, exist_ok=True)

        self._conn = sqlite3.connect(
            str(path),
            check_same_thread=False,
            detect_types=sqlite3.PARSE_DECLTYPES,
        )
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")

        self._create_schema()
        logger.info("Database initialised at %s", self._db_path)

    def close(self) -> None:
        if self._conn:
            self._conn.close()
            self._conn = None

    @contextmanager
    def _cursor(self) -> Generator[sqlite3.Cursor, None, None]:
        """Context manager that provides a cursor with automatic commit/rollback."""
        assert self._conn is not None, "Database not initialised"
        with self._lock:
            cur = self._conn.cursor()
            try:
                yield cur
                self._conn.commit()
            except Exception:
                self._conn.rollback()
                raise

    # ---- schema -----------------------------------------------------------

    def _create_schema(self) -> None:
        assert self._conn is not None
        with self._cursor() as cur:
            cur.executescript("""
                CREATE TABLE IF NOT EXISTS search_zones (
                    id          INTEGER PRIMARY KEY AUTOINCREMENT,
                    name        TEXT NOT NULL,
                    description TEXT DEFAULT '',
                    latitude    REAL,
                    longitude   REAL,
                    status      TEXT DEFAULT 'active',
                    created_at  REAL NOT NULL,
                    updated_at  REAL NOT NULL
                );

                CREATE TABLE IF NOT EXISTS sensor_readings (
                    id          INTEGER PRIMARY KEY AUTOINCREMENT,
                    zone_id     INTEGER REFERENCES search_zones(id),
                    source      TEXT NOT NULL,
                    timestamp   REAL NOT NULL,
                    data_json   TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS alerts (
                    id          INTEGER PRIMARY KEY AUTOINCREMENT,
                    zone_id     INTEGER REFERENCES search_zones(id),
                    timestamp   REAL NOT NULL,
                    severity    TEXT NOT NULL,
                    level       TEXT NOT NULL,
                    message     TEXT NOT NULL,
                    radar_json  TEXT DEFAULT '{}',
                    vitals_json TEXT DEFAULT '{}'
                );

                CREATE TABLE IF NOT EXISTS system_events (
                    id          INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp   REAL NOT NULL,
                    event_type  TEXT NOT NULL,
                    message     TEXT NOT NULL,
                    data_json   TEXT DEFAULT '{}'
                );

                CREATE INDEX IF NOT EXISTS idx_readings_zone
                    ON sensor_readings(zone_id, timestamp);
                CREATE INDEX IF NOT EXISTS idx_readings_source
                    ON sensor_readings(source, timestamp);
                CREATE INDEX IF NOT EXISTS idx_alerts_zone
                    ON alerts(zone_id, timestamp);
                CREATE INDEX IF NOT EXISTS idx_alerts_severity
                    ON alerts(severity);
                CREATE INDEX IF NOT EXISTS idx_events_type
                    ON system_events(event_type, timestamp);
            """)

    # ---- zones ------------------------------------------------------------

    def create_zone(self, zone: SearchZone) -> int:
        """Insert a new search zone. Returns the new row ID."""
        now = time.time()
        with self._cursor() as cur:
            cur.execute(
                """INSERT INTO search_zones
                   (name, description, latitude, longitude, status, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (
                    zone.name,
                    zone.description,
                    zone.latitude,
                    zone.longitude,
                    zone.status,
                    zone.created_at or now,
                    now,
                ),
            )
            return cur.lastrowid  # type: ignore[return-value]

    def get_zone(self, zone_id: int) -> Optional[SearchZone]:
        with self._cursor() as cur:
            cur.execute("SELECT * FROM search_zones WHERE id = ?", (zone_id,))
            row = cur.fetchone()
            return self._row_to_zone(row) if row else None

    def list_zones(self, status: Optional[str] = None) -> list[SearchZone]:
        with self._cursor() as cur:
            if status:
                cur.execute(
                    "SELECT * FROM search_zones WHERE status = ? ORDER BY created_at DESC",
                    (status,),
                )
            else:
                cur.execute("SELECT * FROM search_zones ORDER BY created_at DESC")
            return [self._row_to_zone(r) for r in cur.fetchall()]

    def update_zone_status(self, zone_id: int, status: str) -> None:
        with self._cursor() as cur:
            cur.execute(
                "UPDATE search_zones SET status = ?, updated_at = ? WHERE id = ?",
                (status, time.time(), zone_id),
            )

    def delete_zone(self, zone_id: int) -> None:
        with self._cursor() as cur:
            cur.execute("DELETE FROM sensor_readings WHERE zone_id = ?", (zone_id,))
            cur.execute("DELETE FROM alerts WHERE zone_id = ?", (zone_id,))
            cur.execute("DELETE FROM search_zones WHERE id = ?", (zone_id,))

    @staticmethod
    def _row_to_zone(row: sqlite3.Row) -> SearchZone:
        return SearchZone(
            id=row["id"],
            name=row["name"],
            description=row["description"],
            latitude=row["latitude"],
            longitude=row["longitude"],
            status=row["status"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    # ---- sensor readings --------------------------------------------------

    def insert_sensor_reading(
        self,
        source: str,
        data: dict,
        zone_id: Optional[int] = None,
        timestamp: Optional[float] = None,
    ) -> int:
        ts = timestamp or time.time()
        with self._cursor() as cur:
            cur.execute(
                """INSERT INTO sensor_readings (zone_id, source, timestamp, data_json)
                   VALUES (?, ?, ?, ?)""",
                (zone_id, source, ts, json.dumps(data)),
            )
            return cur.lastrowid  # type: ignore[return-value]

    def get_readings(
        self,
        source: Optional[str] = None,
        zone_id: Optional[int] = None,
        since: Optional[float] = None,
        limit: int = 100,
    ) -> list[dict]:
        clauses: list[str] = []
        params: list[Any] = []

        if source:
            clauses.append("source = ?")
            params.append(source)
        if zone_id is not None:
            clauses.append("zone_id = ?")
            params.append(zone_id)
        if since is not None:
            clauses.append("timestamp >= ?")
            params.append(since)

        where = " AND ".join(clauses)
        sql = "SELECT * FROM sensor_readings"
        if where:
            sql += f" WHERE {where}"
        sql += " ORDER BY timestamp DESC LIMIT ?"
        params.append(limit)

        with self._cursor() as cur:
            cur.execute(sql, params)
            return [
                {
                    "id": r["id"],
                    "zone_id": r["zone_id"],
                    "source": r["source"],
                    "timestamp": r["timestamp"],
                    "data": json.loads(r["data_json"]),
                }
                for r in cur.fetchall()
            ]

    # ---- alerts -----------------------------------------------------------

    def insert_alert(
        self,
        severity: str,
        level: str,
        message: str,
        zone_id: Optional[int] = None,
        timestamp: Optional[float] = None,
        radar_data: Optional[dict] = None,
        vitals_data: Optional[dict] = None,
    ) -> int:
        ts = timestamp or time.time()
        with self._cursor() as cur:
            cur.execute(
                """INSERT INTO alerts
                   (zone_id, timestamp, severity, level, message, radar_json, vitals_json)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (
                    zone_id,
                    ts,
                    severity,
                    level,
                    message,
                    json.dumps(radar_data or {}),
                    json.dumps(vitals_data or {}),
                ),
            )
            return cur.lastrowid  # type: ignore[return-value]

    def get_alerts(
        self,
        zone_id: Optional[int] = None,
        severity: Optional[str] = None,
        since: Optional[float] = None,
        limit: int = 50,
    ) -> list[dict]:
        clauses: list[str] = []
        params: list[Any] = []

        if zone_id is not None:
            clauses.append("zone_id = ?")
            params.append(zone_id)
        if severity:
            clauses.append("severity = ?")
            params.append(severity)
        if since is not None:
            clauses.append("timestamp >= ?")
            params.append(since)

        where = " AND ".join(clauses)
        sql = "SELECT * FROM alerts"
        if where:
            sql += f" WHERE {where}"
        sql += " ORDER BY timestamp DESC LIMIT ?"
        params.append(limit)

        with self._cursor() as cur:
            cur.execute(sql, params)
            return [
                {
                    "id": r["id"],
                    "zone_id": r["zone_id"],
                    "timestamp": r["timestamp"],
                    "severity": r["severity"],
                    "level": r["level"],
                    "message": r["message"],
                    "radar_data": json.loads(r["radar_json"]),
                    "vitals_data": json.loads(r["vitals_json"]),
                }
                for r in cur.fetchall()
            ]

    # ---- system events ----------------------------------------------------

    def log_event(
        self,
        event_type: str,
        message: str,
        data: Optional[dict] = None,
    ) -> None:
        with self._cursor() as cur:
            cur.execute(
                """INSERT INTO system_events (timestamp, event_type, message, data_json)
                   VALUES (?, ?, ?, ?)""",
                (time.time(), event_type, message, json.dumps(data or {})),
            )

    def get_events(
        self,
        event_type: Optional[str] = None,
        limit: int = 50,
    ) -> list[dict]:
        if event_type:
            sql = "SELECT * FROM system_events WHERE event_type = ? ORDER BY timestamp DESC LIMIT ?"
            params: list[Any] = [event_type, limit]
        else:
            sql = "SELECT * FROM system_events ORDER BY timestamp DESC LIMIT ?"
            params = [limit]

        with self._cursor() as cur:
            cur.execute(sql, params)
            return [
                {
                    "id": r["id"],
                    "timestamp": r["timestamp"],
                    "event_type": r["event_type"],
                    "message": r["message"],
                    "data": json.loads(r["data_json"]),
                }
                for r in cur.fetchall()
            ]

    # ---- maintenance ------------------------------------------------------

    def get_stats(self) -> dict:
        """Return row counts for each table."""
        with self._cursor() as cur:
            stats = {}
            for table in ("search_zones", "sensor_readings", "alerts", "system_events"):
                cur.execute(f"SELECT COUNT(*) FROM {table}")  # noqa: S608
                stats[table] = cur.fetchone()[0]
            return stats

    def prune_readings(self, older_than_s: float = 86400 * 7) -> int:
        """Delete sensor readings older than *older_than_s* seconds. Returns rows deleted."""
        cutoff = time.time() - older_than_s
        with self._cursor() as cur:
            cur.execute("DELETE FROM sensor_readings WHERE timestamp < ?", (cutoff,))
            return cur.rowcount
