"""进程入口。

启动 fping-monitor 的长驻主循环，监控 ``conf/server.yaml`` 里的主机，
按 ``config.interval`` 周期跑 fping 检测并按状态机推进 / 通知。
"""
from __future__ import annotations

import argparse
import logging
import signal
import sys
import time
from typing import Optional, Tuple, cast

from database import Database
from detector import FpingDetector
from notifier import NotifyConfig, Notifier
from scheduler import Scheduler
from util import ConfigWatcher, load_yaml, setup_logging

log = logging.getLogger("fping_monitor")


# ---------------------------------------------------------------------------
# 组件构建
# ---------------------------------------------------------------------------


def build_detector(cfg: dict) -> FpingDetector:
    """根据 ``cfg.fping`` 构造 FpingDetector。

    拆分理由：cfg.fping 变了只需重建 detector，不需要重建 Notifier / Scheduler。
    """
    fping_cfg = cfg.get("fping", {}) or {}
    return FpingDetector(
        count=int(fping_cfg.get("count", 1)),
        interval_ms=int(fping_cfg.get("interval_ms", 10)),
        timeout_ms=int(fping_cfg.get("timeout_ms", 500)),
        retry=int(fping_cfg.get("retry", 0)),
        extra=list(fping_cfg.get("extra") or []),
    )


def build_notifier(cfg: dict) -> Notifier:
    """根据 ``cfg.notify`` 构造 Notifier（含异步派发 / 防抖配置）。

    ``cfg`` 是从 YAML 解析出来的 ``dict``，运行时类型是 ``Any``。
    这里 ``cast`` 到 ``NotifyConfig`` 仅给 mypy 一个承诺：传入的字段
    形状合法。真正的字段校验在 ``Notifier.from_config`` 内部
    （缺字段会让 dataclass ``__init__`` 抛 TypeError 然后被吞并 warning）。
    """
    return Notifier.from_config(cast(NotifyConfig, cfg.get("notify", {}) or {}))


def build_scheduler(cfg: dict, db: Database,
                    detector: Optional[FpingDetector] = None,
                    notifier: Optional[Notifier] = None
                    ) -> Tuple[Scheduler, Notifier, FpingDetector]:
    """根据 cfg 构建 Scheduler / Detector / Notifier。

    多数调用方直接传 ``cfg, db``，由本函数内部用 ``build_*`` 构造组件；
    也支持显式传入组件（譬如想复用上一轮的 Notifier，只换 detector）。
    """
    if detector is None:
        detector = build_detector(cfg)
    if notifier is None:
        notifier = build_notifier(cfg)
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
# 长驻主循环
# ---------------------------------------------------------------------------


_MIN_INTERVAL = 1       # 防止 sleep 循环 spin；下限 1s
_MAX_INTERVAL = 86400    # 防止配错写到 86400000；上限 1 天


def _coerce_interval(raw) -> int:
    """把 ``cfg.interval`` 强制夹到 ``[_MIN_INTERVAL, _MAX_INTERVAL]``。

    * 非数字 / 缺失 → 使用 30 并打 WARNING
    * 越界 → 夹到边界 + WARNING
    """
    try:
        v = int(raw)
    except (TypeError, ValueError):
        log.warning("interval 配置非法（%r），回退到 30s", raw)
        return 30
    if v < _MIN_INTERVAL:
        log.warning("interval=%d 太小，夹到 %d", v, _MIN_INTERVAL)
        return _MIN_INTERVAL
    if v > _MAX_INTERVAL:
        log.warning("interval=%d 太大，夹到 %d", v, _MAX_INTERVAL)
        return _MAX_INTERVAL
    return v


