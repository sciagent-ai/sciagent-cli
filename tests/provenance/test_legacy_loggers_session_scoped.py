"""ExecLogger / FetchLogger session-scoping (surface_merge_findings.md
Option 1 fix).

Before: both singletons defaulted to ``<cwd>/_logs/`` on first init and
never re-resolved. Cross-session, every sciagent run in the same
workspace appended to the same JSONL — the project log accumulated
forever and cross-contaminated.

After: when ``log_dir`` is not passed explicitly, the directory is
re-resolved on every read/write. If an active session id is set
(``set_active_session(sid)``), entries go to
``~/.sciagent/sessions/<sid>/_legacy_logs/``. When the active session
changes, subsequent entries follow. Explicit ``log_dir`` from init still
wins (back-compat for callers like ``ProvenanceChecker(log_dir=...)``).

Per ``feedback_no_mock_litellm.md``: tests use real loggers and a real
``set_active_session`` global, not patches.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from sciagent.provenance_log import (
    reset_provenance_logs,
    set_active_session,
)
from sciagent.tools.atomic.shell import ExecLogger
from sciagent.tools.atomic.web import FetchLogger


@pytest.fixture(autouse=True)
def _reset_active_session_and_singletons(monkeypatch, tmp_path):
    """Drop both singletons and the active-session global before and
    after each test. The loggers are module-level singletons, so a
    leftover instance from a previous test would mask the lazy
    re-resolution we're verifying."""
    ExecLogger._instance = None
    FetchLogger._instance = None
    # Also drop the module-level accessor caches so a fresh logger gets
    # built on first use.
    import sciagent.tools.atomic.shell as shell_mod
    import sciagent.tools.atomic.web as web_mod
    shell_mod._exec_logger = None
    web_mod._fetch_logger = None
    set_active_session(None)
    reset_provenance_logs()
    # Point the active-session base_dir at tmp_path for any session log
    # creation that happens during the test, even though these tests
    # don't directly touch ProvenanceLog.
    monkeypatch.chdir(tmp_path)
    yield
    ExecLogger._instance = None
    FetchLogger._instance = None
    shell_mod._exec_logger = None
    web_mod._fetch_logger = None
    set_active_session(None)
    reset_provenance_logs()


# ---------------------------------------------------------------------------
# ExecLogger
# ---------------------------------------------------------------------------


def test_exec_logger_writes_to_session_dir_when_active(tmp_path, monkeypatch):
    """When an active session id is set and no explicit log_dir was passed,
    entries land in ``~/.sciagent/sessions/<sid>/_legacy_logs/``."""
    fake_home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(fake_home))
    monkeypatch.setattr(Path, "home", lambda: fake_home)

    set_active_session("sess-abc")
    logger = ExecLogger()
    logger.log_execution(
        command="echo hi", exit_code=0, stdout="hi\n", stderr="",
        duration_seconds=0.01, timeout=False, working_dir=str(tmp_path),
    )

    expected = fake_home / ".sciagent" / "sessions" / "sess-abc" / "_legacy_logs" / "exec_log.jsonl"
    assert expected.exists(), f"exec log not at {expected}"
    entries = [json.loads(line) for line in expected.read_text().splitlines() if line.strip()]
    assert len(entries) == 1
    assert entries[0]["command"] == "echo hi"

    # And NOT in cwd/_logs.
    assert not (tmp_path / "_logs" / "exec_log.jsonl").exists()


def test_exec_logger_falls_back_to_cwd_when_no_session(tmp_path):
    """No active session, no explicit dir → cwd/_logs/exec_log.jsonl
    (preserves the pre-fix behavior for forensic / test paths)."""
    logger = ExecLogger()
    logger.log_execution(
        command="ls", exit_code=0, stdout="", stderr="",
        duration_seconds=0.01, timeout=False, working_dir=str(tmp_path),
    )
    expected = tmp_path / "_logs" / "exec_log.jsonl"
    assert expected.exists()
    entries = [json.loads(line) for line in expected.read_text().splitlines() if line.strip()]
    assert entries[0]["command"] == "ls"


def test_exec_logger_explicit_log_dir_overrides_session(tmp_path, monkeypatch):
    """If ProvenanceChecker (or any caller) passed log_dir explicitly,
    that path wins regardless of active session — back-compat for
    forensic ``ProvenanceChecker(log_dir=...)`` callers."""
    fake_home = tmp_path / "home"
    monkeypatch.setattr(Path, "home", lambda: fake_home)
    explicit_dir = tmp_path / "custom-logs"

    set_active_session("sess-ignored")
    logger = ExecLogger(log_dir=str(explicit_dir))
    logger.log_execution(
        command="echo override", exit_code=0, stdout="", stderr="",
        duration_seconds=0.01, timeout=False, working_dir=str(tmp_path),
    )
    assert (explicit_dir / "exec_log.jsonl").exists()
    # Did NOT leak into the session dir.
    assert not (fake_home / ".sciagent" / "sessions" / "sess-ignored").exists()


