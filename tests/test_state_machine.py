"""End-to-end state machine tests through Scheduler."""
from datetime import datetime, timezone

import pytest

from models import EventType, Host, HostStatus
from notifier import Notifier
from scheduler import StateMachine


class FakeNotifier(Notifier):
    def __init__(self):
        self.downs = []
        self.recovers = []
        self.channels = []

    def notify_down(self, host):
        self.downs.append(host.name)

    def notify_recover(self, host):
        self.recovers.append(host.name)


def _seed(db, name="h1", ip="8.8.8.8", status=HostStatus.UNKNOWN,
          fail=0, rec=0):
    db.upsert_hosts([{"name": name, "ip": ip}])
    h = db.get_host_by_name(name)
    if status != HostStatus.UNKNOWN or fail or rec:
        db.update_host_state(
            h.id, status=status, fail_count=fail, recover_count=rec,
            last_check=datetime.now(timezone.utc), last_change=datetime.now(timezone.utc),
        )
    return db.get_host_by_name(name)


def test_unknown_to_up_no_notify(db):
    n = FakeNotifier()
    sm = StateMachine(db, n, failure_threshold=3, recovery_threshold=2)
    _seed(db, status=HostStatus.UNKNOWN)
    res = sm.step({"h1": True})
    assert res.changes == [{"host": "h1", "from": "UNKNOWN", "to": "UP"}]
    assert n.downs == [] and n.recovers == []
    assert db.get_host_by_name("h1").status == HostStatus.UP


def test_unknown_to_unknown_no_notify(db):
    n = FakeNotifier()
    sm = StateMachine(db, n, failure_threshold=3, recovery_threshold=2)
    _seed(db, status=HostStatus.UNKNOWN)
    sm.step({"h1": False})
    h = db.get_host_by_name("h1")
    assert h.status == HostStatus.UNKNOWN
    assert h.fail_count == 1
    assert n.downs == [] and n.recovers == []


def test_up_to_down_only_after_threshold(db):
    n = FakeNotifier()
    sm = StateMachine(db, n, failure_threshold=3, recovery_threshold=2)
    _seed(db, status=HostStatus.UP)

    sm.step({"h1": False})
    assert db.get_host_by_name("h1").status == HostStatus.UP
    sm.step({"h1": False})
    assert db.get_host_by_name("h1").status == HostStatus.UP
    sm.step({"h1": False})      # third failure — threshold met
    h = db.get_host_by_name("h1")
    assert h.status == HostStatus.DOWN
    assert n.downs == ["h1"]


def test_down_to_up_only_after_threshold(db):
    n = FakeNotifier()
    sm = StateMachine(db, n, failure_threshold=3, recovery_threshold=2)
    _seed(db, status=HostStatus.DOWN, fail=3)

    sm.step({"h1": True})
    assert db.get_host_by_name("h1").status == HostStatus.DOWN
    sm.step({"h1": True})       # second success — threshold met
    h = db.get_host_by_name("h1")
    assert h.status == HostStatus.UP
    assert n.recovers == ["h1"]


def test_flap_does_not_recover(db):
    n = FakeNotifier()
    sm = StateMachine(db, n, failure_threshold=3, recovery_threshold=2)
    _seed(db, status=HostStatus.DOWN, fail=3)

    sm.step({"h1": True})        # recover_count = 1
    sm.step({"h1": False})       # fail_count = 1, recover_count reset
    sm.step({"h1": True})        # recover_count = 1
    sm.step({"h1": True})        # recover_count = 2 -> UP
    assert db.get_host_by_name("h1").status == HostStatus.UP


def test_event_row_written_on_transition(db):
    n = FakeNotifier()
    sm = StateMachine(db, n, failure_threshold=2, recovery_threshold=1)
    _seed(db, status=HostStatus.UP)

    sm.step({"h1": False})       # DOWN
    sm.step({"h1": False})       # still DOWN, no event
    sm.step({"h1": True})        # UP
    events = db.recent_events(10)
    kinds = [e["event"] for e in events]
    assert kinds == ["RECOVER", "DOWN"]


def test_missing_host_treated_as_down(db):
    n = FakeNotifier()
    sm = StateMachine(db, n, failure_threshold=2, recovery_threshold=1)
    _seed(db, status=HostStatus.UP, ip="8.8.8.8")

    # Detector returned no entry for h1
    sm.step({})
    sm.step({})
    h = db.get_host_by_name("h1")
    assert h.status == HostStatus.DOWN
    assert n.downs == ["h1"]
