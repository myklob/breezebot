"""Tests for indoor temperature source abstraction."""
from __future__ import annotations

import pytest

from nightcool.config import (
    BLESensorConfig,
    IndoorSourceKind,
    IndoorTempConfig,
    NestConfig,
)
from nightcool.sources import (
    BLESource,
    ManualSource,
    SensorFileSource,
    SourceUnavailable,
    make_source,
    read_indoor_with_fallback,
)


def test_manual_source_uses_default_when_state_empty():
    src = ManualSource(reader=lambda: None, default_f=70.0)
    assert src.current_temperature() == 70.0


def test_manual_source_returns_state_value_when_present():
    src = ManualSource(reader=lambda: 68.5, default_f=70.0)
    assert src.current_temperature() == 68.5


def test_sensor_file_reads_float(tmp_path):
    p = tmp_path / "temp.txt"
    p.write_text("72.3\n")
    src = SensorFileSource(p)
    assert src.current_temperature() == 72.3


def test_sensor_file_missing_raises(tmp_path):
    src = SensorFileSource(tmp_path / "missing.txt")
    with pytest.raises(SourceUnavailable):
        src.current_temperature()


def test_sensor_file_non_numeric_raises(tmp_path):
    p = tmp_path / "temp.txt"
    p.write_text("hello")
    with pytest.raises(SourceUnavailable):
        SensorFileSource(p).current_temperature()


def test_ble_source_reads_cache_file(tmp_path):
    p = tmp_path / "ble.txt"
    p.write_text("69.0")
    cfg = BLESensorConfig(mac="aa:bb", cache_file=p)
    assert BLESource(cfg).current_temperature() == 69.0


def test_make_source_dispatches_on_kind(tmp_path):
    cfg_manual = IndoorTempConfig(source=IndoorSourceKind.MANUAL, manual_default_f=70.0)
    assert isinstance(make_source(cfg_manual, lambda: None), ManualSource)

    p = tmp_path / "x.txt"
    p.write_text("70")
    cfg_file = IndoorTempConfig(
        source=IndoorSourceKind.SENSOR_FILE, sensor_file_path=p,
    )
    assert isinstance(make_source(cfg_file, lambda: None), SensorFileSource)


def test_make_source_rejects_incomplete_nest():
    cfg = IndoorTempConfig(source=IndoorSourceKind.NEST)
    with pytest.raises(ValueError):
        make_source(cfg, lambda: None)


def test_read_indoor_with_fallback_returns_provenance(tmp_path):
    p = tmp_path / "missing.txt"
    src = SensorFileSource(p)
    val, name = read_indoor_with_fallback(src, fallback_f=71.5)
    assert val == 71.5
    assert name == "fallback"
