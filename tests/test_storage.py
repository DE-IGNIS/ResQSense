"""
Tests for SQLite storage manager.
"""

import os
import tempfile
import time
import pytest

from src.database.storage import StorageManager, SearchZone


@pytest.fixture
def db():
    """Create a temporary database for each test."""
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    mgr = StorageManager(db_path=path)
    mgr.initialize()
    yield mgr
    mgr.close()
    os.unlink(path)


class TestStorageManager:

    # ---- zones ------------------------------------------------------------

    def test_create_and_get_zone(self, db: StorageManager):
        zone = SearchZone(name="Site Alpha", description="Collapsed building")
        zone_id = db.create_zone(zone)
        assert zone_id > 0

        fetched = db.get_zone(zone_id)
        assert fetched is not None
        assert fetched.name == "Site Alpha"
        assert fetched.status == "active"

    def test_list_zones(self, db: StorageManager):
        db.create_zone(SearchZone(name="A"))
        db.create_zone(SearchZone(name="B"))
        zones = db.list_zones()
        assert len(zones) == 2

    def test_list_zones_by_status(self, db: StorageManager):
        z1 = db.create_zone(SearchZone(name="Active"))
        db.create_zone(SearchZone(name="Done"))
        db.update_zone_status(z1, "completed")

        active = db.list_zones(status="active")
        assert len(active) == 1
        assert active[0].name == "Done"

    def test_delete_zone_cascades(self, db: StorageManager):
        z_id = db.create_zone(SearchZone(name="Delete Me"))
        db.insert_sensor_reading("ld2410d", {"test": 1}, zone_id=z_id)
        db.insert_alert("WARNING", "PROBABLE", "test", zone_id=z_id)

        db.delete_zone(z_id)
        assert db.get_zone(z_id) is None
        assert db.get_readings(zone_id=z_id) == []
        assert db.get_alerts(zone_id=z_id) == []

    # ---- sensor readings --------------------------------------------------

    def test_insert_and_query_readings(self, db: StorageManager):
        db.insert_sensor_reading("ld2410d", {"energy": 50})
        db.insert_sensor_reading("max30100", {"hr": 72})

        all_r = db.get_readings(limit=10)
        assert len(all_r) == 2

        radar = db.get_readings(source="ld2410d")
        assert len(radar) == 1
        assert radar[0]["data"]["energy"] == 50

    def test_readings_since(self, db: StorageManager):
        old_ts = time.time() - 100
        new_ts = time.time()
        db.insert_sensor_reading("ld2410d", {"v": 1}, timestamp=old_ts)
        db.insert_sensor_reading("ld2410d", {"v": 2}, timestamp=new_ts)

        recent = db.get_readings(since=time.time() - 10)
        assert len(recent) == 1
        assert recent[0]["data"]["v"] == 2

    # ---- alerts -----------------------------------------------------------

    def test_insert_and_query_alerts(self, db: StorageManager):
        db.insert_alert("CRITICAL", "CONFIRMED", "Survivor found!")
        db.insert_alert("INFO", "EXPERIMENTAL", "Possible detection")

        all_a = db.get_alerts()
        assert len(all_a) == 2

        crits = db.get_alerts(severity="CRITICAL")
        assert len(crits) == 1
        assert crits[0]["message"] == "Survivor found!"

    # ---- events -----------------------------------------------------------

    def test_log_and_query_events(self, db: StorageManager):
        db.log_event("system", "Started")
        db.log_event("zone", "Created zone A", {"zone_id": 1})

        events = db.get_events()
        assert len(events) == 2

        sys_events = db.get_events(event_type="system")
        assert len(sys_events) == 1

    # ---- maintenance ------------------------------------------------------

    def test_get_stats(self, db: StorageManager):
        stats = db.get_stats()
        assert "search_zones" in stats
        assert "sensor_readings" in stats

    def test_prune_readings(self, db: StorageManager):
        old_ts = time.time() - 86400 * 10  # 10 days ago
        db.insert_sensor_reading("ld2410d", {"old": True}, timestamp=old_ts)
        db.insert_sensor_reading("ld2410d", {"new": True})

        deleted = db.prune_readings(older_than_s=86400 * 7)
        assert deleted == 1
        assert len(db.get_readings()) == 1
