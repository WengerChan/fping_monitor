"""Tests for the healthcheck CLI subcommand.

`python monitor.py healthcheck` 跑两项检查：DB 连通 + fping 探活。
"""
from unittest.mock import patch

import pytest

from monitor import run_healthcheck


def _cfg(db_path="data/state.db", gateway="1.1.1.1"):
    return {
        "database": db_path,
        "healthcheck": {"gateway": gateway},
    }


# ---- 全部健康 ---------------------------------------------------------------


def test_returns_zero_when_all_ok(tmp_path, monkeypatch):
    cfg = _cfg(db_path=str(tmp_path / "state.db"), gateway="1.1.1.1")
    # 不真跑 fping，模拟 alive=True
    from models import Host
    monkeypatch.setattr(
        "monitor.FpingDetector.detect",
        lambda self, hosts: {h.name: True for h in hosts},
    )
    assert run_healthcheck(cfg) == 0


# ---- DB 不健康 --------------------------------------------------------------


def test_returns_one_when_db_fails(tmp_path, monkeypatch):
    cfg = _cfg(db_path="/nonexistent/cant-create/state.db")
    # 即使 fping OK，DB 不通也算不健康
    monkeypatch.setattr(
        "monitor.FpingDetector.detect",
        lambda self, hosts: {h.name: True for h in hosts},
    )
    assert run_healthcheck(cfg) == 1


def test_returns_one_when_db_path_invalid(tmp_path, monkeypatch):
    # DB 路径指向一个文件而不是目录
    bad = tmp_path / "regular_file.db"
    bad.write_text("not a dir")
    cfg = _cfg(db_path=str(bad / "subdir" / "state.db"))
    monkeypatch.setattr(
        "monitor.FpingDetector.detect",
        lambda self, hosts: {h.name: True for h in hosts},
    )
    assert run_healthcheck(cfg) == 1


# ---- fping 不健康 -----------------------------------------------------------


def test_returns_one_when_fping_unreachable(tmp_path, monkeypatch):
    cfg = _cfg(db_path=str(tmp_path / "state.db"), gateway="127.0.0.1")
    # 模拟 fping 探测失败（不可达）
    monkeypatch.setattr(
        "monitor.FpingDetector.detect",
        lambda self, hosts: {h.name: False for h in hosts},
    )
    assert run_healthcheck(cfg) == 1


def test_returns_one_when_fping_raises(tmp_path, monkeypatch):
    cfg = _cfg(db_path=str(tmp_path / "state.db"))

    def _boom(self, hosts):
        raise RuntimeError("fping binary missing")

    monkeypatch.setattr("monitor.FpingDetector.detect", _boom)
    assert run_healthcheck(cfg) == 1


# ---- 默认值 -----------------------------------------------------------------


def test_default_gateway_is_set(tmp_path, monkeypatch):
    """不配 healthcheck.gateway 时应有默认值（1.1.1.1）。"""
    cfg = {"database": str(tmp_path / "state.db")}     # 没 healthcheck
    seen_ips = []
    def _spy(self, hosts):
        seen_ips.extend(h.ip for h in hosts)
        return {h.name: True for h in hosts}
    monkeypatch.setattr("monitor.FpingDetector.detect", _spy)
    assert run_healthcheck(cfg) == 0
    assert seen_ips == ["1.1.1.1"]
