"""状态机 + 单次检测调度。

状态转换表：
    UNKNOWN + 可达              -> UP     ，不通知
    UNKNOWN + 不可达            -> UNKNOWN，仅累加失败计数，不通知（首次发现静默）
    UP      + 可达              -> UP     ，计数器重置
    UP      + 不可达            -> UP     ，失败计数 +1；达到阈值则 DOWN + 通知
    DOWN    + 可达              -> DOWN   ，恢复计数 +1；达到阈值则 UP + 通知
    DOWN    + 不可达            -> DOWN   ，失败计数 +1，不通知

状态机与 fping 解耦：只要传入的 ``Detector`` 实现
``detect() -> {name: bool}``，可以替换为 TCP / HTTP / SSH 检测而无需改核心。
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Dict, List

from database import Database
from detector import Detector
from models import EventType, Host, HostStatus
from notifier import Notifier

log = logging.getLogger("fping_monitor.scheduler")


@dataclass
class CycleResult:
    """一次检测周期的汇总，供测试和报告使用。"""
    timestamp: datetime
    results: Dict[str, bool]              # name -> 本轮是否可达
    changes: List[Dict]                   # 本周期内发生跃迁的主机


class StateMachine:
    """纯函数式状态机：输入 (host, is_alive)，输出 (新状态, 计数器, fired)。

    fired 表示本周期是否发生了状态跃迁（即是否需要写 events + 发通知）。
    """

    def __init__(self, db: Database, notifier: Notifier,
                 failure_threshold: int, recovery_threshold: int):
        self.db = db
        self.notifier = notifier
        self.failure_threshold = failure_threshold
        self.recovery_threshold = recovery_threshold

    def step(self, alive: Dict[str, bool]) -> CycleResult:
        """对所有主机跑一轮状态机。"""
        now = datetime.utcnow()
        changes: List[Dict] = []
        for host in self.db.list_hosts():
            old_status = host.status
            is_alive = bool(alive.get(host.name, False))
            new_status, fail_count, recover_count, fired = self._advance(
                host, is_alive, now
            )
            self.db.update_host_state(
                host.id, status=new_status,
                fail_count=fail_count, recover_count=recover_count,
                last_check=now,
                last_change=now if fired else None,
            )
            if fired:
                # events 表总是写入；通知仅在"非 UNKNOWN 起点"的跃迁时触发
                # （避免首次发现时对历史未告警状态也发出告警）
                kind = (EventType.DOWN
                        if new_status == HostStatus.DOWN
                        else EventType.RECOVER)
                msg = (f"{host.name} ({host.ip}) "
                       f"{old_status.value} -> {new_status.value}")
                self.db.insert_event(host.id, kind, msg, at=now)
                if old_status != HostStatus.UNKNOWN:
                    if kind == EventType.DOWN:
                        self.notifier.notify_down(host)
                    else:
                        self.notifier.notify_recover(host)
                changes.append({
                    "host": host.name,
                    "from": old_status.value,
                    "to": new_status.value,
                })
                log.info("状态变更",
                         extra={"event": "state_change",
                                "host": host.name,
                                "ip": host.ip,
                                "tags": list(host.tags),
                                "from_status": old_status.value,
                                "to_status": new_status.value,
                                "fired_kind": kind.value})
        return CycleResult(timestamp=now, results=alive, changes=changes)

    def _advance(self, host: Host, is_alive: bool, now: datetime
                 ) -> tuple[HostStatus, int, int, bool]:
        """计算单台主机的下一状态。

        返回：(新状态, 失败计数, 恢复计数, fired)
        fired=True 表示本次发生了状态跃迁。
        """
        s = host.status
        fc, rc = host.fail_count, host.recover_count

        if s == HostStatus.UNKNOWN:
            if is_alive:
                # 首次发现可达 → UP
                return HostStatus.UP, 0, 0, True
            # 首次发现不可达 → 继续 UNKNOWN，仅累加失败计数，不通知
            return HostStatus.UNKNOWN, fc + 1, 0, False

        if s == HostStatus.UP:
            if is_alive:
                # 保持 UP，重置两个计数器
                return HostStatus.UP, 0, 0, False
            fc += 1
            if fc >= self.failure_threshold:
                return HostStatus.DOWN, fc, 0, True
            return HostStatus.UP, fc, 0, False

        # s == DOWN
        if is_alive:
            rc += 1
            if rc >= self.recovery_threshold:
                return HostStatus.UP, 0, rc, True
            return HostStatus.DOWN, 0, rc, False
        # 持续不可达：累加失败计数
        return HostStatus.DOWN, fc + 1, 0, False


class Scheduler:
    """单次执行入口：调用方决定调度节奏（systemd timer / cron）。"""

    def __init__(self, cfg: dict, db: Database, detector: Detector,
                 notifier: Notifier):
        self.cfg = cfg
        self.db = db
        self.detector = detector
        self.notifier = notifier
        self.sm = StateMachine(
            db=db,
            notifier=notifier,
            failure_threshold=int(cfg.get("failure_threshold", 3)),
            recovery_threshold=int(cfg.get("recovery_threshold", 2)),
        )

    def run_once(self) -> CycleResult:
        """跑一轮完整的检测 → 状态机 → 通知流程。"""
        hosts = self.db.list_hosts()
        if not hosts:
            log.warning("没有配置主机，本轮跳过")
            return CycleResult(timestamp=datetime.utcnow(),
                               results={}, changes=[])
        alive = self.detector.detect(hosts)
        # 检测结果单独打一条 JSON，方便按 host 在 ES 里检索
        log.info("检测结果",
                 extra={"event": "detection",
                        "results": {h.name: alive.get(h.name) for h in hosts}})
        return self.sm.step(alive)
