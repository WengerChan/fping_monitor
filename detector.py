"""fping 检测层。

每个周期调用一次 ``fping`` 批量化检测所有主机，输出 ``{name: bool}``。
"""
from __future__ import annotations

import logging
import shutil
import subprocess
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from models import Host
from util import parse_fping_output

log = logging.getLogger("fping_monitor.detector")


@dataclass
class DetectResult:
    """单次 fping 检测的结果汇总。

    字段：
        * ``alive``      — ``{name: bool}``：本轮是否可达
        * ``duration_ms``— 整个 fping 调用耗时（包含 fork / select / 解析）
        * ``returncode`` — fping 进程退出码（0=至少一台通；1=全不通；2=参数错）
        * ``attempted``  — 探测的主机数
        * ``reachable``  — 可达的主机数
    """
    alive: Dict[str, bool] = field(default_factory=dict)
    duration_ms: int = 0
    returncode: int = 0
    attempted: int = 0
    reachable: int = 0


@dataclass
class FpingDetector:
    """单次批量化 fping 检测。

    传入 ``hosts`` 但 fping 输出中未出现的主机视为 DOWN。
    """

    count: int = 1
    interval_ms: int = 10
    timeout_ms: int = 500
    retry: int = 0
    extra: Optional[List[str]] = None  # 透传给 fping 的额外参数

    def __post_init__(self) -> None:
        if self.extra is None:
            self.extra = []
        if shutil.which("fping") is None:
            raise RuntimeError(
                "PATH 中找不到 fping，请先安装。"
            )

    def _subprocess_timeout_s(self) -> int:
        """subprocess.run 等待 fping 结束的最长秒数。

        fping 是并发检测（fork + select），不按主机数线性放大。
        一轮时间上限 ≈ count × (timeout + interval) + 启动 overhead。
        加 5s 缓冲防 edge case。
        """
        per_round_ms = self.count * (self.timeout_ms + self.interval_ms)
        return max(10, per_round_ms // 1000 + 5)

    def detect(self, hosts: List[Host]) -> DetectResult:
        if not hosts:
            return DetectResult(alive={}, duration_ms=0, attempted=0, reachable=0)

        ips = [h.ip for h in hosts]
        cmd = [
            "fping",
            "-B", str(self.interval_ms),  # 主机间发送间隔 (ms)
            "-r", str(self.retry),        # 重试次数
            "-t", str(self.timeout_ms),   # 单次超时 (ms)
            "-C", str(self.count),        # 每台主机 ping 几次
            "-e",                         # 显示 RTT
            *(self.extra or []),
            *ips,
        ]
        log.debug("fping 命令：%s", " ".join(cmd))

        started = time.monotonic()
        try:
            proc = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=self._subprocess_timeout_s(),
            )
        except subprocess.TimeoutExpired as e:
            duration = int((time.monotonic() - started) * 1000)
            log.error("fping 超时",
                      extra={"event": "fping_timeout",
                             "timeout_s": self._subprocess_timeout_s(),
                             "duration_ms": duration,
                             "hosts": len(hosts)})
            # 超时视为全部不可达
            return DetectResult(
                alive={h.name: False for h in hosts},
                duration_ms=duration,
                returncode=-1,
                attempted=len(hosts),
                reachable=0,
            )

        duration_ms = int((time.monotonic() - started) * 1000)
        # 可达主机行在 stdout，不可达主机行在 stderr
        alive_rtt = parse_fping_output(proc.stdout or "", proc.stderr or "")

        alive = {h.name: h.ip in alive_rtt for h in hosts}
        return DetectResult(
            alive=alive,
            duration_ms=duration_ms,
            returncode=proc.returncode,
            attempted=len(hosts),
            reachable=sum(1 for v in alive.values() if v),
        )
