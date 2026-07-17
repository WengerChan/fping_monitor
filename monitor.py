"""进程入口。

支持两个运行模式：

  * ``python monitor.py``（默认）—— 长驻主循环
  * ``python monitor.py healthcheck`` —— 一次性健康检查，0 表示健康

健康检查内容（给 docker HEALTHCHECK 用）：
    1. 能否打开 SQLite 数据库
    2. fping 工具链是否可用（用 fping 探一次 config.healthcheck.gateway）
"""
from __future__ import annotations

import argparse
import logging
import signal
import sys
import time
from pathlib import Path
from typing import Optional, Tuple

from database import Database
from detector import FpingDetector
from notifier import Notifier
from scheduler import Scheduler
from util import ConfigWatcher, load_yaml, setup_logging

log = logging.getLogger("fping_monitor")


# ---------------------------------------------------------------------------
# 组件构建
# ---------------------------------------------------------------------------


def build_scheduler(cfg: dict, db: Database) -> Tuple[Scheduler, Notifier, FpingDetector]:
    """根据当前 cfg 构建 Scheduler，配置变更时会重新调用。"""
    fping_cfg = cfg.get("fping", {}) or {}
    detector: FpingDetector = FpingDetector(
        count=int(fping_cfg.get("count", 1)),
        interval_ms=int(fping_cfg.get("interval_ms", 10)),
        timeout_ms=int(fping_cfg.get("timeout_ms", 500)),
        retry=int(fping_cfg.get("retry", 0)),
        extra=list(fping_cfg.get("extra") or []),
    )
    notifier = Notifier.from_config(cfg.get("notify", {}) or {})
    scheduler = Scheduler(cfg=cfg, db=db, detector=detector, notifier=notifier)
    return scheduler, notifier, detector


def init_logging_from_cfg(cfg: dict) -> None:
    """根据 cfg 重新初始化 logging（重建 handler 让 format 变化也能立即生效）。"""
    logging_cfg = cfg.get("logging", {}) or {}
    setup_logging(
        level=logging_cfg.get("level", "INFO"),
        log_dir=logging_cfg.get("dir", "logs"),
        backup_days=int(logging_cfg.get("backup_days", 14)),
        fmt=logging_cfg.get("format", "json"),
    )


# ---------------------------------------------------------------------------
# 健康检查：CLI 子命令形式，给 docker HEALTHCHECK 用
# ---------------------------------------------------------------------------


def run_healthcheck(cfg: dict) -> int:
    """返回 0 = 健康，1 = 不健康。

    检查项：
        1. SQLite 能打开
        2. fping 能探到 healthcheck.gateway 指定的地址
    """
    failures: list[str] = []

    # 1) SQLite 连通性
    try:
        db_path = cfg.get("database", "state.db")
        Database(db_path).list_hosts()           # 触发一次真实读写
    except Exception as e:                        # noqa: BLE001
        failures.append(f"db: {e}")

    # 2) fping 探活
    gateway = (cfg.get("healthcheck") or {}).get("gateway", "1.1.1.1")
    try:
        det = FpingDetector(timeout_ms=500, retry=0)
        # 造一个临时 host 测一次，不入库
        from models import Host
        alive = det.detect([Host(name="__hc__", ip=gateway)])
        if not alive.get("__hc__"):
            failures.append(f"fping: cannot reach {gateway}")
    except Exception as e:                        # noqa: BLE001
        failures.append(f"fping: {e}")

    if failures:
        print("UNHEALTHY: " + "; ".join(failures), file=sys.stderr)
        return 1
    print("OK")
    return 0


# ---------------------------------------------------------------------------
# 长驻主循环
# ---------------------------------------------------------------------------


def run_daemon(config_path: str, servers_path: str) -> None:
    """长驻主循环：按 config.interval 周期检测，配置变更自动热加载。"""
    watcher = ConfigWatcher(config_path, servers_path)
    init_logging_from_cfg(watcher.cfg)

    db = Database(watcher.cfg.get("database", "state.db"))
    if watcher.server_cfg.get("hosts"):
        db.upsert_hosts(watcher.server_cfg["hosts"])
    scheduler, notifier, _ = build_scheduler(watcher.cfg, db)

    # 信号处理：SIGHUP 触发立即重载，SIGINT/SIGTERM 优雅退出
    stop = {"flag": False}

    def _reload_now(sig, frame):
        log.info("收到 SIGHUP，强制重载配置", extra={"signal": "SIGHUP"})
        watcher.reload(force=True)
    def _stop(sig, frame):
        log.info("收到停止信号，本周期结束后退出",
                 extra={"signal": signal.Signals(sig).name})
        stop["flag"] = True

    signal.signal(signal.SIGINT, _stop)
    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGHUP, _reload_now)

    while not stop["flag"]:
        # 1) 检查配置变更
        changed = watcher.reload()
        if changed in ("config", "all"):
            log.info("config.yaml 变更，重建检测器/通知器/状态机",
                     extra={"changed": "config"})
            init_logging_from_cfg(watcher.cfg)
            scheduler, notifier, _ = build_scheduler(watcher.cfg, db)
        if changed in ("servers", "all"):
            hosts = watcher.server_cfg.get("hosts", []) or []
            log.info("server.yaml 变更，同步主机",
                     extra={"changed": "servers", "hosts": len(hosts)})
            db.upsert_hosts(hosts)

        # 2) 跑一轮检测
        try:
            res = scheduler.run_once()
            log.info("本周期完成",
                     extra={"cycle_changes": len(res.changes),
                            "alive_count": sum(1 for v in res.results.values() if v)})
        except Exception:                          # noqa: BLE001
            log.exception("本周期执行失败")

        # 3) sleep 时长按当前 cfg.interval（变更后下一轮立刻生效）
        if stop["flag"]:
            break
        interval = int(watcher.cfg.get("interval", 30))
        for _ in range(interval):
            if stop["flag"]:
                break
            time.sleep(1)

    log.info("常驻进程已退出", extra={"reason": "signal"})


# ---------------------------------------------------------------------------
# CLI 入口
# ---------------------------------------------------------------------------


def main(argv: Optional[list] = None) -> None:
    """CLI 入口：默认长驻；``healthcheck`` 子命令用于 docker HEALTHCHECK。"""
    parser = argparse.ArgumentParser(
        prog="fping_monitor",
        description="fping-monitor 长驻容器主程序，支持配置热加载。",
    )
    parser.add_argument("--config", default="conf/config.yaml",
                        help="全局配置文件路径")
    parser.add_argument("--servers", default="conf/server.yaml",
                        help="主机列表文件路径")

    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("run", help="（默认）启动长驻主循环")
    sub.add_parser("healthcheck", help="一次性健康检查，0=健康 1=不健康")

    args = parser.parse_args(argv)

    cfg = load_yaml(args.config)

    if args.cmd == "healthcheck":
        sys.exit(run_healthcheck(cfg))

    # run：启动前预检一次，避免配置写错时容器无限重启
    try:
        load_yaml(args.servers)
    except Exception as e:
        print(f"配置加载失败：{e}", file=sys.stderr)
        sys.exit(2)

    run_daemon(args.config, args.servers)


if __name__ == "__main__":
    main()
