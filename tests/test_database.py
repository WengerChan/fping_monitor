"""SQLite round-trip tests."""
from datetime import datetime

from database import Database, _decode_tags, _encode_tags
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


# ---- delete_hosts -----------------------------------------------------------


def test_delete_hosts_removes_rows_and_events(db):
    db.upsert_hosts([{"name": "a", "ip": "1.1.1.1"},
                     {"name": "b", "ip": "2.2.2.2"}])
    h_a = db.get_host_by_name("a")
    db.insert_event(h_a.id, EventType.DOWN, "lost")
    assert db.recent_events(10)[0]["host_name"] == "a"

    deleted = db.delete_hosts(["a"])
    assert deleted == 1
    assert db.get_host_by_name("a") is None
    assert db.get_host_by_name("b") is not None
    # events 级联删除
    assert db.recent_events(10) == []


def test_delete_hosts_empty_input_is_noop(db):
    db.upsert_hosts([{"name": "a", "ip": "1.1.1.1"}])
    assert db.delete_hosts([]) == 0
    assert db.get_host_by_name("a") is not None


def test_delete_hosts_unknown_names_silent(db):
    db.upsert_hosts([{"name": "a", "ip": "1.1.1.1"}])
    assert db.delete_hosts(["does-not-exist"]) == 0
    assert db.get_host_by_name("a") is not None


def test_delete_hosts_batch(db):
    db.upsert_hosts([{"name": x, "ip": f"10.0.0.{i}"}
                     for i, x in enumerate(["a", "b", "c", "d"])])
    assert db.delete_hosts(["a", "c"]) == 2
    assert {h.name for h in db.list_hosts()} == {"b", "d"}


def test_host_names_returns_all(db):
    db.upsert_hosts([{"name": "x", "ip": "1.1.1.1"},
                     {"name": "y", "ip": "2.2.2.2"}])
    assert db.host_names() == ["x", "y"]


# ---- user_version / schema 缓存 ---------------------------------------------


def test_user_version_set_after_init(db):
    """首次建表后 user_version 应被设到目标版本号。"""
    v = db._connect().execute("PRAGMA user_version").fetchone()[0]
    assert v == Database._SCHEMA_VERSION


def test_second_database_instance_skips_ddl(tmp_db_path):
    """第二个 Database 实例不应重新跑 schema.sql（幂等但浪费 IO）。"""
    Database(tmp_db_path)                       # 第一次：跑 DDL
    # 如果 _init_schema 被再次调用，应该检测到 user_version 直接返回。
    db2 = Database(tmp_db_path)
    # 表还在、数据可读写
    db2.upsert_hosts([{"name": "a", "ip": "1.1.1.1"}])
    assert db2.get_host_by_name("a") is not None


def test_journal_mode_wal(db):
    """WAL 模式应该被启用。"""
    mode = db._connect().execute("PRAGMA journal_mode").fetchone()[0]
    assert mode.lower() == "wal"


def test_synchronous_normal(db):
    """synchronous=NORMAL（值 1）是 WAL 模式推荐搭配。"""
    sync = db._connect().execute("PRAGMA synchronous").fetchone()[0]
    assert sync == 1


# ---- 长连接 ----------------------------------------------------------------


def test_long_connection_reuses_conn(tmp_db_path):
    """use_long_connection=True 时多次 _connect 应返回同一条连接。"""
    db = Database(tmp_db_path, use_long_connection=True)
    c1 = db._connect()
    c2 = db._connect()
    assert c1 is c2
    db.close()


def test_long_connection_close_releases(tmp_db_path):
    db = Database(tmp_db_path, use_long_connection=True)
    db._connect()
    assert db._long_conn is not None
    db.close()
    assert db._long_conn is None
    # close 后 _connect 重新打开
    db._connect()
    assert db._long_conn is not None
    db.close()


def test_default_no_long_connection(tmp_db_path):
    """默认不开长连接：每次 _connect 都新建。"""
    db = Database(tmp_db_path)
    c1 = db._connect()
    c2 = db._connect()
    assert c1 is not c2
    c1.close(); c2.close()
