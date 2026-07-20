"""Tests for monitor interval clamping."""
import logging
import pytest

from monitor import _coerce_interval


def test_normal_value_passes_through():
    assert _coerce_interval(30) == 30
    assert _coerce_interval(60) == 60
    assert _coerce_interval("120") == 120


def test_zero_clamped_to_min():
    assert _coerce_interval(0) == 1


def test_negative_clamped_to_min():
    assert _coerce_interval(-10) == 1


def test_non_numeric_falls_back_to_30():
    assert _coerce_interval("garbage") == 30
    assert _coerce_interval(None) == 30


def test_excessive_clamped_to_max():
    assert _coerce_interval(999999) == 86400


def test_warns_on_bad_value(caplog):
    caplog.set_level(logging.WARNING, logger="fping_monitor")
    _coerce_interval(0)
    assert any("夹到" in r.message for r in caplog.records)
