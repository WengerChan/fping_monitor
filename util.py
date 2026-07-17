"""通用工具：YAML 加载、日志初始化、fping 输出解析、配置热加载监控。"""
from __future__ import annotations

import json
import logging
import os
import re
from datetime import datetime, timezone
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path
from typing import Any, Dict, Optional

import yaml


def load_yaml(path: str | os.PathLike) -> Dict[str, Any]:
    """加载 YAML 文件并返回字典。根节点必须是 mapping。"""
    with open(path, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    if not isinstance(data, dict):
        raise ValueError(f"YAML 根节点必须是 mapping，得到 {type(data).__name__}")
    return data


# ---------------------------------------------------------------------------
# 日志格式化
# ---------------------------------------------------------------------------

# logging.LogRecord 的内置属性，extra={} 字段不应与之冲突
_LOGRECORD_RESERVED = frozenset({
    "name", "msg", "args", "levelname", "levelno", "pathname", "filename",
    "module", "exc_info", "exc_text", "stack_info", "lineno", "funcName",
    "created", "msecs", "relativeCreated", "thread", "threadName",
    "processName", "process", "message", "asctime", "taskName",
})


class JsonFormatter(logging.Formatter):
    """每条日志输出单行 JSON，logstash ``codec => json_lines`` 可直接消费。

    固定字段：
        * ``ts`` — UTC ISO 8601，带微秒
        * ``level`` — INFO / WARNING / ERROR …
        * ``logger`` — 子 logger 名（如 ``fping_monitor.scheduler``）
        * ``message`` — 已 format 的消息体

    透传字段：``log.info("...", extra={"k": v})`` 中的 ``k=v`` 会作为同级 JSON 字段输出。
    异常：``exc_info`` 字段会包含完整 traceback 文本。
    """

    def format(self, record: logging.LogRecord) -> str:
        ts = datetime.fromtimestamp(record.created, tz=timezone.utc).isoformat()
        payload: Dict[str, Any] = {
            "ts": ts,
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        # 把 extra 字段合并进来（用户通过 log.info(..., extra=...) 传入）
        for k, v in record.__dict__.items():
            if k in _LOGRECORD_RESERVED or k.startswith("_"):
                continue
            payload[k] = v
        if record.exc_info:
            payload["exc_info"] = self.formatException(record.exc_info)
        if record.stack_info:
            payload["stack_info"] = record.stack_info
        # default=str 让 datetime / Path / Enum 等也能序列化
        return json.dumps(payload, ensure_ascii=False, default=str)


class TextFormatter(logging.Formatter):
    """人类可读的纯文本格式，本地开发时用。"""
    DEFAULT = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
    DATEFMT = "%Y-%m-%d %H:%M:%S"

    def __init__(self) -> None:
        super().__init__(fmt=self.DEFAULT, datefmt=self.DATEFMT)


def setup_logging(level: str = "INFO", log_dir: str = "logs",
                  backup_days: int = 14,
                  fmt: str = "json") -> logging.Logger:
    """初始化全局 logger，支持 JSON / TEXT 两种格式。

    行为：
        * 每次调用都重建 handler（保证 ``fmt`` 变更能立刻生效；旧 handler 会先 close）
        * level 每次都更新
        * 写到 ``logs/fping_monitor.log``（按天滚动，保留 ``backup_days`` 天）
        * 同时输出到 stderr
        * ``logger.propagate = False`` 避免重复输出
    """
    logger = logging.getLogger("fping_monitor")
    logger.setLevel(getattr(logging, level.upper(), logging.INFO))
    logger.propagate = False

    # 重建 handler：让 format 变化能立即生效（之前挂的可能不是同一种 Formatter）
    for h in list(logger.handlers):
        try:
            h.close()
        except Exception:                              # noqa: BLE001
            pass
        logger.removeHandler(h)

    Path(log_dir).mkdir(parents=True, exist_ok=True)
    formatter = JsonFormatter() if fmt == "json" else TextFormatter()

    file_handler = TimedRotatingFileHandler(
        filename=os.path.join(log_dir, "fping_monitor.log"),
        when="midnight",
        interval=1,
        backupCount=backup_days,
        encoding="utf-8",
        utc=False,
    )
    file_handler.setFormatter(formatter)
    file_handler.suffix = "%Y-%m-%d"
    logger.addHandler(file_handler)

    stream_handler = logging.StreamHandler()
    stream_handler.setFormatter(formatter)
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


# ---------------------------------------------------------------------------
# 配置热加载：监控两个 YAML 文件的 mtime，变化时重读
# ---------------------------------------------------------------------------


class ConfigWatcher:
    """监控 config.yaml / server.yaml 的 mtime，支持热加载。

    用法：
        watcher = ConfigWatcher("config.yaml", "server.yaml")
        while True:
            changed = watcher.reload()       # 每轮检查一次
            if changed in ("config", "all"):
                rebuild_components(watcher.cfg)
            if changed in ("servers", "all"):
                sync_hosts(watcher.server_cfg)

    ``reload(force=True)`` 会无视 mtime 直接重读，用于响应 SIGHUP 等信号。
    返回值：
        * ``"config"`` — config.yaml 变了
        * ``"servers"`` — server.yaml 变了
        * ``"all"`` — 两个都变了
        * ``None`` — 没变
    """

    def __init__(self, config_path: str | os.PathLike,
                 servers_path: str | os.PathLike):
        self._config_path = Path(config_path)
        self._servers_path = Path(servers_path)
        self._cfg: Dict[str, Any] = {}
        self._server_cfg: Dict[str, Any] = {}
        self._cfg_mtime: float = 0.0
        self._servers_mtime: float = 0.0
        self.reload(force=True)

    # ---- 公共属性 -------------------------------------------------------

    @property
    def cfg(self) -> Dict[str, Any]:
        return self._cfg

    @property
    def server_cfg(self) -> Dict[str, Any]:
        return self._server_cfg

    @property
    def config_path(self) -> Path:
        return self._config_path

    @property
    def servers_path(self) -> Path:
        return self._servers_path

    # ---- 核心方法 -------------------------------------------------------

    def reload(self, force: bool = False) -> Optional[str]:
        """检查 mtime 并按需重读。返回哪个文件变了。"""
        if force:
            self._load_config(force=True)
            self._load_servers(force=True)
            return "all"

        changed: Optional[str] = None
        if self._mtime_changed(self._config_path, self._cfg_mtime):
            self._load_config(force=False)
            changed = "config"
        if self._mtime_changed(self._servers_path, self._servers_mtime):
            self._load_servers(force=False)
            changed = "servers" if changed is None else "all"
        return changed

    # ---- 内部方法 -------------------------------------------------------

    @staticmethod
    def _mtime(path: Path) -> float:
        try:
            return path.stat().st_mtime
        except FileNotFoundError:
            return 0.0

    @classmethod
    def _mtime_changed(cls, path: Path, last: float) -> bool:
        """mtime 变了，且新值非 0（避免文件被删后误判）。"""
        mt = cls._mtime(path)
        if mt == 0.0:
            return False
        return mt != last

    def _load_config(self, *, force: bool) -> None:
        self._cfg = self._safe_load(self._config_path)
        self._cfg_mtime = self._mtime(self._config_path)
        if force:
            log_msg = "强制重载"
        else:
            log_msg = f"mtime 变化 ({self._cfg_mtime:.0f})"
        logging.getLogger("fping_monitor").info(
            "config.yaml %s，已重读", log_msg
        )

    def _load_servers(self, *, force: bool) -> None:
        self._server_cfg = self._safe_load(self._servers_path)
        self._servers_mtime = self._mtime(self._servers_path)
        logging.getLogger("fping_monitor").info(
            "server.yaml 变更（hosts=%d），已重读",
            len(self._server_cfg.get("hosts", []) or []),
        )

    @staticmethod
    def _safe_load(path: Path) -> Dict[str, Any]:
        """加载 YAML，文件不存在时返回空 dict 而不是抛错（便于文件被临时移走的场景）。"""
        if not path.exists():
            logging.getLogger("fping_monitor").warning(
                "配置文件不存在：%s（视为空配置）", path
            )
            return {}
        return load_yaml(path)
