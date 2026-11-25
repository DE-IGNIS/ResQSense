"""
MAX30100 Pulse Oximeter – Data Processor
=========================================

The MAX30100 is an I²C Analog Front-End (AFE) that streams raw
photoplethysmogram (PPG) samples (IR + Red channel) from an internal
16-deep FIFO.  The ESP32-C6 handles the I²C reads, runs DC-removal
filtering on-chip, and sends processed JSON lines to the host over
the serial link.

This module:
  1. Parses the JSON readings arriving from the ESP32.
  2. Applies additional smoothing / outlier rejection on the host side.
  3. Computes BPM and SpO₂ using peak-detection and the Ratio-of-Ratios
     (R-value) method.
  4. Exposes a clean VitalSigns dataclass for downstream consumers.

The heavy real-time DSP (DC removal, band-pass filtering) is assumed to
run on the ESP32; this module provides a *secondary* validation layer.

References:
  - MAX30100 Datasheet (Analog Devices / Maxim Integrated)
  - SpO₂ R-value formula: SpO₂ ≈ 104.0 − 17.0 × R
"""

from __future__ import annotations

import json
import logging
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Optional

import numpy as np

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
# Reasonable physiological bounds
MIN_BPM = 30
MAX_BPM = 220
MIN_SPO2 = 50.0
MAX_SPO2 = 100.0
SAMPLE_WINDOW = 150        # number of samples kept for processing
MIN_PEAK_INTERVAL_S = 0.27 # ~220 BPM
MAX_PEAK_INTERVAL_S = 2.0  # ~30 BPM


# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------
@dataclass
class RawPPGSample:
    """Single sample pair from the MAX30100 FIFO."""
    timestamp: float = 0.0
    ir_value: int = 0
    red_value: int = 0


@dataclass
class VitalSigns:
    """Computed vital signs for downstream consumers."""
    timestamp: float = 0.0
    heart_rate_bpm: float = 0.0
    spo2_percent: float = 0.0
    ir_ac: float = 0.0
    ir_dc: float = 0.0
    red_ac: float = 0.0
    red_dc: float = 0.0
    signal_quality: float = 0.0   # 0.0 – 1.0
    is_finger_present: bool = False
    confidence: str = "none"       # none | low | medium | high

    def to_dict(self) -> dict:
        return {
            "timestamp": self.timestamp,
            "heart_rate_bpm": round(self.heart_rate_bpm, 1),
            "spo2_percent": round(self.spo2_percent, 1),
            "signal_quality": round(self.signal_quality, 2),
            "is_finger_present": self.is_finger_present,
            "confidence": self.confidence,
        }


# ---------------------------------------------------------------------------
# ESP32 JSON line parser
# ---------------------------------------------------------------------------
class MAX30100Parser:
    """
    Parse JSON lines sent by the ESP32 firmware for the MAX30100.

    Expected JSON format::

        {"ir": 54321, "red": 43210, "ts": 1234567.89}

    or batch format::

        {"samples": [{"ir": …, "red": …}, …], "ts": 1234567.89}
    """

    @staticmethod
    def parse_line(data: bytes) -> list[RawPPGSample]:
        """
        Parse one JSON line (or raw bytes) into RawPPGSample(s).

        Returns an empty list on failure.
        """
        try:
            text = data.decode("utf-8", errors="replace").strip()
            if not text:
                return []
            obj = json.loads(text)
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            logger.debug("MAX30100 parse error: %s", exc)
            return []

        now = time.time()

        # Batch format
        if "samples" in obj:
            ts = obj.get("ts", now)
            return [
                RawPPGSample(
                    timestamp=ts,
                    ir_value=int(s.get("ir", 0)),
                    red_value=int(s.get("red", 0)),
                )
                for s in obj["samples"]
            ]

        # Single sample
        return [
            RawPPGSample(
                timestamp=obj.get("ts", now),
                ir_value=int(obj.get("ir", 0)),
                red_value=int(obj.get("red", 0)),
            )
        ]


