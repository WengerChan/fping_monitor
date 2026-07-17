"""进程入口。

只支持容器常驻部署：进程启动后按 ``config.interval`` 周期循环跑检测。
**配置和主机列表支持热加载**：

  * 每轮循环开头用 mtime 检查 config.yaml / server.yaml，变了就重读
  * 收到 ``SIGHUP`` 信号立刻强制 reload（无需等下一轮）
  * 变更后自动重建检测器/通知器/状态机，并把新主机列表同步进数据库

用法：
    python monitor.py                            # 默认配置
    python monitor.py --config /etc/x.yaml --servers /etc/y.yaml
    docker kill -s HUP fping-monitor             # 容器里手动触发重载
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
from detector import Detector, FpingDetector
from notifier import Notifier
from scheduler import Scheduler
from util import ConfigWatcher, load_yaml, setup_logging

log = logging.getLogger("fping_monitor")


# ---------------------------------------------------------------------------
# 组件构建：从 cfg 装配 Detector + Notifier + Scheduler
# ---------------------------------------------------------------------------


def build_scheduler(cfg: dict, db: Database) -> Tuple[Scheduler, Notifier, Detector]:
    """根据当前 cfg 构建 Scheduler，配置变更时会重新调用。

    返回 ``(scheduler, notifier, detector)`` —— notifier/detector 也单独返回，
    方便测试时验证是否真的重建了。
    """
    fping_cfg = cfg.get("fping", {}) or {}
    detector: Detector = FpingDetector(
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
    """根据 cfg 重新初始化 logging（幂等：handler 不重复挂载，level 始终更新）。"""
    logging_cfg = cfg.get("logging", {}) or {}
    setup_logging(
        level=logging_cfg.get("level", "INFO"),
        log_dir=logging_cfg.get("dir", "logs"),
        backup_days=int(logging_cfg.get("backup_days", 14)),
    )


# ---------------------------------------------------------------------------
# 常驻主循环
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

    def _reload_now(*_):
        log.info("收到 SIGHUP，强制重载配置")
        watcher.reload(force=True)
    def _stop(*_):
        log.info("收到停止信号，本周期结束后退出")
        stop["flag"] = True

    signal.signal(signal.SIGINT, _stop)
    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGHUP, _reload_now)

    while not stop["flag"]:
        # 1) 检查配置变更
        changed = watcher.reload()
        if changed in ("config", "all"):
            log.info("config.yaml 变更，重建检测器/通知器/状态机")
            init_logging_from_cfg(watcher.cfg)
            scheduler, notifier, _ = build_scheduler(watcher.cfg, db)
        if changed in ("servers", "all"):
            hosts = watcher.server_cfg.get("hosts", []) or []
            log.info("server.yaml 变更，同步 %d 台主机", len(hosts))
            db.upsert_hosts(hosts)

        # 2) 跑一轮检测
        try:
            res = scheduler.run_once()
            log.info("本周期完成：%d 个状态变更", len(res.changes))
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

    log.info("常驻进程已退出")


# ---------------------------------------------------------------------------
# CLI 入口
# ---------------------------------------------------------------------------


def main(argv: Optional[list] = None) -> None:
    """CLI 入口：解析参数 → 启动常驻循环（热加载由 run_daemon 内部处理）。"""
    parser = argparse.ArgumentParser(
        prog="fping_monitor",
        description="fping-monitor 长驻容器主程序，支持配置热加载。",
    )
    parser.add_argument("--config", default="config.yaml",
                        help="全局配置文件路径")
    parser.add_argument("--servers", default="server.yaml",
                        help="主机列表文件路径")
    args = parser.parse_args(argv)

    # 启动前先做一次"语法级"预检，避免热加载时才发现配置写错
    try:
        load_yaml(args.config)
        load_yaml(args.servers)
    except Exception as e:
        # 此时 logger 还没初始化，直接打 stderr
        print(f"配置加载失败：{e}", file=sys.stderr)
        sys.exit(2)

    run_daemon(args.config, args.servers)


if __name__ == "__main__":
    main()
