"""
Sensor Data Validation & Processing Pipeline
==============================================

Sits between the raw hardware parsers and the detection engine.
Responsibilities:

  1. **Validation** – reject frames with impossible values, CRC errors,
     or out-of-range readings.
  2. **Filtering** – exponential-moving-average smoothing, outlier
     rejection (z-score), and rate-limiting.
  3. **Normalization** – convert raw values into consistent units and
     attach quality metadata.

This module is sensor-agnostic: it works with generic "SensorReading"
objects so the same pipeline can process LD2410D and MAX30100 data.
"""

from __future__ import annotations

import logging
import time
from collections import deque
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Any, Optional

import numpy as np

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Validation result
# ---------------------------------------------------------------------------
class ValidationStatus(Enum):
    VALID        = auto()
    OUT_OF_RANGE = auto()
    STALE        = auto()
    DUPLICATE    = auto()
    CORRUPTED    = auto()


@dataclass
class ValidationResult:
    status: ValidationStatus
    message: str = ""


# ---------------------------------------------------------------------------
# Generic sensor reading envelope
# ---------------------------------------------------------------------------
@dataclass
class SensorReading:
    """
    Uniform envelope for any sensor value passing through the pipeline.

    ``source`` identifies the sensor (e.g. "ld2410d", "max30100").
    ``field_name`` identifies the specific metric (e.g. "moving_energy").
    ``raw_value`` is the original numeric value.
    ``processed_value`` is set after filtering/smoothing.
    """
    source: str
    field_name: str
    raw_value: float
    processed_value: float = 0.0
    timestamp: float = 0.0
    quality: float = 1.0           # 0.0 – 1.0
    validation: ValidationStatus = ValidationStatus.VALID
    metadata: dict = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Validation rules
# ---------------------------------------------------------------------------
@dataclass
class FieldRule:
    """Defines valid bounds and freshness requirements for one metric."""
    min_value: float = -float("inf")
    max_value: float = float("inf")
    max_age_s: float = 10.0        # reading older than this → STALE
    max_rate: float = float("inf") # max change per second (optional)


# Default rules keyed by (source, field_name)
DEFAULT_RULES: dict[tuple[str, str], FieldRule] = {
    # LD2410D radar
    ("ld2410d", "moving_distance_cm"):      FieldRule(min_value=0, max_value=800),
    ("ld2410d", "stationary_distance_cm"):  FieldRule(min_value=0, max_value=800),
    ("ld2410d", "moving_energy"):           FieldRule(min_value=0, max_value=100),
    ("ld2410d", "stationary_energy"):       FieldRule(min_value=0, max_value=100),
    # MAX30100 vitals
    ("max30100", "heart_rate_bpm"):         FieldRule(min_value=25, max_value=250),
    ("max30100", "spo2_percent"):           FieldRule(min_value=50, max_value=100),
    ("max30100", "ir_value"):               FieldRule(min_value=0, max_value=65535),
    ("max30100", "red_value"):              FieldRule(min_value=0, max_value=65535),
}


class SensorValidator:
    """
    Validates individual SensorReadings against configured rules.
    """

    def __init__(self, rules: Optional[dict[tuple[str, str], FieldRule]] = None):
        self._rules = rules or DEFAULT_RULES
        self._last_values: dict[tuple[str, str], tuple[float, float]] = {}

    def validate(self, reading: SensorReading) -> ValidationResult:
        """
        Validate a single reading.

        Side-effects: updates ``reading.validation``.
        """
        key = (reading.source, reading.field_name)
        rule = self._rules.get(key)

        if rule is None:
            # No rule → assume valid
            reading.validation = ValidationStatus.VALID
            return ValidationResult(ValidationStatus.VALID)

        # Range check
        if not (rule.min_value <= reading.raw_value <= rule.max_value):
            reading.validation = ValidationStatus.OUT_OF_RANGE
            return ValidationResult(
                ValidationStatus.OUT_OF_RANGE,
                f"{reading.field_name}={reading.raw_value} outside "
                f"[{rule.min_value}, {rule.max_value}]",
            )

        # Staleness check
        now = time.time()
        if reading.timestamp > 0 and (now - reading.timestamp) > rule.max_age_s:
            reading.validation = ValidationStatus.STALE
            return ValidationResult(
                ValidationStatus.STALE,
                f"{reading.field_name} is {now - reading.timestamp:.1f}s old",
            )

        # Duplicate / rate check
        if key in self._last_values:
            prev_val, prev_ts = self._last_values[key]
            dt = reading.timestamp - prev_ts
            if dt > 0 and rule.max_rate < float("inf"):
                rate = abs(reading.raw_value - prev_val) / dt
                if rate > rule.max_rate:
                    reading.validation = ValidationStatus.OUT_OF_RANGE
                    return ValidationResult(
                        ValidationStatus.OUT_OF_RANGE,
                        f"{reading.field_name} rate {rate:.1f}/s exceeds max {rule.max_rate}",
                    )

        self._last_values[key] = (reading.raw_value, reading.timestamp)
        reading.validation = ValidationStatus.VALID
        return ValidationResult(ValidationStatus.VALID)


