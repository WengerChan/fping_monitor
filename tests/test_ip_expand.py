"""Tests for IP spec expansion (CIDR / range / single)."""
import pytest

from util import MAX_HOSTS_PER_SPEC, expand_ip_spec


# ---- 单 IP ------------------------------------------------------------------


def test_single_ip():
    assert expand_ip_spec("10.1.2.3") == ["10.1.2.3"]


def test_single_ip_strip_whitespace():
    assert expand_ip_spec("  10.1.2.3  ") == ["10.1.2.3"]


# ---- CIDR -------------------------------------------------------------------


def test_cidr_slash_24_excludes_network_and_broadcast():
    # /24 → 256 个地址，剔除 net/broadcast 后剩 254
    result = expand_ip_spec("10.1.2.0/24")
    assert len(result) == 254
    assert result[0] == "10.1.2.1"
    assert result[-1] == "10.1.2.254"


def test_cidr_slash_30_excludes_net_broadcast():
    # /30 = 4 个地址，net=10.1.2.0, broadcast=10.1.2.3 排除，剩 2 个
    result = expand_ip_spec("10.1.2.0/30")
    assert result == ["10.1.2.1", "10.1.2.2"]


def test_cidr_slash_31_keeps_both():
    # /31 只有 2 个地址，没有 hosts()，全保留
    result = expand_ip_spec("10.1.2.0/31")
    assert result == ["10.1.2.0", "10.1.2.1"]


def test_cidr_slash_32_single():
    result = expand_ip_spec("10.1.2.5/32")
    assert result == ["10.1.2.5"]


def test_cidr_slash_28():
    result = expand_ip_spec("10.1.2.0/28")
    assert len(result) == 14
    assert result[0] == "10.1.2.1"
    assert result[-1] == "10.1.2.14"


# ---- 范围 -------------------------------------------------------------------


def test_range_full():
    result = expand_ip_spec("10.1.2.3-10.1.2.10")
    assert result == [
        "10.1.2.3", "10.1.2.4", "10.1.2.5", "10.1.2.6",
        "10.1.2.7", "10.1.2.8", "10.1.2.9", "10.1.2.10",
    ]


def test_range_short_form():
    """短范围：第二个值是 IP 最后一段数字。"""
    result = expand_ip_spec("10.1.2.3-10")
    assert len(result) == 8
    assert result[0] == "10.1.2.3"
    assert result[-1] == "10.1.2.10"


def test_range_short_form_with_zero():
    result = expand_ip_spec("10.1.2.0-3")
    assert result == ["10.1.2.0", "10.1.2.1", "10.1.2.2", "10.1.2.3"]


def test_range_single():
    result = expand_ip_spec("10.1.2.5-10.1.2.5")
    assert result == ["10.1.2.5"]


# ---- 非法 / 边界 ------------------------------------------------------------


def test_empty_string_raises():
    with pytest.raises(ValueError, match="不能为空"):
        expand_ip_spec("")


def test_whitespace_only_raises():
    with pytest.raises(ValueError, match="不能为空"):
        expand_ip_spec("   ")


def test_non_string_raises():
    with pytest.raises(ValueError, match="必须是字符串"):
        expand_ip_spec(123)


def test_invalid_ip_raises():
    with pytest.raises(ValueError, match="非法 IP"):
        expand_ip_spec("abc")


def test_invalid_cidr_raises():
    with pytest.raises(ValueError, match="非法 CIDR"):
        expand_ip_spec("10.1.2.0/33")


def test_range_start_after_end_raises():
    with pytest.raises(ValueError, match="小于起始"):
        expand_ip_spec("10.1.2.10-10.1.2.3")


def test_range_short_form_out_of_range_raises():
    with pytest.raises(ValueError, match="超出 0-255"):
        expand_ip_spec("10.1.2.3-256")


def test_range_short_form_non_numeric_raises():
    with pytest.raises(ValueError, match="最后一段数字"):
        expand_ip_spec("10.1.2.3-abc")


def test_cidr_too_large_raises():
    # /8 是 16M+ 地址，超过上限
    with pytest.raises(ValueError, match="超过单条上限"):
        expand_ip_spec("10.0.0.0/8")


def test_range_too_large_raises():
    # 构造一个 MAX+1 个地址的范围（10.0.0.0 到 10.0.4.0）
    import ipaddress
    start = ipaddress.IPv4Address("10.0.0.0")
    end = ipaddress.IPv4Address(int(start) + MAX_HOSTS_PER_SPEC)
    with pytest.raises(ValueError, match="超过单条上限"):
        expand_ip_spec(f"{start}-{end}")


# ---- 集成：Database.upsert_hosts -------------------------------------------


def test_upsert_single_ip_keeps_name(db):
    db.upsert_hosts([{"name": "gw", "ip": "1.1.1.1"}])
    h = db.get_host_by_name("gw")
    assert h is not None
    assert h.ip == "1.1.1.1"


def test_upsert_cidr_expands_and_appends_ip_suffix(db):
    db.upsert_hosts([{"name": "web", "ip": "10.1.2.0/30"}])
    hosts = db.list_hosts()
    # /30 = 4 地址，net/broadcast 排除后剩 2 个
    assert len(hosts) == 2
    names = [h.name for h in hosts]
    assert names == ["web-10.1.2.1", "web-10.1.2.2"]
    # tags 同步
    db.upsert_hosts([{"name": "web", "ip": "10.1.2.0/30", "tags": ["web", "prod"]}])
    for h in hosts:
        h2 = db.get_host_by_name(h.name)
        assert h2.tags == ["web", "prod"]


def test_upsert_short_range(db):
    db.upsert_hosts([{"name": "db", "ip": "10.1.3.3-5"}])
    names = [h.name for h in db.list_hosts()]
    assert names == ["db-10.1.3.3", "db-10.1.3.4", "db-10.1.3.5"]


def test_upsert_list_form_mixes_specs(db):
    db.upsert_hosts([{
        "name": "mix",
        "ip": ["8.8.8.8", "1.1.1.0/30"],
        "tags": ["ext"],
    }])
    names = sorted(h.name for h in db.list_hosts())
    # /30 排除 net/broadcast 后剩 1.1.1.1, 1.1.1.2
    assert names == ["mix-1.1.1.1", "mix-1.1.1.2", "mix-8.8.8.8"]


def test_upsert_rejects_invalid_spec(db):
    with pytest.raises(ValueError, match="非法起始 IP|非法 IP|不能为空|非法 CIDR"):
        db.upsert_hosts([{"name": "bad", "ip": "not-an-ip"}])


def test_upsert_rejects_missing_name(db):
    with pytest.raises(ValueError, match="name"):
        db.upsert_hosts([{"ip": "1.1.1.1"}])


def test_upsert_rejects_missing_ip(db):
    with pytest.raises(ValueError, match="ip"):
        db.upsert_hosts([{"name": "x"}])
