"""
Human Presence Detection & Alert Logic
========================================

Consumes parsed LD2410D target data and MAX30100 vital signs to
make higher-level rescue decisions:

  1. **Presence Classification** – converts raw sensor state into
     a tiered assessment (no_signal → experimental → probable → confirmed).
  2. **Alert Generation** – produces actionable alerts when presence
     confidence crosses defined thresholds.
  3. **Temporal Smoothing** – prevents single-frame false positives /
     negatives by requiring sustained detections over a configurable
     dwell time.

Terminology
-----------
- *experimental*: sensor reports something, but confidence is low
  (single frame, low energy, no corroboration).
- *probable*: sustained detection above energy thresholds.
- *confirmed*: vital signs (MAX30100) corroborate presence or radar
  signal is very strong and sustained.

The module is stateful: it tracks detection history internally and
emits alerts on *transitions* (e.g. from no_signal → probable).
"""

from __future__ import annotations

import logging
import time
from collections import deque
from dataclasses import dataclass, field
from enum import IntEnum, auto
from typing import Callable, Optional

from src.hardware.ld2410d import TargetData, TargetState
from src.hardware.max30100 import VitalSigns

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Detection tiers
# ---------------------------------------------------------------------------
class PresenceLevel(IntEnum):
    """Graduated presence confidence level."""
    NO_SIGNAL    = 0
    EXPERIMENTAL = 1   # sensor triggered but low confidence
    PROBABLE     = 2   # sustained detection, moderate energy
    CONFIRMED    = 3   # corroborated by vitals or very strong radar


class AlertSeverity(IntEnum):
    """Alert urgency for downstream consumers (UI, logging)."""
    INFO     = 0
    WARNING  = 1
    CRITICAL = 2


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
@dataclass
class DetectionConfig:
    """Tunable parameters for the detection engine."""

    # --- radar thresholds ---
    min_energy_experimental: int = 10   # energy ≥ this → EXPERIMENTAL
    min_energy_probable: int = 30       # energy ≥ this → PROBABLE
    min_energy_confirmed: int = 60      # energy ≥ this (sustained) → CONFIRMED

    # --- temporal smoothing ---
    dwell_frames_experimental: int = 2  # consecutive frames to reach EXPERIMENTAL
    dwell_frames_probable: int = 5      # consecutive frames to reach PROBABLE
    dwell_frames_confirmed: int = 10    # consecutive frames to reach CONFIRMED

    # --- timeouts ---
    absence_timeout_s: float = 3.0      # seconds of no-target → drop to NO_SIGNAL

    # --- vital-sign promotion ---
    vitals_promote: bool = True         # if True, good vitals auto-promote to CONFIRMED
    min_vitals_quality: float = 0.4     # minimum signal_quality for promotion

    # --- distance filter ---
    max_detection_range_cm: int = 600   # ignore targets beyond this


# ---------------------------------------------------------------------------
# Alert
# ---------------------------------------------------------------------------
@dataclass
class Alert:
    """An alert emitted by the detection engine."""
    timestamp: float
    severity: AlertSeverity
    level: PresenceLevel
    message: str
    zone_id: Optional[str] = None
    radar_data: Optional[dict] = None
    vitals_data: Optional[dict] = None

    def to_dict(self) -> dict:
        return {
            "timestamp": self.timestamp,
            "severity": self.severity.name,
            "level": self.level.name,
            "message": self.message,
            "zone_id": self.zone_id,
            "radar_data": self.radar_data,
            "vitals_data": self.vitals_data,
        }


# ---------------------------------------------------------------------------
# Detection State
# ---------------------------------------------------------------------------
@dataclass
class DetectionState:
    """
    Live snapshot of the detection engine's internal state.

    Exposed for the UI / API layer.
    """
    current_level: PresenceLevel = PresenceLevel.NO_SIGNAL
    consecutive_detections: int = 0
    last_detection_time: float = 0.0
    last_target: Optional[TargetData] = None
    last_vitals: Optional[VitalSigns] = None
    active_zone_id: Optional[str] = None

    def to_dict(self) -> dict:
        return {
            "current_level": self.current_level.name,
            "consecutive_detections": self.consecutive_detections,
            "last_detection_time": self.last_detection_time,
            "last_target": self.last_target.to_dict() if self.last_target else None,
            "last_vitals": self.last_vitals.to_dict() if self.last_vitals else None,
            "active_zone_id": self.active_zone_id,
        }


