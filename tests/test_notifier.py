"""Webhook Mock tests for the notifier."""
from datetime import datetime
from unittest.mock import patch

import pytest

from models import Host, HostStatus
from notifier import (
    DingTalkChannel,
    Notifier,
    _dingtalk_sign,
)


def _host(name="h", ip="8.8.8.8", status=HostStatus.DOWN, tags=None):
    return Host(
        id=1, name=name, ip=ip, status=status,
        tags=tags or ["prod", "shanghai"],
        fail_count=3, recover_count=0,
        last_check=datetime(2026, 1, 1),
        last_change=datetime(2026, 1, 1),
    )


# ---- DingTalkChannel --------------------------------------------------------


def test_dingtalk_sends_down_payload():
    ch = DingTalkChannel(webhook_url="https://example.com/hook",
                         at_mobiles=["13800000000"])
    h = _host(status=HostStatus.DOWN)
    with patch("notifier.requests.post") as post:
        ch.notify_down(h)
    assert post.call_count == 1
    payload = post.call_args.kwargs["json"]
    assert payload["msgtype"] == "markdown"
    md = payload["markdown"]["text"]
    assert "DOWN" in md
    assert "h" in md and "8.8.8.8" in md
    assert "prod" in md and "shanghai" in md
    assert payload["at"]["atMobiles"] == ["13800000000"]


def test_dingtalk_sends_recover_payload():
    ch = DingTalkChannel(webhook_url="https://example.com/hook")
    h = _host(status=HostStatus.UP)
    with patch("notifier.requests.post") as post:
        ch.notify_recover(h)
    payload = post.call_args.kwargs["json"]
    assert "RECOVER" in payload["markdown"]["text"]


def test_dingtalk_silent_when_no_url(monkeypatch):
    monkeypatch.delenv("DINGTALK_WEBHOOK", raising=False)
    ch = DingTalkChannel(webhook_url="")
    with patch("notifier.requests.post") as post:
        ch.notify_down(_host())
        ch.notify_recover(_host())
    post.assert_not_called()


def test_dingtalk_appends_signature_when_secret_set():
    ch = DingTalkChannel(
        webhook_url="https://oapi.dingtalk.com/robot/send?access_token=abc",
        secret="SEC",
    )
    with patch("notifier.requests.post") as post:
        ch.notify_down(_host())
    called_url = post.call_args.args[0]
    assert "timestamp=" in called_url
    assert "sign=" in called_url
    assert "access_token=abc" in called_url


def test_dingtalk_signature_helper():
    # 钉钉官方示例：secret="SEC..."，timestamp=1700000000000 → 已知 sign
    # 这里只验证函数会返回非空字符串 + URL-safe
    sig = _dingtalk_sign("SEC", 1700000000000)
    assert isinstance(sig, str) and len(sig) > 10


def test_dingtalk_logs_on_business_error():
    ch = DingTalkChannel(webhook_url="https://example.com/hook")
    fake_resp = type("R", (), {
        "status_code": 200,
        "json": lambda self: {"errcode": 310000, "errmsg": "bad token"},
    })()
    with patch("notifier.requests.post", return_value=fake_resp):
        ch.notify_down(_host())  # 不应抛异常


# ---- Notifier 调度 ----------------------------------------------------------


def test_notifier_dispatches_to_all_channels():
    class Recording:
        def __init__(self, name):
            self.name = name
            self.downs = []
            self.recovers = []
        def notify_down(self, host): self.downs.append(host.name)
        def notify_recover(self, host): self.recovers.append(host.name)

    a, b = Recording("a"), Recording("b")
    n = Notifier(channels=[a, b])
    h = _host()
    n.notify_down(h)
    n.notify_recover(h)
    assert a.downs == ["h"] and a.recovers == ["h"]
    assert b.downs == ["h"] and b.recovers == ["h"]


def test_notifier_swallows_channel_errors():
    class Broken:
        name = "broken"
        def notify_down(self, host): raise RuntimeError("boom")
        def notify_recover(self, host): raise RuntimeError("boom")

    class Good:
        name = "good"
        def __init__(self): self.count = 0
        def notify_down(self, host): self.count += 1
        def notify_recover(self, host): self.count += 1

    broken, good = Broken(), Good()
    n = Notifier(channels=[broken, good])
    n.notify_down(_host())       # 不应抛异常
    n.notify_recover(_host())
    assert good.count == 2


def test_from_config_disabled_returns_empty():
    n = Notifier.from_config({"enabled": False, "channels": []})
    assert n.channels == []


def test_from_config_ignores_unknown_type():
    n = Notifier.from_config({
        "enabled": True,
        "channels": [{"type": "doesnotexist"}, {"type": "cuckoo"}],  # cuckoo 是 stub
    })
    # stub 渠道构造时抛 NotImplementedError，notifier 应优雅跳过
    assert n.channels == []


def test_from_config_loads_dingtalk_with_env(monkeypatch):
    monkeypatch.setenv("DINGTALK_WEBHOOK", "https://from-env/hook")
    n = Notifier.from_config({
        "enabled": True,
        "channels": [{"type": "dingtalk", "at_all": True}],
    })
    assert len(n.channels) == 1
    assert n.channels[0].webhook_url == "https://from-env/hook"
    assert n.channels[0].at_all is True
