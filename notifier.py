"""通知器：统一的 ``notify_down`` / ``notify_recover`` 接口。

通知渠道在 ``config.yaml`` 的 ``notify.channels`` 下配置，每个渠道接收
完整事件并自行决定如何格式化与发送。新增渠道只需在 ``_CHANNELS`` 注册
一个类，并在 YAML 里指定 ``type``。

当前支持的渠道：
  * ``dingtalk`` — 钉钉自定义机器人 Webhook（完整实现）
  * ``cuckoo``   — 内部布谷鸟告警平台占位（保留扩展点）
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import logging
import os
import time
import urllib.parse
from dataclasses import dataclass
from typing import Iterable, List, Protocol

import requests

from models import Host

log = logging.getLogger("fping_monitor.notifier")


class Channel(Protocol):
    """通知渠道协议。"""
    name: str
    def notify_down(self, host: Host) -> None: ...
    def notify_recover(self, host: Host) -> None: ...


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
    at_mobiles: List[str] = None             # type: ignore[assignment]
    at_all: bool = False
    timeout: float = 5.0

    def __post_init__(self) -> None:
        if self.at_mobiles is None:
            self.at_mobiles = []
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
                log.error("DingTalk POST 失败：%s %s", r.status_code, r.text[:200])
                return
            # 钉钉返回 {"errcode":0,"errmsg":"ok"}；非 0 视作业务失败
            try:
                body = r.json()
            except ValueError:
                return
            if body.get("errcode", 0) != 0:
                log.error("DingTalk 业务错误：%s", body)
        except requests.RequestException as e:
            log.error("DingTalk POST 异常：%s", e)

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


class Notifier:
    """把通知事件分发给所有已配置渠道，单个渠道失败不影响其他渠道。"""

    def __init__(self, channels: Iterable[Channel]):
        self.channels: List[Channel] = list(channels)

    @classmethod
    def from_config(cls, cfg: dict) -> "Notifier":
        """根据 YAML 配置构建 Notifier。

        行为：
            * ``enabled: false`` → 返回空 Notifier
            * 未知 type → 记 warning 并跳过
            * 渠道构造抛 ``NotImplementedError`` → 记 warning 并跳过
            * 渠道构造抛其他异常 → 记 error 并跳过
        """
        if not cfg.get("enabled", False):
            return cls(channels=[])
        channels: List[Channel] = []
        for entry in cfg.get("channels", []):
            ctype = entry.get("type")
            cls_ = _CHANNELS.get(ctype)
            if cls_ is None:
                log.warning("未知的通知渠道类型：%s", ctype)
                continue
            try:
                channels.append(cls_(**{k: v for k, v in entry.items() if k != "type"}))
            except NotImplementedError as e:
                log.warning("跳过未实现的渠道 %s：%s", ctype, e)
            except TypeError as e:
                log.error("渠道 %s 配置非法：%s", ctype, e)
        return cls(channels=channels)

    def notify_down(self, host: Host) -> None:
        for ch in self.channels:
            try:
                ch.notify_down(host)
            except Exception as e:                       # noqa: BLE001
                log.exception("通过 %s 发送 DOWN 失败：%s", ch.name, e)

    def notify_recover(self, host: Host) -> None:
        for ch in self.channels:
            try:
                ch.notify_recover(host)
            except Exception as e:                       # noqa: BLE001
                log.exception("通过 %s 发送 RECOVER 失败：%s", ch.name, e)