def test_exec_logger_follows_session_change_mid_process(tmp_path, monkeypatch):
    """Two sessions in the same Python process must land in distinct
    files. Pre-fix this failed: the singleton locked log_dir on first
    init and every later session piled onto the first file."""
    fake_home = tmp_path / "home"
    monkeypatch.setattr(Path, "home", lambda: fake_home)

    set_active_session("session-A")
    logger = ExecLogger()
    logger.log_execution(
        command="cmd-A", exit_code=0, stdout="", stderr="",
        duration_seconds=0.01, timeout=False, working_dir=str(tmp_path),
    )

    set_active_session("session-B")
    # Same singleton — must re-resolve, NOT reuse session-A's path.
    logger.log_execution(
        command="cmd-B", exit_code=0, stdout="", stderr="",
        duration_seconds=0.01, timeout=False, working_dir=str(tmp_path),
    )

    log_a = fake_home / ".sciagent" / "sessions" / "session-A" / "_legacy_logs" / "exec_log.jsonl"
    log_b = fake_home / ".sciagent" / "sessions" / "session-B" / "_legacy_logs" / "exec_log.jsonl"
    assert log_a.exists() and log_b.exists()
    entries_a = [json.loads(line) for line in log_a.read_text().splitlines() if line.strip()]
    entries_b = [json.loads(line) for line in log_b.read_text().splitlines() if line.strip()]
    assert [e["command"] for e in entries_a] == ["cmd-A"]
    assert [e["command"] for e in entries_b] == ["cmd-B"]


# ---------------------------------------------------------------------------
# FetchLogger (same shape; identical contract)
# ---------------------------------------------------------------------------


def test_fetch_logger_writes_to_session_dir_when_active(tmp_path, monkeypatch):
    fake_home = tmp_path / "home"
    monkeypatch.setattr(Path, "home", lambda: fake_home)

    set_active_session("sess-fetch")
    logger = FetchLogger()
    logger.log_fetch(
        url="https://example.org/data.csv",
        final_url="https://example.org/data.csv",
        status_code=200, content_type="text/csv",
        content="a,b\n1,2\n", success=True,
    )
    expected = fake_home / ".sciagent" / "sessions" / "sess-fetch" / "_legacy_logs" / "fetch_log.jsonl"
    assert expected.exists()
    entries = [json.loads(line) for line in expected.read_text().splitlines() if line.strip()]
    assert entries[0]["url"] == "https://example.org/data.csv"
    assert not (tmp_path / "_logs" / "fetch_log.jsonl").exists()


def test_fetch_logger_falls_back_to_cwd_when_no_session(tmp_path):
    logger = FetchLogger()
    logger.log_fetch(
        url="https://example.org/", final_url="https://example.org/",
        status_code=200, content_type="text/html", content="<html/>",
        success=True,
    )
    assert (tmp_path / "_logs" / "fetch_log.jsonl").exists()


def test_fetch_logger_follows_session_change_mid_process(tmp_path, monkeypatch):
    fake_home = tmp_path / "home"
    monkeypatch.setattr(Path, "home", lambda: fake_home)

    set_active_session("sess-1")
    logger = FetchLogger()
    logger.log_fetch(
        url="https://a.example/", final_url="https://a.example/",
        status_code=200, content_type="text/html", content="A",
        success=True,
    )
    set_active_session("sess-2")
    logger.log_fetch(
        url="https://b.example/", final_url="https://b.example/",
        status_code=200, content_type="text/html", content="B",
        success=True,
    )
    log_1 = fake_home / ".sciagent" / "sessions" / "sess-1" / "_legacy_logs" / "fetch_log.jsonl"
    log_2 = fake_home / ".sciagent" / "sessions" / "sess-2" / "_legacy_logs" / "fetch_log.jsonl"
    assert log_1.exists() and log_2.exists()
    urls_1 = [json.loads(l)["url"] for l in log_1.read_text().splitlines() if l.strip()]
    urls_2 = [json.loads(l)["url"] for l in log_2.read_text().splitlines() if l.strip()]
    assert urls_1 == ["https://a.example/"]
    assert urls_2 == ["https://b.example/"]


# ---------------------------------------------------------------------------
# Reader-side: get_recent_*, find_*, clear() use the lazy path correctly
# ---------------------------------------------------------------------------


def test_exec_logger_reads_from_current_session_dir(tmp_path, monkeypatch):
    """Reads (get_recent_executions, find_execution) must use the same
    lazy-resolved path that writes use — otherwise the EXEC gate sees
    entries from the wrong session."""
    fake_home = tmp_path / "home"
    monkeypatch.setattr(Path, "home", lambda: fake_home)

    set_active_session("sess-read")
    logger = ExecLogger()
    logger.log_execution(
        command="pytest -q", exit_code=0, stdout="ok", stderr="",
        duration_seconds=0.5, timeout=False, working_dir=str(tmp_path),
    )

    recent = logger.get_recent_executions(limit=10)
    assert len(recent) == 1
    assert recent[0]["command"] == "pytest -q"

    found = logger.find_execution("pytest")
    assert len(found) == 1
