"""SQLite round-trip tests."""
from datetime import datetime

from database import _decode_tags, _encode_tags
from models import EventType, HostStatus


def test_schema_initialised(db):
    rows = db.list_hosts()
    assert rows == []


def test_upsert_then_update(db):
    db.upsert_hosts([{"name": "a", "ip": "1.1.1.1"}])
    h = db.get_host_by_name("a")
    assert h.ip == "1.1.1.1"
    assert h.status == HostStatus.UNKNOWN
    assert h.tags == []

    # Re-upsert with new IP — should update, not insert duplicate
    db.upsert_hosts([{"name": "a", "ip": "2.2.2.2"}])
    h = db.get_host_by_name("a")
    assert h.ip == "2.2.2.2"
    assert len(db.list_hosts()) == 1


def test_update_host_state(db):
    db.upsert_hosts([{"name": "a", "ip": "1.1.1.1"}])
    h = db.get_host_by_name("a")
    now = datetime(2026, 1, 1, 12, 0, 0)
    db.update_host_state(
        h.id, status=HostStatus.DOWN,
        fail_count=3, recover_count=0,
        last_check=now, last_change=now,
    )
    h = db.get_host_by_name("a")
    assert h.status == HostStatus.DOWN
    assert h.fail_count == 3
    assert h.last_check == now


def test_events_persisted(db):
    db.upsert_hosts([{"name": "a", "ip": "1.1.1.1"}])
    h = db.get_host_by_name("a")
    db.insert_event(h.id, EventType.DOWN, "lost", at=datetime(2026, 1, 1))
    db.insert_event(h.id, EventType.RECOVER, "back", at=datetime(2026, 1, 2))
    events = db.recent_events(10)
    assert [e["event"] for e in events] == ["RECOVER", "DOWN"]
    assert events[0]["host_name"] == "a"


# ---- tags 字段 --------------------------------------------------------------


def test_upsert_persists_tags(db):
    db.upsert_hosts([{"name": "a", "ip": "1.1.1.1", "tags": ["prod", "db"]}])
    h = db.get_host_by_name("a")
    assert h.tags == ["prod", "db"]


def test_upsert_updates_tags(db):
    db.upsert_hosts([{"name": "a", "ip": "1.1.1.1", "tags": ["prod"]}])
    db.upsert_hosts([{"name": "a", "ip": "1.1.1.1", "tags": ["staging", "shanghai"]}])
    h = db.get_host_by_name("a")
    assert h.tags == ["staging", "shanghai"]


def test_upsert_without_tags_defaults_to_empty(db):
    db.upsert_hosts([{"name": "a", "ip": "1.1.1.1"}])
    assert db.get_host_by_name("a").tags == []


def test_encode_decode_tags():
    assert _encode_tags([]) == ""
    assert _encode_tags(["a", "b"]) == "a,b"
    assert _encode_tags([" a ", "", "b "]) == "a,b"
    assert _decode_tags("") == []
    assert _decode_tags(None) == []
    assert _decode_tags("a,b,c") == ["a", "b", "c"]
    assert _decode_tags(" a , b , ") == ["a", "b"]
