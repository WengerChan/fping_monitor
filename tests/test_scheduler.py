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
