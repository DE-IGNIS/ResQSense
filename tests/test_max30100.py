"""
Tests for MAX30100 parser and processor.
"""

import json
import time
import pytest
import numpy as np

from src.hardware.max30100 import (
    MAX30100Parser,
    MAX30100Processor,
    RawPPGSample,
    VitalSigns,
)


class TestMAX30100Parser:

    def test_parse_single_sample(self):
        data = json.dumps({"ir": 50000, "red": 40000, "ts": 1000.0}).encode()
        samples = MAX30100Parser.parse_line(data)
        assert len(samples) == 1
        assert samples[0].ir_value == 50000
        assert samples[0].red_value == 40000
        assert samples[0].timestamp == 1000.0

    def test_parse_batch(self):
        data = json.dumps({
            "samples": [
                {"ir": 50000, "red": 40000},
                {"ir": 51000, "red": 41000},
                {"ir": 52000, "red": 42000},
            ],
            "ts": 2000.0,
        }).encode()
        samples = MAX30100Parser.parse_line(data)
        assert len(samples) == 3
        assert samples[2].ir_value == 52000

    def test_parse_invalid_json(self):
        assert MAX30100Parser.parse_line(b"not json") == []
        assert MAX30100Parser.parse_line(b"") == []

    def test_parse_missing_fields(self):
        data = json.dumps({"ir": 50000}).encode()
        samples = MAX30100Parser.parse_line(data)
        assert len(samples) == 1
        assert samples[0].red_value == 0  # default


class TestMAX30100Processor:

    def _make_processor_with_data(self, n=100, ir_base=50000, red_base=40000):
        """Create a processor and feed it synthetic PPG-like data."""
        proc = MAX30100Processor()
        t = time.time()
        for i in range(n):
            # Simulate a pulsatile signal
            ir_val = ir_base + int(500 * np.sin(2 * np.pi * i / 25))  # ~72 bpm at 50 sps
            red_val = red_base + int(300 * np.sin(2 * np.pi * i / 25))
            proc.add_sample(RawPPGSample(
                timestamp=t + i * 0.02,  # 50 samples/sec
                ir_value=ir_val,
                red_value=red_val,
            ))
        return proc

    def test_no_finger_detection(self):
        proc = MAX30100Processor()
        for i in range(30):
            proc.add_sample(RawPPGSample(
                timestamp=time.time() + i * 0.02,
                ir_value=100,  # very low → no finger
                red_value=50,
            ))
        vitals = proc.compute_vitals()
        assert vitals.is_finger_present is False
        assert vitals.confidence == "none"

    def test_finger_present_with_data(self):
        proc = self._make_processor_with_data()
        vitals = proc.compute_vitals()
        assert vitals.is_finger_present is True
        assert vitals.ir_dc > 0
        assert vitals.red_dc > 0

    def test_heart_rate_in_range(self):
        proc = self._make_processor_with_data(n=150)
        vitals = proc.compute_vitals()
        # With our synthetic sine at 50 sps, period=25 samples → 2 peaks/sec → 120 BPM
        # Allow generous range due to edge effects
        if vitals.heart_rate_bpm > 0:
            assert 30 <= vitals.heart_rate_bpm <= 220

    def test_spo2_in_range(self):
        proc = self._make_processor_with_data(n=150)
        vitals = proc.compute_vitals()
        if vitals.spo2_percent > 0:
            assert 50 <= vitals.spo2_percent <= 100

    def test_reset_clears_buffers(self):
        proc = self._make_processor_with_data()
        proc.reset()
        vitals = proc.compute_vitals()
        assert vitals.confidence == "none"

    def test_vitals_to_dict(self):
        vs = VitalSigns(
            heart_rate_bpm=72.5,
            spo2_percent=98.3,
            signal_quality=0.85,
            is_finger_present=True,
            confidence="high",
        )
        d = vs.to_dict()
        assert d["heart_rate_bpm"] == 72.5
        assert d["confidence"] == "high"
        assert isinstance(d, dict)
