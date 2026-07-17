"""fping 检测层。

每个周期调用一次 ``fping`` 批量化检测所有主机，输出 ``{name: bool}``。
"""
from __future__ import annotations

import logging
import shutil
import subprocess
from dataclasses import dataclass
from typing import Dict, List

from models import Host
from util import parse_fping_output

log = logging.getLogger("fping_monitor.detector")


@dataclass
class FpingDetector:
    """单次批量化 fping 检测。

    传入 ``hosts`` 但 fping 输出中未出现的主机视为 DOWN。
    """

    count: int = 1
    interval_ms: int = 10
    timeout_ms: int = 500
    retry: int = 0
    extra: List[str] = None  # 透传给 fping 的额外参数

    def __post_init__(self) -> None:
        if self.extra is None:
            self.extra = []
        if shutil.which("fping") is None:
            raise RuntimeError(
                "PATH 中找不到 fping，请先安装。"
            )

    def detect(self, hosts: List[Host]) -> Dict[str, bool]:
        if not hosts:
            return {}

        ips = [h.ip for h in hosts]
        cmd = [
            "fping",
            "-B", str(self.interval_ms),  # 主机间发送间隔 (ms)
            "-r", str(self.retry),        # 重试次数
            "-t", str(self.timeout_ms),   # 单次超时 (ms)
            "-C", str(self.count),        # 每台主机 ping 几次
            "-e",                         # 显示 RTT
            *self.extra,
            *ips,
        ]
        log.debug("fping 命令：%s", " ".join(cmd))
        # 超时时间 = (timeout * count + 100ms) * 主机数 / 1000 + 5s 缓冲
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=max(10, (self.timeout_ms * self.count + 100) * max(1, len(ips)) // 1000 + 5),
        )
        # 可达主机行在 stdout，不可达主机行在 stderr
        alive_rtt = parse_fping_output(proc.stdout or "", proc.stderr or "")

        result: Dict[str, bool] = {}
        for h in hosts:
            result[h.name] = h.ip in alive_rtt
        return result
