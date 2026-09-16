"""Unit tests for M4 hardening in cli/main.py: connection retry with
backoff, and "one-line message always, traceback only under --verbose"
error reporting. Pure Python -- no database, no subprocess; `time.sleep`
is monkeypatched so a retry test never actually waits.
"""

from __future__ import annotations

import io
import sys

import pytest

from rowproof.cli.main import (
    _CONNECT_RETRY_ATTEMPTS,
    _connect_with_retry,
    _report_error,
    build_parser,
)


class _FlakyConnector:
    def __init__(self, fail_times: int, error_cls=ConnectionError):
        self.fail_times = fail_times
        self.error_cls = error_cls
        self.attempts = 0
        self.connected_dsn = None

    def connect(self, dsn: str) -> None:
        self.attempts += 1
        if self.attempts <= self.fail_times:
            raise self.error_cls(f"attempt {self.attempts} failed")
        self.connected_dsn = dsn


def test_connect_with_retry_succeeds_on_first_try_without_sleeping(monkeypatch):
    slept = []
    monkeypatch.setattr("rowproof.cli.main.time.sleep", lambda s: slept.append(s))
    c = _FlakyConnector(fail_times=0)
    _connect_with_retry(c, "dsn://x")
    assert c.attempts == 1
    assert c.connected_dsn == "dsn://x"
    assert slept == []


def test_connect_with_retry_recovers_after_transient_failures(monkeypatch):
    slept = []
    monkeypatch.setattr("rowproof.cli.main.time.sleep", lambda s: slept.append(s))
    c = _FlakyConnector(fail_times=_CONNECT_RETRY_ATTEMPTS - 1)
    _connect_with_retry(c, "dsn://x")
    assert c.attempts == _CONNECT_RETRY_ATTEMPTS
    assert c.connected_dsn == "dsn://x"
    assert len(slept) == _CONNECT_RETRY_ATTEMPTS - 1  # backoff between failed attempts only


def test_connect_with_retry_raises_the_last_error_after_exhausting_attempts(monkeypatch):
    monkeypatch.setattr("rowproof.cli.main.time.sleep", lambda s: None)
    c = _FlakyConnector(fail_times=_CONNECT_RETRY_ATTEMPTS + 5, error_cls=ConnectionError)
    with pytest.raises(ConnectionError):
        _connect_with_retry(c, "dsn://x")
    assert c.attempts == _CONNECT_RETRY_ATTEMPTS


def test_connect_with_retry_never_sleeps_after_the_final_attempt(monkeypatch):
    slept = []
    monkeypatch.setattr("rowproof.cli.main.time.sleep", lambda s: slept.append(s))
    c = _FlakyConnector(fail_times=_CONNECT_RETRY_ATTEMPTS + 5)
    with pytest.raises(ConnectionError):
        _connect_with_retry(c, "dsn://x")
    assert len(slept) == _CONNECT_RETRY_ATTEMPTS - 1


def test_report_error_always_prints_one_line_message(capsys):
    _report_error(ValueError("bad credentials"), verbose=False)
    captured = capsys.readouterr()
    assert captured.err.strip() == "error: bad credentials"


def test_report_error_omits_traceback_without_verbose(capsys):
    try:
        raise RuntimeError("boom")
    except RuntimeError as e:
        _report_error(e, verbose=False)
    captured = capsys.readouterr()
    assert "Traceback" not in captured.err


def test_report_error_appends_traceback_with_verbose(capsys):
    try:
        raise RuntimeError("boom")
    except RuntimeError as e:
        _report_error(e, verbose=True)
    captured = capsys.readouterr()
    assert "error: boom" in captured.err
    assert "Traceback" in captured.err
    assert "RuntimeError: boom" in captured.err


def test_report_error_custom_prefix():
    buf = io.StringIO()
    old_stderr = sys.stderr
    sys.stderr = buf
    try:
        _report_error(ValueError("x"), verbose=False, prefix="error (a -> b)")
    finally:
        sys.stderr = old_stderr
    assert buf.getvalue().strip() == "error (a -> b): x"


def test_cli_version_flag_prints_version_and_exits_zero(capsys):
    parser = build_parser()
    with pytest.raises(SystemExit) as exc_info:
        parser.parse_args(["--version"])
    assert exc_info.value.code == 0
    captured = capsys.readouterr()
    assert "rowproof" in captured.out
