"""检测层。

一个周期内只调用一次 ``fping`` 批量检测所有主机，结果解析为
``{ip: rtt_ms}``，再转成 ``{name: bool}`` 交给上层使用。

要扩展 TCP / HTTP / SSH 检测，只需再写一个实现 ``Detector`` 协议
（``detect(hosts) -> Dict[str, bool]``）的类，并注入到 ``StateMachine``。
"""
from __future__ import annotations

import logging
import shutil
import subprocess
from dataclasses import dataclass
from typing import Dict, List, Mapping, Protocol

from models import Host
from util import parse_fping_output

log = logging.getLogger("fping_monitor.detector")


class Detector(Protocol):
    """检测器协议。返回 ``{name: bool}``：True 表示本轮可达。"""
    def detect(self, hosts: List[Host]) -> Dict[str, bool]: ...


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
                "PATH 中找不到 fping，请先安装或自定义检测器。"
            )

    def detect(self, hosts: List[Host]) -> Dict[str, bool]:
        if not hosts:
            return {}

        ips = [h.ip for h in hosts]
        # fping 返回值：至少一台可达为 0；全部不可达为 1；参数错误为 2 等。
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


def merge_results(*maps: Mapping[str, bool]) -> Dict[str, bool]:
    """把多个检测器结果做 AND 合并。

    只有当所有检测器都判定为可达时，主机才视为 UP。常用于
    "ping AND https" 这类多维度探活场景。
    """
    if not maps:
        return {}
    keys = set(maps[0].keys())
    for m in maps[1:]:
        keys &= set(m.keys())
    return {k: all(m.get(k, False) for m in maps) for k in keys}
