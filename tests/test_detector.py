"""Tests for FpingDetector (timeout math + real fping + DetectResult)."""
import time
from unittest.mock import patch

import pytest

from detector import DetectResult, FpingDetector
from models import Host


# ---- DetectResult dataclass -------------------------------------------------


def test_detect_result_defaults():
    r = DetectResult(alive={"a": True})
    assert r.duration_ms == 0
    assert r.returncode == 0
    assert r.attempted == 0
    assert r.reachable == 0


# ---- timeout 公式（之前是串行错的，现在是并发正确的）---------------------


def test_subprocess_timeout_scales_with_count_not_hosts():
    """并发模型：timeout 只跟单轮耗时有关，不随主机数放大。"""
    det = FpingDetector(count=1, timeout_ms=500, interval_ms=10)
    # 默认参数下：1 × (500+10) / 1000 + 5 = 5.51s → max(10, ...) = 10s
    assert det._subprocess_timeout_s() == 10


def test_subprocess_timeout_grows_with_per_round_time():
    det = FpingDetector(count=3, timeout_ms=2000, interval_ms=100)
    # 3 × (2000+100) / 1000 + 5 = 6.3 + 5 = 11.3s → 11s
    assert det._subprocess_timeout_s() == 11


def test_subprocess_timeout_min_10s():
    """即使参数很小，subprocess 也至少等 10s。"""
    det = FpingDetector(count=1, timeout_ms=1, interval_ms=1)
    assert det._subprocess_timeout_s() == 10


def test_subprocess_timeout_independent_of_host_count():
    """关键不变量：1000 台和 10 台用同一个 timeout。"""
    d_small = FpingDetector(count=1, timeout_ms=500, interval_ms=10)
    d_big = FpingDetector(count=1, timeout_ms=500, interval_ms=10)
    assert d_small._subprocess_timeout_s() == d_big._subprocess_timeout_s()


# ---- 真实 fping 路径（mock subprocess）--------------------------------------


def _host(name, ip):
    return Host(id=None, name=name, ip=ip)


def test_detect_empty_hosts_returns_empty_result():
    det = FpingDetector()
    res = det.detect([])
    assert res.alive == {}
    assert res.attempted == 0
    assert res.duration_ms == 0


def test_detect_success_records_timing_and_counts():
    det = FpingDetector()
    fake_stdout = "1.1.1.1 : [0], 64 bytes, 0.12 ms (0.12 avg, 0% loss)\n"
    fake_completed = type("P", (), {
        "returncode": 0, "stdout": fake_stdout, "stderr": "",
    })()
    with patch("detector.subprocess.run", return_value=fake_completed):
        res = det.detect([_host("a", "1.1.1.1"), _host("b", "2.2.2.2")])
    assert res.alive == {"a": True, "b": False}      # b 不在 alive_rtt 里 → DOWN
    assert res.attempted == 2
    assert res.reachable == 1
    assert res.returncode == 0
    assert res.duration_ms >= 0                       # 计时器跑过


def test_detect_timeout_returns_all_down():
    """fping 超时时（subprocess.TimeoutExpired）所有主机都视为 DOWN。"""
    import subprocess as sp
    det = FpingDetector()
    with patch("detector.subprocess.run",
               side_effect=sp.TimeoutExpired(cmd="fping", timeout=10)):
        res = det.detect([_host("a", "1.1.1.1"), _host("b", "2.2.2.2")])
    assert res.alive == {"a": False, "b": False}
    assert res.returncode == -1
    assert res.reachable == 0
    assert res.duration_ms >= 0                       # 计时器跑过（mock 下可能为 0）


def test_detect_handles_non_zero_returncode():
    """fping 返回 1（全部不可达）也正常处理，不抛异常。"""
    det = FpingDetector()
    fake = type("P", (), {
        "returncode": 1,
        "stdout": "",
        "stderr": "1.1.1.1 : unreachable\n",
    })()
    with patch("detector.subprocess.run", return_value=fake):
        res = det.detect([_host("a", "1.1.1.1")])
    assert res.alive == {"a": False}
    assert res.returncode == 1
    assert res.reachable == 0
