"""Docker HEALTHCHECK 入口。

被 ``Dockerfile`` 的 ``HEALTHCHECK`` 指令调用：

    HEALTHCHECK CMD ["python", "healthcheck.py"]

容器内 ``WORKDIR=/app``，所以相对路径 ``conf/config.yaml`` 和
``data/state.db`` 都对得上。

检查项：
    1. SQLite 数据库能打开并执行 ``SELECT 1``
    2. ``fping`` 能 ping 通 ``conf.healthcheck.gateway`` 指定的烟测地址

退出码 ``0`` = 健康，``1`` = 不健康。失败原因写到 stderr，方便
``docker inspect`` 查看。

为什么独立成脚本而不是 ``monitor.py`` 子命令：
    * ``monitor.py`` CLI 现在只保留 ``run``（启动长驻主循环），没有
      一次性检查模式
    * HEALTHCHECK 需要短平快：单进程、零项目内部依赖、毫秒级启动
    * 独立脚本让 HEALTHCHECK 链路不耦合主进程的 import / 日志初始化
"""
from __future__ import annotations

import logging
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path
from typing import Optional

# 容器内 WORKDIR=/app；本地开发时也用这个相对路径
CONFIG_PATH = Path("conf/config.yaml")
DEFAULT_GATEWAY = "1.1.1.1"
FPING_TIMEOUT_S = 10

log = logging.getLogger("fping_monitor.healthcheck")


def _load_cfg() -> dict:
    """读 conf/config.yaml；文件缺失或解析失败时返回空 dict。

    配置坏了不算"不健康"——HEALTHCHECK 主要看运行时可达性，配置错误
    应当由 daemon 启动期的加载校验捕获。但仍然 warning 提醒一下。
    """
    if not CONFIG_PATH.exists():
        log.warning("找不到 %s，用默认 healthcheck 配置", CONFIG_PATH)
        return {}
    try:
        import yaml  # 在函数内 import，让 yaml 缺失走到 except 路径
        loaded = yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8"))
        return loaded if isinstance(loaded, dict) else {}
    except Exception as e:                        # noqa: BLE001
        log.warning("读取 %s 失败：%s；用默认 healthcheck 配置",
                    CONFIG_PATH, e)
        return {}


def _check_db(db_path: str) -> Optional[str]:
    """SQLite 连通性。返回 None = OK，否则返回失败原因。"""
    try:
        conn = sqlite3.connect(db_path, timeout=5)
        try:
            conn.execute("SELECT 1").fetchone()
        finally:
            conn.close()
    except Exception as e:                        # noqa: BLE001
        return f"db: {e}"
    return None


def _check_fping(gateway: str) -> Optional[str]:
    """fping 探活。返回 None = OK，否则返回失败原因。"""
    if shutil.which("fping") is None:
        return "fping: binary not found in PATH"
    try:
        proc = subprocess.run(
            ["fping", "-C1", "-t500", gateway],
            capture_output=True,
            text=True,
            timeout=FPING_TIMEOUT_S,
        )
    except subprocess.TimeoutExpired:
        return f"fping: timeout after {FPING_TIMEOUT_S}s"
    except Exception as e:                        # noqa: BLE001
        return f"fping: {e}"
    # fping 退出码：0 = 至少一台通；非 0 = 全不通或参数错
    if proc.returncode != 0:
        return f"fping: cannot reach {gateway} (rc={proc.returncode})"
    return None


def main() -> int:
    """返回 0 = 健康，1 = 不健康。"""
    cfg = _load_cfg()
    db_path = cfg.get("database", "data/state.db")
    gateway = (cfg.get("healthcheck") or {}).get("gateway", DEFAULT_GATEWAY)

    failures: list[str] = []
    for result in (_check_db(db_path), _check_fping(gateway)):
        if result is not None:
            failures.append(result)

    if failures:
        print("UNHEALTHY: " + "; ".join(failures), file=sys.stderr)
        return 1
    print("OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
