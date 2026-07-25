"""Tests for the standalone healthcheck script (Docker HEALTHCHECK 入口)."""
from __future__ import annotations

import sqlite3

import healthcheck


# ---- 全健康 -----------------------------------------------------------------


def test_main_returns_zero_when_all_ok(monkeypatch, tmp_path, capsys):
    cfg = tmp_path / "config.yaml"
    cfg.write_text("database: data/state.db\nhealthcheck:\n  gateway: 1.1.1.1\n")
    monkeypatch.setattr(healthcheck, "CONFIG_PATH", cfg)
    monkeypatch.setattr(healthcheck, "_check_db", lambda p: None)
    monkeypatch.setattr(healthcheck, "_check_fping", lambda g: None)

    assert healthcheck.main() == 0
    out = capsys.readouterr()
    assert "OK" in out.out


# ---- DB 不健康 --------------------------------------------------------------


def test_main_returns_one_when_db_fails(monkeypatch, tmp_path, capsys):
    cfg = tmp_path / "config.yaml"
    cfg.write_text("database: data/state.db\n")
    monkeypatch.setattr(healthcheck, "CONFIG_PATH", cfg)
    monkeypatch.setattr(healthcheck, "_check_db", lambda p: "db: unable to open")
    monkeypatch.setattr(healthcheck, "_check_fping", lambda g: None)

    assert healthcheck.main() == 1
    err = capsys.readouterr().err
    assert "UNHEALTHY" in err
    assert "db: unable to open" in err


def test_check_db_returns_error_for_missing_dir(tmp_path):
    """路径指向不存在目录时，_check_db 应返回失败原因（不抛异常）。"""
    bad = tmp_path / "no-such-dir" / "state.db"
    result = healthcheck._check_db(str(bad))
    assert result is not None
    assert result.startswith("db:")


def test_check_db_ok_for_real_file(tmp_path):
    db = tmp_path / "ok.db"
    sqlite3.connect(db).close()
    assert healthcheck._check_db(str(db)) is None


# ---- fping 不健康 -----------------------------------------------------------


def test_main_returns_one_when_fping_fails(monkeypatch, tmp_path, capsys):
    cfg = tmp_path / "config.yaml"
    cfg.write_text("database: data/state.db\nhealthcheck:\n  gateway: 1.1.1.1\n")
    monkeypatch.setattr(healthcheck, "CONFIG_PATH", cfg)
    monkeypatch.setattr(healthcheck, "_check_db", lambda p: None)
    monkeypatch.setattr(
        healthcheck, "_check_fping",
        lambda g: f"fping: cannot reach {g}",
    )

    assert healthcheck.main() == 1
    err = capsys.readouterr().err
    assert "UNHEALTHY" in err
    assert "fping: cannot reach" in err


def test_main_returns_one_when_both_fail(monkeypatch, tmp_path, capsys):
    cfg = tmp_path / "config.yaml"
    cfg.write_text("database: data/state.db\n")
    monkeypatch.setattr(healthcheck, "CONFIG_PATH", cfg)
    monkeypatch.setattr(healthcheck, "_check_db", lambda p: "db: read-only")
    monkeypatch.setattr(healthcheck, "_check_fping", lambda g: "fping: down")

    assert healthcheck.main() == 1
    err = capsys.readouterr().err
    assert "db: read-only" in err
    assert "fping: down" in err


# ---- 配置默认值 -------------------------------------------------------------


def test_default_gateway_when_no_config(monkeypatch, tmp_path):
    """config.yaml 不存在时，gateway 应有默认 1.1.1.1。"""
    monkeypatch.setattr(healthcheck, "CONFIG_PATH", tmp_path / "missing.yaml")
    monkeypatch.setattr(healthcheck, "_check_db", lambda p: None)
    seen = []
    monkeypatch.setattr(
        healthcheck, "_check_fping",
        lambda g: (seen.append(g) or None),
    )
    assert healthcheck.main() == 0
    assert seen == ["1.1.1.1"]


def test_load_cfg_returns_empty_when_file_missing(monkeypatch, tmp_path):
    monkeypatch.setattr(healthcheck, "CONFIG_PATH", tmp_path / "missing.yaml")
    assert healthcheck._load_cfg() == {}


def test_load_cfg_returns_empty_when_yaml_invalid(monkeypatch, tmp_path):
    bad = tmp_path / "bad.yaml"
    bad.write_text(": not valid yaml :")
    monkeypatch.setattr(healthcheck, "CONFIG_PATH", bad)
    assert healthcheck._load_cfg() == {}
