"""进程入口。

只支持容器常驻部署：进程启动后按 ``config.interval`` 周期循环跑检测。
``--config`` / ``--servers`` 仅用于让运维在容器外也能跑（基本不会用）。

用法：
    python monitor.py                    # 用默认 config.yaml / server.yaml
    python monitor.py --config /etc/x.yaml --servers /etc/y.yaml
"""
from __future__ import annotations

import argparse
import logging
import signal
import sys
import time
from datetime import datetime
from pathlib import Path

from database import Database
from detector import FpingDetector
from notifier import Notifier
from scheduler import Scheduler
from util import load_yaml, setup_logging

log = logging.getLogger("fping_monitor")


def build_components(cfg: dict, server_cfg: dict, db_path: str):
    """根据配置装配 Database + Detector + Notifier + Scheduler。"""
    db = Database(db_path)
    if server_cfg.get("hosts"):
        db.upsert_hosts(server_cfg["hosts"])
    fping_cfg = cfg.get("fping", {}) or {}
    detector = FpingDetector(
        count=int(fping_cfg.get("count", 1)),
        interval_ms=int(fping_cfg.get("interval_ms", 10)),
        timeout_ms=int(fping_cfg.get("timeout_ms", 500)),
        retry=int(fping_cfg.get("retry", 0)),
        extra=list(fping_cfg.get("extra") or []),
    )
    notifier = Notifier.from_config(cfg.get("notify", {}) or {})
    scheduler = Scheduler(cfg=cfg, db=db, detector=detector, notifier=notifier)
    return db, scheduler


def run_daemon(cfg: dict, server_cfg: dict) -> None:
    """常驻主循环：按 config.interval 周期跑检测，收到信号后优雅退出。"""
    db, scheduler = build_components(cfg, server_cfg, cfg.get("database", "state.db"))
    interval = int(cfg.get("interval", 30))
    log.info("进入常驻模式，间隔 %ss", interval)

    stop = {"flag": False}

    def _stop(*_):
        log.info("收到停止信号，本周期结束后退出")
        stop["flag"] = True
    signal.signal(signal.SIGINT, _stop)
    signal.signal(signal.SIGTERM, _stop)

    while not stop["flag"]:
        try:
            res = scheduler.run_once()
            log.info("本周期完成：%d 个状态变更", len(res.changes))
        except Exception:                          # noqa: BLE001
            log.exception("本周期执行失败")
        # 拆成 1 秒 sleep 以便及时响应信号
        for _ in range(interval):
            if stop["flag"]:
                break
            time.sleep(1)
    log.info("常驻进程已退出")


def main(argv=None):
    """CLI 入口：解析参数 → 加载配置 → 启动常驻循环。"""
    parser = argparse.ArgumentParser(prog="fping_monitor")
    parser.add_argument("--config", default="config.yaml",
                        help="全局配置文件路径")
    parser.add_argument("--servers", default="server.yaml",
                        help="主机列表文件路径")
    args = parser.parse_args(argv)

    # 顺序固定：先 load 配置 → 再 init 日志 → 再启动循环
    cfg = load_yaml(args.config)
    logging_cfg = cfg.get("logging", {}) or {}
    setup_logging(
        level=logging_cfg.get("level", "INFO"),
        log_dir=logging_cfg.get("dir", "logs"),
        backup_days=int(logging_cfg.get("backup_days", 14)),
    )
    server_cfg = load_yaml(args.servers)

    run_daemon(cfg, server_cfg)


if __name__ == "__main__":
    main()
