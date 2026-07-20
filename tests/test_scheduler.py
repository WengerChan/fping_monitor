"""Scheduler + Detector integration tests with a fake detector."""
from datetime import datetime

from detector import DetectResult
from models import EventType, HostStatus
from notifier import Notifier
from scheduler import Scheduler


class FakeDetector:
    """按队列返回结果；空队列时全 True。"""
    def __init__(self, scenarios):
        # scenarios 里每条是 dict[str, bool]，包成 DetectResult
        self._scenarios = list(scenarios)

    def detect(self, hosts):
        if not self._scenarios:
            alive = {h.name: True for h in hosts}
            return DetectResult(alive=alive, attempted=len(hosts),
                               reachable=len(hosts), duration_ms=0)
        alive = self._scenarios.pop(0)
        return DetectResult(
            alive=alive,
            attempted=len(hosts),
            reachable=sum(1 for v in alive.values() if v),
            duration_ms=10,
        )


class RecordingNotifier(Notifier):
    def __init__(self):
        self.channels = []
        self.events = []
    def notify_down(self, host): self.events.append(("DOWN", host.name))
    def notify_recover(self, host): self.events.append(("RECOVER", host.name))


def test_end_to_end_full_cycle(db):
    db.upsert_hosts([{"name": "up", "ip": "1.1.1.1"},
                     {"name": "down", "ip": "8.8.8.8"}])
    n = RecordingNotifier()
    det = FakeDetector([
        {"up": True, "down": True},     # cycle 1: bootstrap — both UP, no notify
        {"up": True, "down": True},     # cycle 2
        {"up": True, "down": False},    # cycle 3: down -> fail_count=1
        {"up": True, "down": False},    # cycle 4: fail_count=2
        {"up": True, "down": False},    # cycle 5: fail_count=3 -> DOWN, notify
        {"up": True, "down": False},    # cycle 6: stay DOWN
        {"up": True, "down": True},     # cycle 7: recover_count=1
        {"up": True, "down": True},     # cycle 8: recover_count=2 -> UP, notify
    ])
    sched = Scheduler(
        cfg={"failure_threshold": 3, "recovery_threshold": 2},
        db=db, detector=det, notifier=n,
    )
    for _ in range(8):
        sched.run_once()

    h_down = db.get_host_by_name("down")
    h_up = db.get_host_by_name("up")
    assert h_down.status == HostStatus.UP
    assert h_up.status == HostStatus.UP
    assert n.events == [("DOWN", "down"), ("RECOVER", "down")]


def test_no_hosts_is_noop(db):
    n = RecordingNotifier()
    sched = Scheduler(
        cfg={"failure_threshold": 1, "recovery_threshold": 1},
        db=db, detector=FakeDetector([]), notifier=n,
    )
    res = sched.run_once()
    assert res.changes == []
    assert n.events == []


# ---- detection 日志体积保护 --------------------------------------------------


def _capture_scheduler_logs(monkeypatch):
    """为 fping_monitor.scheduler 装一个 list-handler，返回 records 列表。

    caplog 看不到 ``fping_monitor`` 的子 logger，因为它的 propagate=False。
    测试场景下我们临时塞一个 handler 到子 logger 上就能拿到所有记录。
    """
    import logging
    captured = []
    handler = logging.Handler()
    handler.emit = lambda record: captured.append(record)
    sched_logger = logging.getLogger("fping_monitor.scheduler")
    sched_logger.addHandler(handler)
    monkeypatch.setattr(sched_logger, "propagate", False)   # 防重复
    try:
        return captured
    finally:
        # 实际清理在 monkeypatch tearDown 之后做，避免 list 引用丢失；
        # 但 handler 是真的会被 addHandler 的对象，所以手动 remove 更稳。
        pass


def test_detection_log_truncates_when_many_hosts(db, monkeypatch):
    """主机数 > 阈值时，detection 日志用 results_sample 替代全量 results。"""
    import logging
    from scheduler import _LOG_RESULTS_INLINE_THRESHOLD, _LOG_RESULTS_SAMPLE_SIZE
    from detector import DetectResult

    n_hosts = _LOG_RESULTS_INLINE_THRESHOLD + 50
    hosts_dict = [{"name": f"h{i}", "ip": f"10.0.0.{i}"} for i in range(n_hosts)]
    db.upsert_hosts(hosts_dict)

    class FullDetector:
        def detect(self, hosts):
            return DetectResult(
                alive={h.name: True for h in hosts},
                attempted=len(hosts), reachable=len(hosts),
            )

    n = RecordingNotifier()
    sched = Scheduler(
        cfg={"failure_threshold": 1, "recovery_threshold": 1},
        db=db, detector=FullDetector(), notifier=n,
    )

    captured = _capture_scheduler_logs(monkeypatch)
    sched.run_once()
    detection_logs = [r for r in captured if getattr(r, "event", None) == "detection"]
    assert len(detection_logs) == 1
    rec = detection_logs[0]
    assert getattr(rec, "results_truncated") is True
    sample = getattr(rec, "results_sample")
    assert isinstance(sample, dict)
    # down 抽样 + up 抽样不超过阈值
    assert len(sample) <= _LOG_RESULTS_SAMPLE_SIZE


def test_detection_log_inline_when_few_hosts(db, monkeypatch):
    """主机数 <= 阈值时仍打全量 results。"""
    from detector import DetectResult

    db.upsert_hosts([{"name": f"h{i}", "ip": f"10.0.0.{i}"} for i in range(3)])

    class FullDetector:
        def detect(self, hosts):
            return DetectResult(
                alive={h.name: True for h in hosts},
                attempted=len(hosts), reachable=len(hosts),
            )

    n = RecordingNotifier()
    sched = Scheduler(
        cfg={"failure_threshold": 1, "recovery_threshold": 1},
        db=db, detector=FullDetector(), notifier=n,
    )

    captured = _capture_scheduler_logs(monkeypatch)
    sched.run_once()
    detection_logs = [r for r in captured if getattr(r, "event", None) == "detection"]
    assert len(detection_logs) == 1
    rec = detection_logs[0]
    assert getattr(rec, "results", None) == {"h0": True, "h1": True, "h2": True}
    assert not hasattr(rec, "results_truncated")