# ---------------------------------------------------------------------------
# Detection Engine
# ---------------------------------------------------------------------------
class DetectionEngine:
    """
    Core presence detection state machine.

    Feed it LD2410D target frames and MAX30100 vital signs;
    it maintains a detection level and emits alerts on transitions.

    Usage
    -----
    >>> engine = DetectionEngine()
    >>> engine.on_alert(my_alert_handler)
    >>> engine.update_radar(target_data)       # call on each frame
    >>> engine.update_vitals(vital_signs)      # call when available
    >>> state = engine.get_state()
    """

    def __init__(self, config: Optional[DetectionConfig] = None, zone_id: Optional[str] = None):
        self._cfg = config or DetectionConfig()
        self._zone_id = zone_id

        self._level = PresenceLevel.NO_SIGNAL
        self._consecutive = 0
        self._last_detection_time = 0.0
        self._last_target: Optional[TargetData] = None
        self._last_vitals: Optional[VitalSigns] = None

        # Energy history for trend analysis
        self._energy_history: deque[int] = deque(maxlen=30)

        # Alert callbacks
        self._alert_handlers: list[Callable[[Alert], None]] = []

        # Previous level (for transition detection)
        self._prev_level = PresenceLevel.NO_SIGNAL

    # ---- public API -------------------------------------------------------

    def on_alert(self, handler: Callable[[Alert], None]) -> None:
        """Register a callback for alert transitions."""
        self._alert_handlers.append(handler)

    def update_radar(self, target: TargetData) -> PresenceLevel:
        """
        Process a new LD2410D target frame.

        Returns the updated PresenceLevel.
        """
        self._last_target = target
        now = target.timestamp or time.time()

        # --- range filter ---
        max_dist = max(target.moving_distance_cm, target.stationary_distance_cm)
        if max_dist > self._cfg.max_detection_range_cm:
            self._handle_no_target(now)
            return self._level

        if not target.has_target:
            self._handle_no_target(now)
            return self._level

        # --- target detected ---
        self._last_detection_time = now
        peak_energy = max(target.moving_energy, target.stationary_energy)
        self._energy_history.append(peak_energy)
        self._consecutive += 1

        # --- classify level ---
        new_level = self._classify_radar(peak_energy)

        # --- vital-sign promotion ---
        if (
            self._cfg.vitals_promote
            and self._last_vitals is not None
            and self._last_vitals.is_finger_present
            and self._last_vitals.signal_quality >= self._cfg.min_vitals_quality
        ):
            new_level = max(new_level, PresenceLevel.CONFIRMED)

        self._transition(new_level, now)
        return self._level

    def update_vitals(self, vitals: VitalSigns) -> PresenceLevel:
        """
        Incorporate new vital signs into the detection state.

        Good vitals can *promote* an existing radar detection to CONFIRMED.
        """
        self._last_vitals = vitals
        now = vitals.timestamp or time.time()

        if (
            vitals.is_finger_present
            and vitals.signal_quality >= self._cfg.min_vitals_quality
            and self._level >= PresenceLevel.EXPERIMENTAL
        ):
            self._transition(PresenceLevel.CONFIRMED, now)
            return self._level

        # Vitals alone (without radar) produce EXPERIMENTAL at best
        if vitals.is_finger_present and self._level == PresenceLevel.NO_SIGNAL:
            self._transition(PresenceLevel.EXPERIMENTAL, now)

        return self._level

    def get_state(self) -> DetectionState:
        """Return a snapshot of the engine's internal state."""
        return DetectionState(
            current_level=self._level,
            consecutive_detections=self._consecutive,
            last_detection_time=self._last_detection_time,
            last_target=self._last_target,
            last_vitals=self._last_vitals,
            active_zone_id=self._zone_id,
        )

    def set_zone(self, zone_id: str) -> None:
        """Associate detection with a named search zone."""
        self._zone_id = zone_id

    def reset(self) -> None:
        """Clear all state."""
        self._level = PresenceLevel.NO_SIGNAL
        self._prev_level = PresenceLevel.NO_SIGNAL
        self._consecutive = 0
        self._last_detection_time = 0.0
        self._last_target = None
        self._last_vitals = None
        self._energy_history.clear()

    # ---- private -----------------------------------------------------------

    def _classify_radar(self, peak_energy: int) -> PresenceLevel:
        """
        Map peak energy + consecutive count to a PresenceLevel.
        """
        if peak_energy >= self._cfg.min_energy_confirmed and \
                self._consecutive >= self._cfg.dwell_frames_confirmed:
            return PresenceLevel.CONFIRMED
        if peak_energy >= self._cfg.min_energy_probable and \
                self._consecutive >= self._cfg.dwell_frames_probable:
            return PresenceLevel.PROBABLE
        if peak_energy >= self._cfg.min_energy_experimental and \
                self._consecutive >= self._cfg.dwell_frames_experimental:
            return PresenceLevel.EXPERIMENTAL
        if peak_energy >= self._cfg.min_energy_experimental:
            return PresenceLevel.EXPERIMENTAL
        return PresenceLevel.NO_SIGNAL

    def _handle_no_target(self, now: float) -> None:
        """Handle a no-target frame (may downgrade after timeout)."""
        if self._level == PresenceLevel.NO_SIGNAL:
            self._consecutive = 0
            return

        elapsed = now - self._last_detection_time
        if elapsed >= self._cfg.absence_timeout_s:
            self._consecutive = 0
            self._transition(PresenceLevel.NO_SIGNAL, now)
        # else: hold current level (hysteresis)

    def _transition(self, new_level: PresenceLevel, now: float) -> None:
        """Update level and fire alert if it changed."""
        if new_level == self._level:
            return

        old_level = self._level
        self._level = new_level
        self._prev_level = old_level

        # Determine severity
        if new_level == PresenceLevel.NO_SIGNAL:
            severity = AlertSeverity.INFO
        elif new_level == PresenceLevel.EXPERIMENTAL:
            severity = AlertSeverity.INFO
        elif new_level == PresenceLevel.PROBABLE:
            severity = AlertSeverity.WARNING
        else:
            severity = AlertSeverity.CRITICAL

        msg = self._build_message(old_level, new_level)
        alert = Alert(
            timestamp=now,
            severity=severity,
            level=new_level,
            message=msg,
            zone_id=self._zone_id,
            radar_data=self._last_target.to_dict() if self._last_target else None,
            vitals_data=self._last_vitals.to_dict() if self._last_vitals else None,
        )

        logger.info("Alert [%s]: %s", severity.name, msg)
        self._emit_alert(alert)

    @staticmethod
    def _build_message(old: PresenceLevel, new: PresenceLevel) -> str:
        """Human-readable transition message."""
        if new == PresenceLevel.CONFIRMED:
            return "SURVIVOR DETECTED – presence confirmed with high confidence"
        if new == PresenceLevel.PROBABLE:
            return "Probable human presence detected – sustained radar signal"
        if new == PresenceLevel.EXPERIMENTAL:
            return "Experimental detection – possible presence, low confidence"
        if new == PresenceLevel.NO_SIGNAL and old >= PresenceLevel.PROBABLE:
            return "Signal lost – previously detected presence no longer observed"
        return f"Detection level changed: {old.name} → {new.name}"

    def _emit_alert(self, alert: Alert) -> None:
        for handler in self._alert_handlers:
            try:
                handler(alert)
            except Exception:
                logger.exception("Alert handler error")
