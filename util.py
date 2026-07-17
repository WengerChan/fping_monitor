"""通用工具：YAML 加载、日志初始化、fping 输出解析。"""
from __future__ import annotations

import logging
import os
import re
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path
from typing import Any, Dict

import yaml


def load_yaml(path: str | os.PathLike) -> Dict[str, Any]:
    """加载 YAML 文件并返回字典。根节点必须是 mapping。"""
    with open(path, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    if not isinstance(data, dict):
        raise ValueError(f"YAML 根节点必须是 mapping，得到 {type(data).__name__}")
    return data


def setup_logging(level: str = "INFO", log_dir: str = "logs",
                  backup_days: int = 14) -> logging.Logger:
    """初始化全局 logger。

    行为：
        * 输出到控制台 + 按天滚动的日志文件（logs/fping_monitor.log）
        * 日志文件名后缀为日期，保留 ``backup_days`` 天的历史
        * 多次调用不会重复挂载 handler
    """
    logger = logging.getLogger("fping_monitor")
    logger.setLevel(getattr(logging, level.upper(), logging.INFO))
    logger.propagate = False

    if logger.handlers:
        return logger

    Path(log_dir).mkdir(parents=True, exist_ok=True)
    fmt = logging.Formatter(
        "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    file_handler = TimedRotatingFileHandler(
        filename=os.path.join(log_dir, "fping_monitor.log"),
        when="midnight",
        interval=1,
        backupCount=backup_days,
        encoding="utf-8",
        utc=False,
    )
    file_handler.setFormatter(fmt)
    file_handler.suffix = "%Y-%m-%d"
    logger.addHandler(file_handler)

    stream_handler = logging.StreamHandler()
    stream_handler.setFormatter(fmt)
    logger.addHandler(stream_handler)

    return logger


# fping ``-C <count> -e`` 在每台可达主机上输出一行：
#   8.8.8.8 : [0], 64 bytes, 0.12 ms (0.12 avg, 0% loss)
# 不可达主机把信息写到 stderr，格式如：
#   8.8.8.8 : unreachable
#   ICMP Time Exceeded from 8.8.8.8 for 1.2.3.4
# 我们只需要从 stdout 提取可达主机的 RTT。
_FPING_ALIVE_RE = re.compile(
    r"^(?P<ip>\S+)\s*:\s*\[[\d,\s]+\],\s*\d+\s*bytes,\s*(?P<rtt>[\d.]+)\s*ms"
)
_FPING_DEAD_RE = re.compile(
    r"^(?P<ip>\S+)\s*:\s*unreachable"
)


def parse_fping_output(stdout: str, stderr: str = "") -> Dict[str, float]:
    """解析 fping 输出。

    返回 ``{ip: rtt_ms}``，仅包含 fping 判定为可达的主机。
    如果 stderr 中出现 ``<ip> : unreachable`` 且该 IP 之前被误识别为可达，
    会从结果中剔除。其他 stderr 行（如 ICMP 错误）忽略。
    """
    alive: Dict[str, float] = {}
    for line in (stdout or "").splitlines():
        line = line.strip()
        if not line:
            continue
        m = _FPING_ALIVE_RE.match(line)
        if m:
            try:
                alive[m.group("ip")] = float(m.group("rtt"))
            except ValueError:
                continue
    for line in (stderr or "").splitlines():
        line = line.strip()
        if not line:
            continue
        m = _FPING_DEAD_RE.match(line)
        if m and m.group("ip") in alive:
            del alive[m.group("ip")]
    return alive