def run_daemon(config_path: str, servers_path: str) -> None:
    """长驻主循环：按 config.interval 周期检测，配置变更自动热加载。"""
    watcher = ConfigWatcher(config_path, servers_path)
    init_logging_from_cfg(watcher.cfg)

    # daemon 是单线程 SQLite 写者，复用长连接省每次 1~2ms 的 open/close
    db = Database(watcher.cfg.get("database", "state.db"),
                  use_long_connection=True)
    if watcher.server_cfg.get("hosts"):
        db.upsert_hosts(watcher.server_cfg["hosts"])
    scheduler, notifier, _ = build_scheduler(watcher.cfg, db)

    # 信号处理：SIGHUP 触发立即重载，SIGINT/SIGTERM 优雅退出
    stop = {"flag": False}

    def _reload_now(sig, frame):
        log.info("收到 SIGHUP，强制重载配置", extra={"signal": "SIGHUP"})
        watcher.reload(force=True)
    current_notifier = {"ref": notifier}

    def _stop(sig, frame):
        log.info("收到停止信号，本周期结束后退出",
                 extra={"signal": signal.Signals(sig).name})
        stop["flag"] = True

    def _shutdown_notifier():
        """把当前 notifier 的后台线程池 join 掉再退出，保证 webhook 发完。"""
        n = current_notifier["ref"]
        if n is not None:
            try:
                n.close(wait=True)
            except Exception:                  # noqa: BLE001
                log.exception("关闭 notifier 失败")

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
            # 重建前先把旧 notifier 的后台线程 join 掉，避免 webhook 还在跑被换掉
            current_notifier["ref"].close(wait=False)
            scheduler, notifier, _ = build_scheduler(watcher.cfg, db)
            current_notifier["ref"] = notifier
        if changed in ("servers", "all"):
            raw_hosts = watcher.server_cfg.get("hosts", []) or []
            # 先 upsert 新主机，再算差集删除 YAML 里没了的（不删除的代价是
            # 状态机永远跑着已经下线的僵尸主机，events 表也会持续被写）。
            db.upsert_hosts(raw_hosts)
            db_names = set(db.host_names())
            yaml_names = set()
            for h in raw_hosts:
                name = h.get("name")
                if not name:
                    continue
                ip_field = h.get("ip")
                if isinstance(ip_field, list):
                    specs = [str(s) for s in ip_field]
                else:
                    specs = [str(ip_field)] if ip_field else []
                if len(specs) == 1:
                    yaml_names.add(name)
                else:
                    from util import expand_ip_spec
                    expanded = []
                    for s in specs:
                        try:
                            expanded.extend(expand_ip_spec(s))
                        except ValueError:
                            continue  # 单条 spec 坏掉不影响整体同步
                    yaml_names.update(f"{name}-{ip}" for ip in expanded)
            stale = sorted(db_names - yaml_names)
            if stale:
                deleted = db.delete_hosts(stale)
                log.info("server.yaml 中移除的主机已清理",
                         extra={"event": "hosts_pruned",
                                "deleted": deleted,
                                "names": stale})
            log.info("server.yaml 变更，同步主机",
                     extra={"changed": "servers", "hosts": len(raw_hosts)})

        # 2) 跑一轮检测
        try:
            res = scheduler.run_once()
            log.info("本周期完成",
                     extra={"cycle_changes": len(res.changes),
                            "alive_count": sum(1 for v in res.results.values() if v)})
        except Exception:                          # noqa: BLE001
            log.exception("本周期执行失败")

        # 3) sleep 时长按当前 cfg.interval（变更后下一轮立刻生效）。
        #    interval 取下限 1，防止配置写 0/负数导致本循环 spin 空 CPU。
        if stop["flag"]:
            break
        interval = _coerce_interval(watcher.cfg.get("interval", 30))
        for _ in range(interval):
            if stop["flag"]:
                break
            time.sleep(1)

    _shutdown_notifier()
    db.close()
    log.info("常驻进程已退出", extra={"reason": "signal"})


# ---------------------------------------------------------------------------
# CLI 入口
# ---------------------------------------------------------------------------


def main(argv: Optional[list] = None) -> None:
    """CLI 入口：启动 fping-monitor 长驻主循环。"""
    parser = argparse.ArgumentParser(
        prog="fping_monitor",
        description="fping-monitor 长驻容器主程序，支持配置热加载。",
    )
    parser.add_argument("--config", default="conf/config.yaml",
                        help="全局配置文件路径")
    parser.add_argument("--servers", default="conf/server.yaml",
                        help="主机列表文件路径")

    args = parser.parse_args(argv)

    # 启动前预检一次，避免配置写错时容器无限重启
    try:
        load_yaml(args.servers)
    except Exception as e:
        print(f"配置加载失败：{e}", file=sys.stderr)
        sys.exit(2)

    run_daemon(args.config, args.servers)


if __name__ == "__main__":
    main()
