"""Tests for monitor's component-build helpers."""
import pytest

from monitor import build_detector, build_notifier, build_scheduler, _coerce_interval
from detector import FpingDetector
from notifier import Notifier


def test_build_detector_uses_cfg_fping():
    d = build_detector({"fping": {"count": 3, "timeout_ms": 1000,
                                   "interval_ms": 20, "retry": 2}})
    assert isinstance(d, FpingDetector)
    assert d.count == 3
    assert d.timeout_ms == 1000
    assert d.interval_ms == 20
    assert d.retry == 2


def test_build_detector_defaults_when_fping_missing():
    d = build_detector({})
    assert d.count == 1
    assert d.timeout_ms == 500


def test_build_notifier_returns_notifier_instance():
    # build_notifier 接受 cfg，期望内部有 "notify" 子键（与 build_scheduler 一致）。
    n = build_notifier({"notify": {"enabled": True,
                                    "channels": [{"type": "dingtalk",
                                                  "webhook_url": "https://x"}]}})
    assert isinstance(n, Notifier)
    assert len(n.channels) == 1


def test_build_scheduler_uses_internal_builders(monkeypatch):
    db = object()      # Scheduler 不会在这一层调 db
    sched, notifier, detector = build_scheduler(
        {"failure_threshold": 5, "fping": {"count": 2}}, db
    )
    assert detector.count == 2
    assert isinstance(notifier, Notifier)


def test_build_scheduler_accepts_explicit_components():
    """显式传 detector/notifier 时不应再调 build_detector / build_notifier。"""
    class DummyDetector:
        count = 99
    class DummyNotifier:
        channels = ["x"]
    sched, notifier, detector = build_scheduler(
        {}, db=object(),
        detector=DummyDetector(), notifier=DummyNotifier(),
    )
    assert detector.count == 99
    assert notifier.channels == ["x"]