# ---------------------------------------------------------------------------
# Signal Processor (host-side)
# ---------------------------------------------------------------------------
class MAX30100Processor:
    """
    Host-side signal processing for MAX30100 raw PPG data.

    Maintains a rolling window of samples, computes DC/AC components,
    detects peaks for BPM, and derives SpO₂ via the R-value method.
    """

    def __init__(self, window_size: int = SAMPLE_WINDOW):
        self._window_size = window_size
        self._ir_buffer: deque[float] = deque(maxlen=window_size)
        self._red_buffer: deque[float] = deque(maxlen=window_size)
        self._timestamps: deque[float] = deque(maxlen=window_size)
        self._last_vitals = VitalSigns()

        # Finger detection threshold: if IR DC < this, no finger
        self._finger_threshold = 1000

    def add_sample(self, sample: RawPPGSample) -> None:
        """Add a new raw PPG sample to the processing window."""
        self._ir_buffer.append(float(sample.ir_value))
        self._red_buffer.append(float(sample.red_value))
        self._timestamps.append(sample.timestamp)

    def add_samples(self, samples: list[RawPPGSample]) -> None:
        """Add a batch of raw PPG samples."""
        for s in samples:
            self.add_sample(s)

    def compute_vitals(self) -> VitalSigns:
        """
        Compute heart-rate and SpO₂ from the current sample window.

        Returns a VitalSigns snapshot.
        """
        vs = VitalSigns(timestamp=time.time())

        if len(self._ir_buffer) < 20:
            vs.confidence = "none"
            self._last_vitals = vs
            return vs

        ir_arr = np.array(self._ir_buffer, dtype=np.float64)
        red_arr = np.array(self._red_buffer, dtype=np.float64)
        ts_arr = np.array(self._timestamps, dtype=np.float64)

        # --- DC components (moving average) ---
        ir_dc = float(np.mean(ir_arr))
        red_dc = float(np.mean(red_arr))
        vs.ir_dc = ir_dc
        vs.red_dc = red_dc

        # --- Finger detection ---
        vs.is_finger_present = ir_dc > self._finger_threshold
        if not vs.is_finger_present:
            vs.confidence = "none"
            self._last_vitals = vs
            return vs

        # --- AC components (std-dev as proxy for pulsatile amplitude) ---
        ir_ac = float(np.std(ir_arr))
        red_ac = float(np.std(red_arr))
        vs.ir_ac = ir_ac
        vs.red_ac = red_ac

        # --- Signal quality (SNR proxy) ---
        if ir_dc > 0:
            vs.signal_quality = min(1.0, ir_ac / (ir_dc * 0.05 + 1))
        else:
            vs.signal_quality = 0.0

        # --- Heart rate via peak detection ---
        bpm = self._compute_heart_rate(ir_arr, ts_arr)
        if bpm is not None:
            vs.heart_rate_bpm = bpm

        # --- SpO₂ via Ratio-of-Ratios ---
        spo2 = self._compute_spo2(ir_ac, ir_dc, red_ac, red_dc)
        if spo2 is not None:
            vs.spo2_percent = spo2

        # --- Confidence classification ---
        vs.confidence = self._classify_confidence(vs)

        self._last_vitals = vs
        return vs

    @property
    def last_vitals(self) -> VitalSigns:
        """Return the most recently computed VitalSigns."""
        return self._last_vitals

    def reset(self) -> None:
        """Clear all buffered data."""
        self._ir_buffer.clear()
        self._red_buffer.clear()
        self._timestamps.clear()
        self._last_vitals = VitalSigns()

    # ---- private -----------------------------------------------------------

    @staticmethod
    def _compute_heart_rate(
        ir_arr: np.ndarray, ts_arr: np.ndarray
    ) -> Optional[float]:
        """
        Simple peak-detection BPM calculation.

        Uses zero-crossing of the first derivative of the DC-removed signal.
        """
        if len(ir_arr) < 20:
            return None

        # Remove DC
        ir_ac = ir_arr - np.mean(ir_arr)

        # Simple smoothing (moving average, kernel=5)
        kernel_size = 5
        kernel = np.ones(kernel_size) / kernel_size
        smoothed = np.convolve(ir_ac, kernel, mode="same")

        # Find peaks: points where the derivative changes sign (+ → -)
        diff = np.diff(smoothed)
        peaks = []
        for i in range(1, len(diff)):
            if diff[i - 1] > 0 and diff[i] <= 0:
                # Confirm it's a real peak (above zero)
                if smoothed[i] > 0:
                    peaks.append(i)

        if len(peaks) < 2:
            return None

        # Compute inter-peak intervals
        intervals = []
        for i in range(1, len(peaks)):
            dt = ts_arr[peaks[i]] - ts_arr[peaks[i - 1]]
            if MIN_PEAK_INTERVAL_S <= dt <= MAX_PEAK_INTERVAL_S:
                intervals.append(dt)

        if not intervals:
            return None

        mean_interval = float(np.mean(intervals))
        bpm = 60.0 / mean_interval

        if MIN_BPM <= bpm <= MAX_BPM:
            return bpm
        return None

    @staticmethod
    def _compute_spo2(
        ir_ac: float, ir_dc: float, red_ac: float, red_dc: float
    ) -> Optional[float]:
        """
        Compute SpO₂ using the Ratio-of-Ratios method.

        R = (AC_red / DC_red) / (AC_ir / DC_ir)
        SpO₂ ≈ 104.0 − 17.0 × R

        This is an *empirical approximation* — not a clinically
        calibrated value.
        """
        if ir_dc <= 0 or red_dc <= 0 or ir_ac <= 0:
            return None

        ratio = (red_ac / red_dc) / (ir_ac / ir_dc)
        spo2 = 104.0 - 17.0 * ratio

        if MIN_SPO2 <= spo2 <= MAX_SPO2:
            return spo2
        return None

    @staticmethod
    def _classify_confidence(vs: VitalSigns) -> str:
        """Assign a confidence label based on signal quality and plausibility."""
        if not vs.is_finger_present:
            return "none"
        if vs.signal_quality < 0.2:
            return "low"
        if vs.heart_rate_bpm < MIN_BPM or vs.heart_rate_bpm > MAX_BPM:
            return "low"
        if vs.spo2_percent < 70:
            return "low"
        if vs.signal_quality < 0.5:
            return "medium"
        return "high"
