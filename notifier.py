"""通知器：统一的 ``notify_down`` / ``notify_recover`` 接口。

通知渠道在 ``config.yaml`` 的 ``notify.channels`` 下配置，每个渠道接收
完整事件并自行决定如何格式化与发送。新增渠道只需在 ``_CHANNELS`` 注册
一个类，并在 YAML 里指定 ``type``。

当前支持的渠道：
  * ``dingtalk`` — 钉钉自定义机器人 Webhook（完整实现）
  * ``cuckoo``   — 内部布谷鸟告警平台占位（保留扩展点）

派发模式：
  * ``async_dispatch=True``（默认）：用 ``ThreadPoolExecutor`` 派发，
    webhook POST 不阻塞主循环；适合生产环境。
  * ``async_dispatch=False``：同步派发，测试 / 本地开发用。

防抖：
  * ``min_interval_s > 0`` 时，``(channel.name, host.name, kind)`` 在
    ``min_interval_s`` 秒内不重复发送，防止抖动期间刷屏 webhook。
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import logging
import os
import time
import urllib.parse
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Protocol, Tuple, TypedDict

import requests

from models import Host

log = logging.getLogger("fping_monitor.notifier")


class Channel(Protocol):
    """通知渠道协议。"""
    name: str
    def notify_down(self, host: Host) -> None: ...
    def notify_recover(self, host: Host) -> None: ...


# 通知配置 TypedDict：让 ``Notifier.from_config`` 的字段名拼错能在静态检查
# 时被 mypy 抓到（运行时仍然兼容，因为是 ``**dict`` 解包到 dataclass，
# 拼错的字段会被 dataclass __init__ 抛 TypeError 然后被吞）。
class DingTalkConfig(TypedDict, total=False):
    type: str          # 固定为 "dingtalk"
    webhook_url: str
    secret: str
    at_mobiles: List[str]
    at_all: bool
    timeout: float


class NotifyConfig(TypedDict, total=False):
    enabled: bool
    channels: List[Dict[str, Any]]
    async_dispatch: bool
    max_workers: int
    min_interval_s: float


# ---------------------------------------------------------------------------
# 钉钉
# ---------------------------------------------------------------------------


def _dingtalk_sign(secret: str, timestamp_ms: int) -> str:
    """计算钉钉机器人加签参数。

    参考：https://open.dingtalk.com/document/orgapp/custom-robots-send-group-messages
    """
    secret_enc = secret.encode("utf-8")
    string_to_sign = f"{timestamp_ms}\n{secret}".encode("utf-8")
    hmac_code = hmac.new(secret_enc, string_to_sign, digestmod=hashlib.sha256).digest()
    return urllib.parse.quote_plus(base64.b64encode(hmac_code))


@dataclass
class DingTalkChannel:
    """钉钉自定义机器人 Webhook 渠道。"""
    webhook_url: str = ""                    # 也可走 DINGTALK_WEBHOOK 环境变量
    secret: str = ""                         # 可选：机器人开启加签时必须填
    at_mobiles: List[str] = field(default_factory=list)
    at_all: bool = False
    timeout: float = 5.0

    def __post_init__(self) -> None:
        # 允许通过环境变量覆盖 Webhook
        self.webhook_url = (
            self.webhook_url
            or os.environ.get("DINGTALK_WEBHOOK", "")
        )
        if not self.webhook_url:
            log.warning("DingTalk 渠道已配置但 webhook_url 为空")

    @property
    def name(self) -> str:
        return "dingtalk"

    def _endpoint(self) -> str:
        """如果配了 secret，给 URL 拼上 timestamp 和 sign。"""
        url = self.webhook_url
        if not self.secret:
            return url
        ts = int(round(time.time() * 1000))
        sign = _dingtalk_sign(self.secret, ts)
        sep = "&" if "?" in url else "?"
        return f"{url}{sep}timestamp={ts}&sign={sign}"

    def _post(self, payload: dict) -> None:
        try:
            r = requests.post(self._endpoint(), json=payload, timeout=self.timeout)
            if r.status_code != 200:
                log.error("DingTalk POST 失败",
                          extra={"channel": "dingtalk",
                                 "status_code": r.status_code,
                                 "response": r.text[:200]})
                return
            # 钉钉返回 {"errcode":0,"errmsg":"ok"}；非 0 视作业务失败
            try:
                body = r.json()
            except ValueError:
                return
            if body.get("errcode", 0) != 0:
                log.error("DingTalk 业务错误",
                          extra={"channel": "dingtalk",
                                 "errcode": body.get("errcode"),
                                 "errmsg": body.get("errmsg")})
        except requests.RequestException as e:
            log.error("DingTalk POST 异常",
                      extra={"channel": "dingtalk", "error": str(e)})

    def _build(self, host: Host, kind: str) -> dict:
        """组装 markdown 消息体。"""
        ts = host.last_change.isoformat() if host.last_change else ""
        tag_line = f"tags: `{'`,`'.join(host.tags) or '-'}`"
        body = (
            f"### {kind}: {host.name} ({host.ip})\n\n"
            f"- 状态：**{host.status.value}**\n"
            f"- {tag_line}\n"
            f"- 时间：{ts}\n"
            f"- 连续失败：{host.fail_count}  连续成功：{host.recover_count}"
        )
        return {
            "msgtype": "markdown",
            "markdown": {"title": f"{kind} {host.name}", "text": body},
            "at": {
                "atMobiles": self.at_mobiles,
                "isAtAll": self.at_all,
            },
        }

    def notify_down(self, host: Host) -> None:
        if not self.webhook_url:
            return
        self._post(self._build(host, "🔴 DOWN"))

    def notify_recover(self, host: Host) -> None:
        if not self.webhook_url:
            return
        self._post(self._build(host, "🟢 RECOVER"))


# ---------------------------------------------------------------------------
# 布谷鸟：占位渠道，留待后续接入
# ---------------------------------------------------------------------------


class _StubChannel:
    """占位基类。子类的 ``name`` 字段决定注册名。"""
    name: str = "stub"
    def __init__(self, *_, **__):
        raise NotImplementedError(
            f"渠道 '{self.name}' 尚未实现，请先补全逻辑再启用。"
        )
    def notify_down(self, host: Host) -> None: ...
    def notify_recover(self, host: Host) -> None: ...


class CuckooChannel(_StubChannel):
    """内部布谷鸟告警平台占位。"""
    name = "cuckoo"


# 渠道注册表
_CHANNELS = {
    "dingtalk": DingTalkChannel,
    "cuckoo": CuckooChannel,
}


# ---------------------------------------------------------------------------
# 调度器
# ---------------------------------------------------------------------------


_KIND_DOWN = "down"
_KIND_RECOVER = "recover"


class Notifier:
    """把通知事件分发给所有已配置渠道，单个渠道失败不影响其他渠道。

    默认用 ``ThreadPoolExecutor`` 异步派发，避免钉钉等 webhook 慢响应
    阻塞主检测循环（100 台同时 DOWN 也不会让 fping 卡住）。测试或本地
    调试可显式传 ``async_dispatch=False``。

    防抖：``min_interval_s`` 控制同一 ``(channel, host, kind)`` 的最小
    重发间隔，防止抖动期间重复刷屏。
    """

    def __init__(self, channels: Iterable[Channel], *,
                 async_dispatch: bool = True,
                 max_workers: int = 4,
                 min_interval_s: float = 0.0):
        self.channels: List[Channel] = list(channels)
        self.async_dispatch = async_dispatch
        self.min_interval_s = float(min_interval_s)
        self._last_sent: Dict[Tuple[str, str, str], float] = {}
        self._executor: Optional[ThreadPoolExecutor] = None
        if async_dispatch and self.channels:
            self._executor = ThreadPoolExecutor(
                max_workers=max(1, max_workers),
                thread_name_prefix="fping-notifier",
            )

    @classmethod
    def from_config(cls, cfg: NotifyConfig) -> "Notifier":
        """根据 YAML 配置构建 Notifier。

        配置项：
            * ``enabled`` — false 时返回空 Notifier
            * ``async_dispatch`` — 默认 true（生产推荐异步派发）
            * ``max_workers`` — 默认 4
            * ``min_interval_s`` — 默认 0（关闭防抖）

        渠道构造异常处理：
            * ``NotImplementedError`` → warning 并跳过（占位渠道）
            * ``TypeError`` → error 并跳过（字段名错）
            * 未知 ``type`` → warning 并跳过
        """
        if not cfg.get("enabled", False):
            return cls(channels=[])
        channels: List[Channel] = []
        for entry in cfg.get("channels", []):
            ctype = entry.get("type")
            if ctype is None:
                # 配置遗漏 type 字段，等同于未知渠道
                log.warning("未知的通知渠道类型", extra={"channel": ctype})
                continue
            cls_ = _CHANNELS.get(ctype)
            if cls_ is None:
                log.warning("未知的通知渠道类型", extra={"channel": ctype})
                continue
            try:
                channels.append(cls_(**{k: v for k, v in entry.items() if k != "type"}))
            except NotImplementedError as e:
                log.warning("跳过未实现的渠道",
                            extra={"channel": ctype, "reason": str(e)})
            except TypeError as e:
                log.error("渠道配置非法",
                          extra={"channel": ctype, "error": str(e)})
        return cls(
            channels=channels,
            async_dispatch=bool(cfg.get("async_dispatch", True)),
            max_workers=int(cfg.get("max_workers", 4)),
            min_interval_s=float(cfg.get("min_interval_s", 0.0)),
        )

    # ---- 公开 API --------------------------------------------------------

    def notify_down(self, host: Host) -> None:
        self._dispatch(host, _KIND_DOWN)

    def notify_recover(self, host: Host) -> None:
        self._dispatch(host, _KIND_RECOVER)

    def close(self, *, wait: bool = True) -> None:
        """关闭后台线程池。daemon 退出前应调用，保证 webhook 发送完成。"""
        if self._executor is not None:
            self._executor.shutdown(wait=wait)
            self._executor = None

    # ---- 内部 ------------------------------------------------------------

    def _dispatch(self, host: Host, kind: str) -> None:
        method_name = "notify_down" if kind == _KIND_DOWN else "notify_recover"
        for ch in self.channels:
            if not self._allow_send(ch, host, kind):
                continue
            method = getattr(ch, method_name)
            if self._executor is not None:
                self._executor.submit(self._safe_call, ch, method, host, kind)
            else:
                self._safe_call(ch, method, host, kind)

    def _allow_send(self, ch: Channel, host: Host, kind: str) -> bool:
        """防抖：min_interval_s>0 时同一 (channel, host, kind) 不重复发。

        首次发送不受窗口限制（monotonic 在进程刚启动时是 ~0，
        用 0 当 sentinel 会误判；用对象存在性做哨兵更稳）。
        """
        if self.min_interval_s <= 0:
            return True
        key = (ch.name, host.name, kind)
        last = self._last_sent.get(key)
        now = time.monotonic()
        if last is not None and now - last < self.min_interval_s:
            log.debug(
                "通知被防抖跳过",
                extra={"channel": ch.name, "host": host.name,
                       "kind": kind, "min_interval_s": self.min_interval_s,
                       "since_last_s": round(now - last, 2)},
            )
            return False
        self._last_sent[key] = now
        return True

    @staticmethod
    def _safe_call(ch: Channel, method, host: Host, kind: str) -> None:
        try:
            method(host)
        except Exception as e:                       # noqa: BLE001
            log.exception(
                "通过渠道发送通知失败",
                extra={"channel": ch.name, "host": host.name,
                       "kind": kind, "error": str(e)},
            )
