"""Tests for the JSON log formatter."""
import io
import json
import logging
from datetime import datetime

import pytest

from util import JsonFormatter, TextFormatter, setup_logging


def _make_record(msg="hello", args=(), level=logging.INFO,
                 extra=None, exc_info=None):
    """手工构造 LogRecord，避免实际 logger 的副作用。"""
    record = logging.LogRecord(
        name="fping_monitor.test",
        level=level,
        pathname=__file__,
        lineno=1,
        msg=msg,
        args=args,
        exc_info=exc_info,
    )
    if extra:
        for k, v in extra.items():
            setattr(record, k, v)
    return record


# ---- JsonFormatter ----------------------------------------------------------


def test_basic_fields_present():
    line = JsonFormatter().format(_make_record("hi"))
    obj = json.loads(line)
    assert obj["level"] == "INFO"
    assert obj["logger"] == "fping_monitor.test"
    assert obj["message"] == "hi"
    # ts 是 ISO 8601 且能 parse 回来
    datetime.fromisoformat(obj["ts"])


def test_extra_fields_merged():
    rec = _make_record("notify",
                       extra={"event": "state_change", "host": "db1",
                              "tags": ["prod", "db"], "from": "UP", "to": "DOWN"})
    obj = json.loads(JsonFormatter().format(rec))
    assert obj["event"] == "state_change"
    assert obj["host"] == "db1"
    assert obj["tags"] == ["prod", "db"]
    assert obj["from"] == "UP"
    assert obj["to"] == "DOWN"


def test_args_get_substituted_into_message():
    rec = _make_record("本周期完成：%d 个状态变更", args=(3,))
    obj = json.loads(JsonFormatter().format(rec))
    assert obj["message"] == "本周期完成：3 个状态变更"


def test_chinese_unicode_safe():
    rec = _make_record("主机 %s 不可达", args=("网关-上海",))
    obj = json.loads(JsonFormatter().format(rec))
    assert "网关-上海" in obj["message"]


def test_exception_info_included():
    try:
        raise ValueError("boom")
    except ValueError:
        import sys
        rec = _make_record("失败", exc_info=sys.exc_info())
    obj = json.loads(JsonFormatter().format(rec))
    assert "exc_info" in obj
    assert "ValueError: boom" in obj["exc_info"]


def test_reserved_fields_not_leaked():
    """LogRecord 内置字段不应出现在 extra 字段中。"""
    rec = _make_record("x", extra={"host": "h1"})
    obj = json.loads(JsonFormatter().format(rec))
    # 内置字段不应作为业务字段出现
    assert "args" not in obj
    assert "pathname" not in obj
    assert "funcName" not in obj
    assert obj["host"] == "h1"


def test_complex_value_serialized_as_string():
    """不可 JSON 序列化的值走 default=str。"""
    rec = _make_record("x", extra={"now": datetime(2026, 7, 17, 15, 30)})
    obj = json.loads(JsonFormatter().format(rec))
    assert "2026-07-17" in obj["now"]


# ---- setup_logging 集成 -----------------------------------------------------


def test_setup_logging_json_format(tmp_path):
    logger = setup_logging(level="INFO", log_dir=str(tmp_path), fmt="json")
    buf = io.StringIO()
    logger.handlers[-1].stream = buf   # 替换 stream handler 的输出
    logger.info("test msg", extra={"host": "h1"})
    line = buf.getvalue().strip()
    obj = json.loads(line)
    assert obj["message"] == "test msg"
    assert obj["host"] == "h1"


def test_setup_logging_text_format(tmp_path):
    logger = setup_logging(level="INFO", log_dir=str(tmp_path), fmt="text")
    buf = io.StringIO()
    logger.handlers[-1].stream = buf
    logger.info("text msg")
    line = buf.getvalue().strip()
    # text 格式不是 JSON
    with pytest.raises(json.JSONDecodeError):
        json.loads(line)
    assert "text msg" in line
    assert "[INFO]" in line


def test_setup_logging_rebuilds_on_format_change(tmp_path):
    """第二次 setup 改 fmt 时应真的把 handler 换掉。"""
    logger = setup_logging(level="INFO", log_dir=str(tmp_path), fmt="json")
    json_handlers = [h for h in logger.handlers
                     if isinstance(h.formatter, JsonFormatter)]
    assert len(json_handlers) >= 1

    setup_logging(level="INFO", log_dir=str(tmp_path), fmt="text")
    # 全部 handler 现在应该是 TextFormatter
    assert all(isinstance(h.formatter, TextFormatter) for h in logger.handlers)
    # 旧 JsonFormatter handler 应已被移除
    assert not any(isinstance(h.formatter, JsonFormatter) for h in logger.handlers)