# ---------------------------------------------------------------------------
# Smoothing / Filtering
# ---------------------------------------------------------------------------
class EMAFilter:
    """Exponential Moving Average filter for a single stream."""

    def __init__(self, alpha: float = 0.3):
        """
        Parameters
        ----------
        alpha : float
            Smoothing factor in [0, 1].  Higher → less smoothing.
        """
        self._alpha = alpha
        self._value: Optional[float] = None

    def update(self, raw: float) -> float:
        if self._value is None:
            self._value = raw
        else:
            self._value = self._alpha * raw + (1 - self._alpha) * self._value
        return self._value

    @property
    def value(self) -> Optional[float]:
        return self._value

    def reset(self) -> None:
        self._value = None


class OutlierDetector:
    """
    Z-score-based outlier detector for a rolling window.

    If a sample's z-score exceeds the threshold, it is flagged.
    """

    def __init__(self, window: int = 30, threshold: float = 3.0):
        self._window: deque[float] = deque(maxlen=window)
        self._threshold = threshold

    def is_outlier(self, value: float) -> bool:
        if len(self._window) < 5:
            self._window.append(value)
            return False

        arr = np.array(self._window)
        mean = float(np.mean(arr))
        std = float(np.std(arr))

        self._window.append(value)

        if std < 1e-6:
            # With zero variance, any deviation from the constant is an outlier
            return abs(value - mean) > 1e-6

        z = abs(value - mean) / std
        return z > self._threshold


# ---------------------------------------------------------------------------
# Processing Pipeline
# ---------------------------------------------------------------------------
class SensorProcessingPipeline:
    """
    End-to-end pipeline: validate → outlier-check → smooth → quality-tag.

    Maintains per-(source, field) filter state.
    """

    def __init__(
        self,
        validator: Optional[SensorValidator] = None,
        ema_alpha: float = 0.3,
        outlier_window: int = 30,
        outlier_threshold: float = 3.0,
    ):
        self._validator = validator or SensorValidator()
        self._ema_alpha = ema_alpha
        self._ema_filters: dict[tuple[str, str], EMAFilter] = {}
        self._outlier_detectors: dict[tuple[str, str], OutlierDetector] = {}
        self._outlier_window = outlier_window
        self._outlier_threshold = outlier_threshold

    def process(self, reading: SensorReading) -> SensorReading:
        """
        Run the full pipeline on a single reading.

        Returns the same object with ``processed_value`` and ``quality``
        updated in place.
        """
        key = (reading.source, reading.field_name)

        # 1. Validate
        result = self._validator.validate(reading)
        if result.status != ValidationStatus.VALID:
            reading.quality = 0.0
            reading.processed_value = reading.raw_value
            logger.debug("Validation failed: %s", result.message)
            return reading

        # 2. Outlier check
        detector = self._outlier_detectors.setdefault(
            key,
            OutlierDetector(self._outlier_window, self._outlier_threshold),
        )
        if detector.is_outlier(reading.raw_value):
            reading.quality *= 0.3  # degrade quality but don't discard
            logger.debug("Outlier detected: %s=%s", reading.field_name, reading.raw_value)

        # 3. EMA smoothing
        filt = self._ema_filters.setdefault(key, EMAFilter(self._ema_alpha))
        reading.processed_value = filt.update(reading.raw_value)

        return reading

    def process_batch(self, readings: list[SensorReading]) -> list[SensorReading]:
        """Process multiple readings, returning the same list."""
        return [self.process(r) for r in readings]

    def reset(self) -> None:
        """Clear all filter state."""
        self._ema_filters.clear()
        self._outlier_detectors.clear()
