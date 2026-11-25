"""
Integration test: BackendService wiring (offline mode, no hardware).
"""

import os
import tempfile
import time
import pytest

from src.core.backend_service import BackendService
from src.core.detection import Alert
from src.hardware.ld2410d import TargetData, TargetState
from src.hardware.max30100 import VitalSigns


@pytest.fixture
def svc():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    service = BackendService(db_path=path)
    service.start(port=None)  # offline mode
    yield service
    service.stop()
    os.unlink(path)


class TestBackendService:

    def test_start_offline(self, svc: BackendService):
        assert svc.is_running is True

    def test_create_and_list_zones(self, svc: BackendService):
        z_id = svc.create_zone("Test Zone", description="Unit test")
        assert z_id > 0
        zones = svc.list_zones()
        assert any(z["name"] == "Test Zone" for z in zones)

    def test_detection_state_initial(self, svc: BackendService):
        state = svc.get_detection_state()
        assert state.current_level.name == "NO_SIGNAL"

    def test_get_last_vitals(self, svc: BackendService):
        vs = svc.get_last_vitals()
        assert isinstance(vs, VitalSigns)

    def test_list_serial_ports(self, svc: BackendService):
        ports = svc.list_serial_ports()
        assert isinstance(ports, list)

    def test_db_stats(self, svc: BackendService):
        stats = svc.get_db_stats()
        assert "search_zones" in stats

    def test_complete_zone(self, svc: BackendService):
        z_id = svc.create_zone("Done Zone")
        svc.complete_zone(z_id)
        zones = svc.list_zones(status="completed")
        assert any(z["id"] == z_id for z in zones)

    def test_alert_history_empty(self, svc: BackendService):
        history = svc.get_alert_history()
        assert isinstance(history, list)
        assert len(history) == 0
