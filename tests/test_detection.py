"""
Tests for detection engine and alert manager.
"""

import time
import pytest

from src.hardware.ld2410d import TargetData, TargetState
from src.hardware.max30100 import VitalSigns
from src.core.detection import (
    DetectionEngine,
    DetectionConfig,
    PresenceLevel,
    Alert,
    AlertSeverity,
)
from src.core.alerts import AlertManager


# ---------------------------------------------------------------------------
# Detection Engine
# ---------------------------------------------------------------------------
class TestDetectionEngine:

    def _make_target(self, state=TargetState.MOVING, mov_energy=50, stat_energy=30, dist=100):
        return TargetData(
            timestamp=time.time(),
            state=state,
            moving_distance_cm=dist,
            moving_energy=mov_energy,
            stationary_distance_cm=dist,
            stationary_energy=stat_energy,
        )

    def test_no_target_stays_no_signal(self):
        engine = DetectionEngine()
        target = self._make_target(state=TargetState.NO_TARGET, mov_energy=0, stat_energy=0)
        level = engine.update_radar(target)
        assert level == PresenceLevel.NO_SIGNAL

    def test_single_frame_gives_experimental(self):
        cfg = DetectionConfig(dwell_frames_experimental=1)
        engine = DetectionEngine(config=cfg)
        target = self._make_target(mov_energy=15)
        level = engine.update_radar(target)
        assert level >= PresenceLevel.EXPERIMENTAL

    def test_sustained_detection_reaches_probable(self):
        cfg = DetectionConfig(
            dwell_frames_probable=3,
            min_energy_probable=25,
        )
        engine = DetectionEngine(config=cfg)

        for _ in range(5):
            level = engine.update_radar(self._make_target(mov_energy=40))

        assert level >= PresenceLevel.PROBABLE

    def test_vitals_promote_to_confirmed(self):
        cfg = DetectionConfig(vitals_promote=True, dwell_frames_experimental=1)
        engine = DetectionEngine(config=cfg)

        # First establish experimental via radar
        engine.update_radar(self._make_target(mov_energy=15))

        # Now add good vitals
        vitals = VitalSigns(
            timestamp=time.time(),
            is_finger_present=True,
            signal_quality=0.8,
            heart_rate_bpm=72,
            spo2_percent=98,
        )
        level = engine.update_vitals(vitals)
        assert level == PresenceLevel.CONFIRMED

    def test_out_of_range_ignored(self):
        cfg = DetectionConfig(max_detection_range_cm=300)
        engine = DetectionEngine(config=cfg)
        target = self._make_target(dist=500, mov_energy=80)
        level = engine.update_radar(target)
        assert level == PresenceLevel.NO_SIGNAL

    def test_alert_callback_fires(self):
        alerts_received = []
        cfg = DetectionConfig(dwell_frames_experimental=1)
        engine = DetectionEngine(config=cfg)
        engine.on_alert(lambda a: alerts_received.append(a))

        engine.update_radar(self._make_target(mov_energy=15))
        assert len(alerts_received) >= 1
        assert isinstance(alerts_received[0], Alert)

    def test_reset_clears_state(self):
        engine = DetectionEngine()
        engine.update_radar(self._make_target(mov_energy=50))
        engine.reset()
        state = engine.get_state()
        assert state.current_level == PresenceLevel.NO_SIGNAL
        assert state.consecutive_detections == 0

    def test_state_to_dict(self):
        engine = DetectionEngine()
        d = engine.get_state().to_dict()
        assert "current_level" in d
        assert "consecutive_detections" in d


# ---------------------------------------------------------------------------
# Alert Manager
# ---------------------------------------------------------------------------
class TestAlertManager:

    def _make_alert(self, severity=AlertSeverity.WARNING, msg="test"):
        return Alert(
            timestamp=time.time(),
            severity=severity,
            level=PresenceLevel.PROBABLE,
            message=msg,
        )

    def test_handle_and_retrieve(self):
        mgr = AlertManager()
        a = self._make_alert()
        mgr.handle_alert(a)
        assert mgr.total_count == 1
        recent = mgr.get_recent(10)
        assert len(recent) == 1
        assert recent[0].message == "test"

    def test_subscriber_called(self):
        received = []
        mgr = AlertManager()
        mgr.subscribe(lambda a: received.append(a))
        mgr.handle_alert(self._make_alert())
        assert len(received) == 1

    def test_filter_by_severity(self):
        mgr = AlertManager()
        mgr.handle_alert(self._make_alert(AlertSeverity.INFO, "info"))
        mgr.handle_alert(self._make_alert(AlertSeverity.CRITICAL, "crit"))
        mgr.handle_alert(self._make_alert(AlertSeverity.WARNING, "warn"))

        crits = mgr.get_critical()
        assert len(crits) == 1
        assert crits[0].message == "crit"

    def test_clear(self):
        mgr = AlertManager()
        mgr.handle_alert(self._make_alert())
        mgr.clear()
        assert mgr.total_count == 0
