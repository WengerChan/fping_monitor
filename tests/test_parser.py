"""Tests for the fping output parser."""
from util import parse_fping_output


def test_parses_reachable_lines():
    out = (
        "8.8.8.8 : [0], 64 bytes, 0.12 ms (0.12 avg, 0% loss)\n"
        "1.1.1.1 : [0], 64 bytes, 0.45 ms (0.45 avg, 0% loss)\n"
    )
    assert parse_fping_output(out) == {"8.8.8.8": 0.12, "1.1.1.1": 0.45}


def test_strips_unreachable_from_stderr():
    out = "8.8.8.8 : [0], 64 bytes, 0.12 ms (0.12 avg, 0% loss)\n"
    err = "1.1.1.1 : unreachable\n"
    assert parse_fping_output(out, err) == {"8.8.8.8": 0.12}


def test_handles_empty_input():
    assert parse_fping_output("", "") == {}
    assert parse_fping_output("not fping output\n") == {}


def test_handles_malformed_lines_gracefully():
    out = (
        "garbage line\n"
        "8.8.8.8 : [0], 64 bytes, 0.30 ms (0.30 avg, 0% loss)\n"
        "8.8.4.4 : [0], 64 bytes, notanumber ms (0% loss)\n"
    )
    assert parse_fping_output(out) == {"8.8.8.8": 0.30}


def test_ignores_unrelated_stderr():
    out = "8.8.8.8 : [0], 64 bytes, 0.99 ms (0.99 avg, 0% loss)\n"
    err = "ICMP Time Exceeded from 10.0.0.1 for 8.8.8.8\n"
    assert parse_fping_output(out, err) == {"8.8.8.8": 0.99}
