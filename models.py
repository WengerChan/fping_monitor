"""fping_monitor 的数据模型。

纯数据容器，不做任何 I/O。保持简洁以便序列化与跨模块共享。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import List, Optional


class HostStatus(str, Enum):
    """主机存活状态。"""
    UNKNOWN = "UNKNOWN"  # 初始状态，尚未确认
    UP = "UP"            # 在线
    DOWN = "DOWN"        # 离线


class EventType(str, Enum):
    """事件类型，仅在状态发生跃迁时记录。"""
    DOWN = "DOWN"        # 主机掉线
    RECOVER = "RECOVER"  # 主机恢复


@dataclass
class Host:
    """主机实体，与 ``hosts`` 表一一对应。"""
    id: Optional[int] = None
    name: str = ""
    ip: str = ""
    tags: List[str] = field(default_factory=list)  # 业务标签，例如 ["prod", "db", "shanghai"]
    status: HostStatus = HostStatus.UNKNOWN
    fail_count: int = 0     # 连续失败计数（达到阈值触发 DOWN）
    recover_count: int = 0  # 连续成功计数（达到阈值触发 UP）
    last_check: Optional[datetime] = None   # 最近一次检测时间
    last_change: Optional[datetime] = None  # 最近一次状态变更时间

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "name": self.name,
            "ip": self.ip,
            "tags": list(self.tags),
            "status": self.status.value,
            "fail_count": self.fail_count,
            "recover_count": self.recover_count,
            "last_check": self.last_check.isoformat() if self.last_check else None,
            "last_change": self.last_change.isoformat() if self.last_change else None,
        }


@dataclass
class Event:
    """状态跃迁事件，对应 ``events`` 表的一行。"""
    id: Optional[int] = None
    host_id: int = 0
    event: EventType = EventType.DOWN
    time: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    message: str = ""
