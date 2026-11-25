"""
Tests for sensor data validation and processing pipeline.
"""

import time
import pytest

from src.core.sensor_processor import (
    SensorReading,
    SensorValidator,
    SensorProcessingPipeline,
    EMAFilter,
    OutlierDetector,
    ValidationStatus,
    FieldRule,
)


class TestSensorValidator:

    def test_valid_reading(self):
        v = SensorValidator()
        r = SensorReading(
            source="ld2410d",
            field_name="moving_energy",
            raw_value=50.0,
            timestamp=time.time(),
        )
        result = v.validate(r)
        assert result.status == ValidationStatus.VALID

    def test_out_of_range_high(self):
        v = SensorValidator()
        r = SensorReading(
            source="ld2410d",
            field_name="moving_energy",
            raw_value=150.0,  # max is 100
            timestamp=time.time(),
        )
        result = v.validate(r)
        assert result.status == ValidationStatus.OUT_OF_RANGE

    def test_out_of_range_low(self):
        v = SensorValidator()
        r = SensorReading(
            source="ld2410d",
            field_name="moving_distance_cm",
            raw_value=-10.0,
            timestamp=time.time(),
        )
        result = v.validate(r)
        assert result.status == ValidationStatus.OUT_OF_RANGE

    def test_stale_reading(self):
        v = SensorValidator()
        r = SensorReading(
            source="max30100",
            field_name="heart_rate_bpm",
            raw_value=72.0,
            timestamp=time.time() - 20,  # 20 seconds old
        )
        result = v.validate(r)
        assert result.status == ValidationStatus.STALE

    def test_unknown_field_passes(self):
        v = SensorValidator()
        r = SensorReading(
            source="unknown",
            field_name="whatever",
            raw_value=999.0,
            timestamp=time.time(),
        )
        result = v.validate(r)
        assert result.status == ValidationStatus.VALID


class TestEMAFilter:

    def test_first_value_passthrough(self):
        f = EMAFilter(alpha=0.3)
        assert f.update(100.0) == 100.0

    def test_smoothing(self):
        f = EMAFilter(alpha=0.5)
        f.update(100.0)
        result = f.update(200.0)
        assert result == 150.0  # 0.5 * 200 + 0.5 * 100

    def test_reset(self):
        f = EMAFilter(alpha=0.3)
        f.update(100.0)
        f.reset()
        assert f.value is None


class TestOutlierDetector:

    def test_normal_values_not_outliers(self):
        d = OutlierDetector(window=20, threshold=3.0)
        for v in [50, 51, 49, 52, 48, 50, 51, 49]:
            assert d.is_outlier(v) is False

    def test_extreme_value_is_outlier(self):
        d = OutlierDetector(window=20, threshold=2.0)
        for v in [50] * 15:
            d.is_outlier(v)
        assert d.is_outlier(500) is True  # way outside


class TestSensorProcessingPipeline:

    def test_valid_reading_processed(self):
        pipe = SensorProcessingPipeline()
        r = SensorReading(
            source="ld2410d",
            field_name="moving_energy",
            raw_value=50.0,
            timestamp=time.time(),
        )
        result = pipe.process(r)
        assert result.validation == ValidationStatus.VALID
        assert result.processed_value > 0

    def test_invalid_reading_quality_zero(self):
        pipe = SensorProcessingPipeline()
        r = SensorReading(
            source="ld2410d",
            field_name="moving_energy",
            raw_value=200.0,  # out of range
            timestamp=time.time(),
        )
        result = pipe.process(r)
        assert result.quality == 0.0

    def test_batch_processing(self):
        pipe = SensorProcessingPipeline()
        readings = [
            SensorReading(
                source="ld2410d",
                field_name="moving_energy",
                raw_value=float(v),
                timestamp=time.time(),
            )
            for v in [40, 42, 38, 41, 39]
        ]
        results = pipe.process_batch(readings)
        assert len(results) == 5
        assert all(r.validation == ValidationStatus.VALID for r in results)
