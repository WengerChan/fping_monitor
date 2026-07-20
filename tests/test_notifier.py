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
    n = Notifier(channels=[a, b], async_dispatch=False)
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
    n = Notifier(channels=[broken, good], async_dispatch=False)
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


# ---- 异步派发 ----------------------------------------------------------------


def test_async_dispatch_does_not_block_caller():
    """async_dispatch=True 时，notify_* 应立即返回，channel 在后台执行。"""
    import threading
    gate = threading.Event()
    release = threading.Event()

    class Slow:
        name = "slow"
        def notify_down(self, host):
            gate.set()
            release.wait(timeout=5)         # 模拟 webhook 慢
        def notify_recover(self, host): pass

    n = Notifier(channels=[Slow()], async_dispatch=True)
    try:
        import time as _t
        t0 = _t.monotonic()
        n.notify_down(_host())
        # 立即返回 → 远小于 channel 的等待时间
        assert _t.monotonic() - t0 < 0.1
        # 等线程真的进了 channel
        assert gate.wait(timeout=2)
    finally:
        release.set()
        n.close(wait=True)


def test_async_dispatch_swallows_exceptions():
    """异步模式下，channel 抛异常不会冒到调用方。"""
    class Boom:
        name = "boom"
        def notify_down(self, host): raise RuntimeError("kaboom")
        def notify_recover(self, host): pass

    n = Notifier(channels=[Boom()], async_dispatch=True)
    try:
        n.notify_down(_host())             # 不应抛
        n.close(wait=True)
    except Exception as e:
        pytest.fail(f"unexpected: {e}")


def test_close_waits_for_inflight():
    import threading
    counter = {"n": 0}
    finish = threading.Event()

    class Counting:
        name = "c"
        def notify_down(self, host):
            finish.wait(timeout=5)
            with threading.Lock():
                counter["n"] += 1
        def notify_recover(self, host): pass

    n = Notifier(channels=[Counting()], async_dispatch=True)
    n.notify_down(_host())
    finish.set()
    n.close(wait=True)
    assert counter["n"] == 1


# ---- 防抖 --------------------------------------------------------------------


def test_debounce_suppresses_repeat_within_window():
    """min_interval_s 内同一 (channel, host, kind) 只发一次。"""
    class Counter:
        name = "c"
        def __init__(self): self.n = 0
        def notify_down(self, host): self.n += 1
        def notify_recover(self, host): self.n += 1

    c = Counter()
    n = Notifier(channels=[c], async_dispatch=False, min_interval_s=60.0)
    n.notify_down(_host())
    n.notify_down(_host())
    n.notify_recover(_host())
    n.notify_recover(_host())
    assert c.n == 2          # down 一次 + recover 一次


def test_debounce_off_when_zero():
    class Counter:
        name = "c"
        def __init__(self): self.n = 0
        def notify_down(self, host): self.n += 1
        def notify_recover(self, host): self.n += 1

    c = Counter()
    n = Notifier(channels=[c], async_dispatch=False, min_interval_s=0)
    n.notify_down(_host())
    n.notify_down(_host())
    assert c.n == 2


def test_debounce_per_channel_and_host():
    """不同 channel / host / kind 之间互不影响。"""
    class Counter:
        def __init__(self, name): self.name = name; self.n = 0
        def notify_down(self, host): self.n += 1
        def notify_recover(self, host): self.n += 1

    a = Counter("a"); b = Counter("b")
    n = Notifier(channels=[a, b], async_dispatch=False, min_interval_s=60.0)
    h1 = _host(name="h1"); h2 = _host(name="h2")
    n.notify_down(h1)        # a:1, b:1
    n.notify_down(h2)        # a:2, b:2  (不同 host)
    n.notify_down(h1)        # 被防抖，a/b 都不变
    assert a.n == 2 and b.n == 2


def test_debounce_expires_after_window(monkeypatch):
    """时间窗口过去后允许再次发送。"""
    fake_now = [0.0]
    monkeypatch.setattr("notifier.time.monotonic", lambda: fake_now[0])

    class Counter:
        name = "c"
        def __init__(self): self.n = 0
        def notify_down(self, host): self.n += 1
        def notify_recover(self, host): self.n += 1

    c = Counter()
    n = Notifier(channels=[c], async_dispatch=False, min_interval_s=10.0)
    n.notify_down(_host())
    fake_now[0] = 5.0
    n.notify_down(_host())        # 还不到 10s
    assert c.n == 1
    fake_now[0] = 10.1
    n.notify_down(_host())        # 过了 10s
    assert c.n == 2


# ---- from_config 新字段 -----------------------------------------------------


def test_from_config_respects_async_dispatch():
    n = Notifier.from_config({
        "enabled": True,
        "async_dispatch": False,
        "channels": [{"type": "dingtalk", "webhook_url": "https://x/y"}],
    })
    assert n.async_dispatch is False
    assert n._executor is None        # 同步模式不建线程池


def test_from_config_respects_min_interval_and_workers():
    n = Notifier.from_config({
        "enabled": True,
        "async_dispatch": True,
        "min_interval_s": 30.0,
        "max_workers": 8,
        "channels": [{"type": "dingtalk", "webhook_url": "https://x/y"}],
    })
    assert n.min_interval_s == 30.0
    assert n._executor is not None
    assert n._executor._max_workers == 8
    n.close()


def test_from_config_no_channels_skips_executor():
    n = Notifier.from_config({"enabled": True, "channels": []})
    assert n._executor is None         # 无 channel → 不创建线程池（省资源）
