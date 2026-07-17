"""SQLite 持久化层。

sqlite3 的薄封装，不引入 ORM 和迁移框架。
首次连接时从 ``sql/schema.sql`` 应用 DDL。所有公共方法都在成功后自动提交。
"""
from __future__ import annotations

import logging
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Iterable, List, Optional

from models import Event, EventType, Host, HostStatus
from util import expand_ip_spec

log = logging.getLogger("fping_monitor.db")

# 初始化脚本路径，部署时与代码一起发布
SCHEMA_FILE = Path(__file__).parent / "sql" / "schema.sql"


def _encode_tags(tags) -> str:
    """把 list[str] 序列化为数据库里存储的逗号分隔字符串。空列表返回空串。"""
    if not tags:
        return ""
    return ",".join(str(t).strip() for t in tags if str(t).strip())


def _decode_tags(raw: Optional[str]) -> List[str]:
    """把数据库里的逗号分隔字符串还原为 list[str]。空串/None 都返回空列表。"""
    if not raw:
        return []
    return [t.strip() for t in raw.split(",") if t.strip()]


class Database:
    def __init__(self, path: str):
        self.path = path
        # 确保父目录存在，避免首次运行时因目录缺失而失败
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()

    # ---- 连接管理 -------------------------------------------------------

    def _connect(self) -> sqlite3.Connection:
        """创建一条新连接。autocommit 模式（isolation_level=None），由调用方显式控制事务。"""
        conn = sqlite3.connect(self.path, timeout=10, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        return conn

    def _init_schema(self) -> None:
        """首次启动时建表。"""
        if not SCHEMA_FILE.exists():
            raise FileNotFoundError(f"找不到 schema 文件：{SCHEMA_FILE}")
        with self._connect() as conn:
            conn.executescript(SCHEMA_FILE.read_text(encoding="utf-8"))

    # ---- 主机表 ---------------------------------------------------------

    def upsert_hosts(self, hosts: Iterable[dict]) -> None:
        """批量写入主机。已存在则更新 IP 和 tags，不删除 YAML 中已移除的主机（删除是显式操作）。

        ``ip`` 字段支持简写：单 IP、CIDR、完整范围、短范围。展开成多个主机时，
        自动把 ``name`` 拼上 IP 作为后缀保证唯一性。

        每条 host 字典支持字段：
            * ``name`` (必填) — 主机名；展开多 IP 时会自动追加 ``-IP`` 后缀
            * ``ip``   (必填) — 单个 IP / CIDR / 范围，也支持 list 形式混合多个 spec
            * ``tags`` (可选) — list[str]，会序列化为逗号分隔字符串

        示例：
            ``{"name": "web", "ip": "10.1.2.3-10"}`` → 8 条主机，name 为
            ``web-10.1.2.3`` ... ``web-10.1.2.10``
        """
        rows: list[tuple[str, str, str]] = []
        for h in hosts:
            name = h.get("name")
            if not name:
                raise ValueError("host 缺少必填字段 name")
            tags = _encode_tags(h.get("tags") or [])
            ip_field = h.get("ip")
            if ip_field is None or ip_field == "":
                raise ValueError(f"host {name!r} 缺少必填字段 ip")

            # 支持 list 形式：每条 spec 单独展开
            if isinstance(ip_field, list):
                specs = [str(s) for s in ip_field]
            else:
                specs = [str(ip_field)]

            expanded: list[str] = []
            for s in specs:
                expanded.extend(expand_ip_spec(s))

            if not expanded:
                continue
            if len(expanded) == 1:
                rows.append((name, expanded[0], tags))
            else:
                for ip in expanded:
                    rows.append((f"{name}-{ip}", ip, tags))

        if not rows:
            return
        with self._connect() as conn:
            conn.executemany(
                """
                INSERT INTO hosts (name, ip, tags) VALUES (?, ?, ?)
                ON CONFLICT(name) DO UPDATE SET
                    ip   = excluded.ip,
                    tags = excluded.tags
                """,
                rows,
            )

    def list_hosts(self) -> List[Host]:
        """返回全部主机，按 id 升序。"""
        with self._connect() as conn:
            cur = conn.execute("SELECT * FROM hosts ORDER BY id")
            return [self._row_to_host(r) for r in cur.fetchall()]

    def get_host_by_name(self, name: str) -> Optional[Host]:
        with self._connect() as conn:
            cur = conn.execute("SELECT * FROM hosts WHERE name = ?", (name,))
            row = cur.fetchone()
            return self._row_to_host(row) if row else None

    def get_host_by_ip(self, ip: str) -> Optional[Host]:
        with self._connect() as conn:
            cur = conn.execute("SELECT * FROM hosts WHERE ip = ?", (ip,))
            row = cur.fetchone()
            return self._row_to_host(row) if row else None

    def update_host_state(self, host_id: int, *, status: HostStatus,
                          fail_count: int, recover_count: int,
                          last_check: datetime,
                          last_change: Optional[datetime]) -> None:
        """更新一台主机的检测结果。

        ``last_change`` 传 None 时保留原值，用于"本次没有状态跃迁"的场景。
        """
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE hosts
                   SET status = ?,
                       fail_count = ?,
                       recover_count = ?,
                       last_check = ?,
                       last_change = COALESCE(?, last_change)
                 WHERE id = ?
                """,
                (status.value, fail_count, recover_count,
                 last_check.isoformat(timespec="seconds"),
                 last_change.isoformat(timespec="seconds") if last_change else None,
                 host_id),
            )

    # ---- 事件表 ---------------------------------------------------------

    def insert_event(self, host_id: int, event: EventType,
                     message: str = "", at: Optional[datetime] = None) -> None:
        """插入一条状态跃迁事件。"""
        when = (at or datetime.utcnow()).isoformat(timespec="seconds")
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO events (host_id, event, time, message) VALUES (?, ?, ?, ?)",
                (host_id, event.value, when, message),
            )

    def recent_events(self, limit: int = 50) -> List[dict]:
        """返回最近 N 条事件，按时间倒序。"""
        with self._connect() as conn:
            cur = conn.execute(
                """
                SELECT e.id, e.host_id, h.name AS host_name, e.event, e.time, e.message
                  FROM events e JOIN hosts h ON h.id = e.host_id
                 ORDER BY e.time DESC, e.id DESC
                 LIMIT ?
                """,
                (limit,),
            )
            return [dict(r) for r in cur.fetchall()]

    # ---- 工具方法 -------------------------------------------------------

    @staticmethod
    def _row_to_host(row: sqlite3.Row) -> Host:
        """把 sqlite3.Row 转成 Host 对象。"""
        def _parse(ts: Optional[str]) -> Optional[datetime]:
            return datetime.fromisoformat(ts) if ts else None

        return Host(
            id=row["id"],
            name=row["name"],
            ip=row["ip"],
            tags=_decode_tags(row["tags"]),
            status=HostStatus(row["status"]),
            fail_count=row["fail_count"],
            recover_count=row["recover_count"],
            last_check=_parse(row["last_check"]),
            last_change=_parse(row["last_change"]),
        )
