"""Tests for ConfigWatcher (hot reload)."""
import os
import time
from pathlib import Path

import pytest

from util import ConfigWatcher


def _write(path: Path, content: str) -> None:
    path.write_text(content, encoding="utf-8")


def _touch(path: Path, content: str) -> None:
    """写入新内容并把 mtime 推后（避开 1 秒精度）。"""
    _write(path, content)
    # mtime 精度通常 1 秒，+1.1 保险
    future = time.time() + 1.1
    os.utime(path, (future, future))


@pytest.fixture
def cfg_path(tmp_path):
    p = tmp_path / "config.yaml"
    _write(p, "interval: 30\nfailure_threshold: 3\n")
    return p


@pytest.fixture
def srv_path(tmp_path):
    p = tmp_path / "server.yaml"
    _write(p, "hosts:\n  - name: a\n    ip: 1.1.1.1\n")
    return p


# ---- 基础行为 --------------------------------------------------------------


def test_init_forces_reload(cfg_path, srv_path):
    w = ConfigWatcher(cfg_path, srv_path)
    assert w.cfg.get("interval") == 30
    assert w.server_cfg.get("hosts")[0]["name"] == "a"


def test_no_reload_when_unchanged(cfg_path, srv_path):
    w = ConfigWatcher(cfg_path, srv_path)
    # mtime 没变 → None
    assert w.reload() is None


def test_reload_picks_up_config_change(cfg_path, srv_path):
    w = ConfigWatcher(cfg_path, srv_path)
    _touch(cfg_path, "interval: 60\nfailure_threshold: 5\n")
    assert w.reload() == "config"
    assert w.cfg["interval"] == 60
    assert w.cfg["failure_threshold"] == 5


def test_reload_picks_up_servers_change(cfg_path, srv_path):
    w = ConfigWatcher(cfg_path, srv_path)
    _touch(srv_path, "hosts:\n  - name: a\n    ip: 1.1.1.1\n  - name: b\n    ip: 2.2.2.2\n")
    assert w.reload() == "servers"
    assert len(w.server_cfg["hosts"]) == 2


def test_reload_returns_all_when_both_change(cfg_path, srv_path):
    w = ConfigWatcher(cfg_path, srv_path)
    _touch(cfg_path, "interval: 10\n")
    _touch(srv_path, "hosts:\n  - name: x\n    ip: 9.9.9.9\n")
    assert w.reload() == "all"


# ---- 强制 reload -----------------------------------------------------------


def test_force_reload_ignores_mtime(cfg_path, srv_path):
    w = ConfigWatcher(cfg_path, srv_path)
    # 不动文件，强制 reload
    assert w.reload(force=True) == "all"


def test_force_reload_reads_current_content(cfg_path, srv_path):
    w = ConfigWatcher(cfg_path, srv_path)
    # 直接改文件（不调 touch），手动跑 force
    _write(cfg_path, "interval: 99\n")
    w.reload(force=True)
    assert w.cfg["interval"] == 99


# ---- 边界场景 --------------------------------------------------------------


def test_missing_file_does_not_break(tmp_path, cfg_path):
    """servers 文件暂时不存在时，不应让 reload 抛异常。"""
    srv = tmp_path / "nope.yaml"
    w = ConfigWatcher(cfg_path, srv)
    assert w.server_cfg == {}        # load_yaml 返回空 dict
    assert w.reload() is None        # mtime=0 → 视为未变


def test_sequential_changes_each_detected(cfg_path, srv_path):
    """两个文件分两次改，要能分别识别。"""
    w = ConfigWatcher(cfg_path, srv_path)
    _touch(cfg_path, "interval: 5\n")
    assert w.reload() == "config"
    _touch(srv_path, "hosts: []\n")
    assert w.reload() == "servers"
    _touch(cfg_path, "interval: 6\n")
    assert w.reload() == "config"


# ---- 集成：daemon 风格使用 -------------------------------------------------


def test_typical_daemon_loop(cfg_path, srv_path):
    """模拟 daemon 主循环：每轮 reload，组件按需重建。"""
    w = ConfigWatcher(cfg_path, srv_path)
    rebuild_count = 0

    # 轮 1：无变化
    changed = w.reload()
    if changed in ("config", "all"):
        rebuild_count += 1
    assert rebuild_count == 0

    # 轮 2：interval 改了
    _touch(cfg_path, "interval: 5\n")
    changed = w.reload()
    if changed in ("config", "all"):
        rebuild_count += 1
    assert w.cfg["interval"] == 5
    assert rebuild_count == 1

    # 轮 3：SIGHUP 模拟
    w.reload(force=True)
    # 不变 mtime 也会重新读
    assert w.cfg["interval"] == 5


# ---- YAML 异常兜底 ---------------------------------------------------------


def test_malformed_yaml_keeps_old_config(cfg_path, srv_path):
    """YAML 解析失败时，daemon 不应崩溃，应保留旧配置 + 旧 mtime。"""
    w = ConfigWatcher(cfg_path, srv_path)
    assert w.cfg.get("interval") == 30

    # 写错 YAML（流序列没闭合）
    _touch(cfg_path, "interval: [60, 30\n")
    assert w.reload() is None       # 没真"换成功"
    assert w.cfg.get("interval") == 30       # 旧值保留

    # 修好以后下一次 reload 能恢复
    _touch(cfg_path, "interval: 60\n")
    assert w.reload() == "config"
    assert w.cfg.get("interval") == 60


def test_malformed_servers_yaml_keeps_old_servers(cfg_path, srv_path):
    w = ConfigWatcher(cfg_path, srv_path)
    assert len(w.server_cfg["hosts"]) == 1

    _touch(srv_path, "hosts: [\n")      # 错的
    assert w.reload() is None
    assert len(w.server_cfg["hosts"]) == 1

    _touch(srv_path, "hosts:\n  - {name: a, ip: 1.1.1.1}\n  - {name: b, ip: 2.2.2.2}\n")
    assert w.reload() == "servers"
    assert len(w.server_cfg["hosts"]) == 2


def test_one_file_bad_other_file_good_returns_partial_change(cfg_path, srv_path):
    """config 坏了 / server 好了：reload 应只报 'servers'，不误报 'config'。"""
    w = ConfigWatcher(cfg_path, srv_path)
    _touch(cfg_path, "interval: [60\n")           # 坏
    _touch(srv_path, "hosts: []\n")               # 好
    assert w.reload() == "servers"
    assert w.cfg.get("interval") == 30            # 旧值
    assert w.server_cfg == {"hosts": []}
