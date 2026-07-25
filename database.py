"""SQLite 持久化层。

sqlite3 的薄封装，不引入 ORM 和迁移框架。
首次连接时从 ``sql/schema.sql`` 应用 DDL。所有公共方法都在成功后自动提交。
"""
from __future__ import annotations

import logging
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, List, Optional

from models import EventType, Host, HostStatus
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
    def __init__(self, path: str, *, use_long_connection: bool = False):
        self.path = path
        # 确保父目录存在，避免首次运行时因目录缺失而失败
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._use_long_connection = use_long_connection
        self._long_conn: Optional[sqlite3.Connection] = None
        self._init_schema()

    # ---- 连接管理 -------------------------------------------------------

    def _connect(self) -> sqlite3.Connection:
        """获取一条 SQLite 连接。

        ``use_long_connection=False``（默认）：每次调用都新建连接。
        适合测试 / 多进程场景，连接生命周期显式、可重入。

        ``use_long_connection=True``：复用同一条连接，省去每操作 1~2ms 的
        open/close 开销。**仅在单线程 daemon 里使用**，否则 SQLite 会因
        多线程持有连接抛 ProgrammingError。daemon 主循环退出前应 ``close()``。
        """
        if self._use_long_connection:
            if self._long_conn is None:
                self._long_conn = self._open_connection()
            return self._long_conn
        return self._open_connection()

    def _open_connection(self) -> sqlite3.Connection:
        # 注意：PRAGMA journal_mode / synchronous 是 **连接级别** 的设置，
        # 不会被持久化到 db 文件 header（foreign_keys 才会）。所以每次新
        # 连接都要重新应用一遍，否则会掉回 SQLite 的默认值。
        conn = sqlite3.connect(self.path, timeout=10, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA synchronous = NORMAL")
        return conn

    def close(self) -> None:
        """关闭长连接。daemon SIGTERM 退出前调用，保证 WAL 文件落盘。"""
        if self._long_conn is not None:
            try:
                self._long_conn.close()
            except Exception:                       # noqa: BLE001
                pass
            self._long_conn = None

    # 当前 schema 版本号（与 schema.sql 里的 DDL 同步）。如果改了 DDL
    # 但没改这里，schema 不会被重新执行——请同时 bump 这个值。
    _SCHEMA_VERSION = 1

    def _init_schema(self) -> None:
        """按需建表（用 ``PRAGMA user_version`` 做幂等闸门）。

        行为：
            * 首次建库（user_version=0）→ 执行 schema.sql → 写入新版本号
            * 已建库且版本匹配 → 跳过
            * 已建库但版本不匹配 → 执行 schema.sql（DDL 自身应是幂等的
              ``IF NOT EXISTS``），再覆盖版本号

        这样频繁调用（例如配置热加载触发重建 Database）也不会重复 IO。
        """
        if not SCHEMA_FILE.exists():
            raise FileNotFoundError(f"找不到 schema 文件：{SCHEMA_FILE}")
        with self._connect() as conn:
            cur = conn.execute("PRAGMA user_version")
            current = cur.fetchone()[0]
            if current >= self._SCHEMA_VERSION:
                return
            conn.executescript(SCHEMA_FILE.read_text(encoding="utf-8"))
            # schema.sql 只含 DDL，user_version 由 Python 代码统一管。
            # 用占位符避开参数化限制（PRAGMA user_version 不接受 ? 占位符）。
            conn.execute(f"PRAGMA user_version = {self._SCHEMA_VERSION}")

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

    def delete_hosts(self, names: Iterable[str]) -> int:
        """按 name 删除主机。级联删除该主机的 events 行。

        返回实际删除的主机行数。

        不存在的 name 静默跳过（``DELETE WHERE name IN (...)`` 语义）。
        """
        names_list = list(names)
        if not names_list:
            return 0
        placeholders = ",".join("?" for _ in names_list)
        with self._connect() as conn:
            cur = conn.execute(
                f"DELETE FROM hosts WHERE name IN ({placeholders})",
                names_list,
            )
            return cur.rowcount

    def host_names(self) -> List[str]:
        """返回全部主机的 name 列表（用于 YAML→DB 同步时算差集）。"""
        with self._connect() as conn:
            cur = conn.execute("SELECT name FROM hosts ORDER BY id")
            return [r[0] for r in cur.fetchall()]

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
        when = (at or datetime.now(timezone.utc)).isoformat(timespec="seconds")
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
