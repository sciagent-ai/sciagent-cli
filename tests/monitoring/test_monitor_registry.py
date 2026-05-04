"""MonitorRegistry + Watcher unit tests.

Uses real subprocesses (`sh -c`) for accurate semantics — bufsize=1 +
text=True line-buffering is hard to mock faithfully without a process.
Tests are bounded to a few hundred ms each so the suite stays fast.
"""

from __future__ import annotations

import subprocess
import threading
import time

import pytest

from sciagent.monitoring import (
    MAX_ACTIVE_WATCHERS,
    MonitorRegistry,
    format_events_as_system_reminder,
)
from sciagent.tools.registry import BaseTool


@pytest.fixture(autouse=True)
def _clean_registry_and_event():
    MonitorRegistry.reset_for_tests()
    BaseTool._shared_interrupt_event = None
    yield
    MonitorRegistry.reset_for_tests()
    BaseTool._shared_interrupt_event = None


def _wait_for_finished(reg: MonitorRegistry, watcher_id: str, timeout: float = 3.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        for w in reg.list_watchers():
            if w["watcher_id"] == watcher_id and w["finished"]:
                return
        time.sleep(0.02)
    raise AssertionError(f"watcher {watcher_id} did not finish in {timeout}s")


# ---- spawn / drain happy path -----------------------------------------


def test_spawn_returns_immediately():
    """spawn() must return quickly — the reader does its work on a
    background thread. <50ms is conservative; in practice it's ~1ms."""
    reg = MonitorRegistry.instance()
    start = time.monotonic()
    wid = reg.spawn(command="sleep 5", description="long-runner")
    elapsed = time.monotonic() - start
    assert elapsed < 0.5, f"spawn took {elapsed:.3f}s — should be near-instant"
    assert wid.startswith("mon_")
    reg.stop(wid)


def test_three_lines_become_three_events_plus_exit():
    """Each stdout line → one MonitorEvent with monotonic seq; subprocess
    exit appends a synthetic [exit code N] event."""
    reg = MonitorRegistry.instance()
    wid = reg.spawn(
        command="for i in 1 2 3; do echo line $i; done",
        description="counter",
    )
    _wait_for_finished(reg, wid)
    events = reg.drain()
    lines = [e.line for e in events]
    seqs = [e.seq for e in events]
    assert lines[:3] == ["line 1", "line 2", "line 3"]
    assert any("[exit code 0]" in line for line in lines)
    assert seqs == sorted(seqs), "seq numbers must be monotonic"


def test_empty_lines_skipped():
    reg = MonitorRegistry.instance()
    wid = reg.spawn(
        command="echo line1; echo ''; echo line2",
        description="with-blanks",
    )
    _wait_for_finished(reg, wid)
    events = reg.drain()
    real_lines = [e.line for e in events if not e.line.startswith("[exit")]
    assert real_lines == ["line1", "line2"]


# ---- caps -------------------------------------------------------------


def test_per_watcher_cap_enforced():
    """50 lines emitted; default per-watcher cap of 20 → drain returns 20.
    Remaining events stay in the watcher's deque for the next drain."""
    reg = MonitorRegistry.instance()
    wid = reg.spawn(
        command='for i in $(seq 1 50); do echo line$i; done',
        description="bulk",
    )
    _wait_for_finished(reg, wid)
    first = reg.drain(max_events_per_watcher=20)
    assert len(first) == 20
    second = reg.drain(max_events_per_watcher=20)
    assert len(second) == 20
    third = reg.drain(max_events_per_watcher=20)
    # 50 lines + 1 exit = 51 events. After 40 drained, 11 remain.
    assert len(third) == 11


def test_total_cap_caps_across_watchers():
    """max_total clamps the sum across all watchers."""
    reg = MonitorRegistry.instance()
    a = reg.spawn(
        command='for i in $(seq 1 30); do echo a$i; done',
        description="a",
    )
    b = reg.spawn(
        command='for i in $(seq 1 30); do echo b$i; done',
        description="b",
    )
    _wait_for_finished(reg, a)
    _wait_for_finished(reg, b)
    out = reg.drain(max_events_per_watcher=20, max_total=25)
    assert len(out) == 25


# ---- stop / lifecycle -------------------------------------------------


def test_stop_unknown_returns_not_stopped():
    reg = MonitorRegistry.instance()
    out = reg.stop("mon_nonexistent")
    assert out["stopped"] is False
    assert out["watcher_id"] == "mon_nonexistent"


def test_stop_already_finished_returns_cached_exit_code():
    reg = MonitorRegistry.instance()
    wid = reg.spawn(command="echo hi; exit 7", description="quickly-dies")
    _wait_for_finished(reg, wid)
    out = reg.stop(wid)
    assert out["stopped"] is True
    assert out["exit_code"] == 7


def test_stop_kills_long_running_subprocess():
    """SIGTERM should reach a sleeping subprocess; the watcher's exit
    code reflects the termination."""
    reg = MonitorRegistry.instance()
    wid = reg.spawn(command="sleep 10", description="sleeper")
    time.sleep(0.05)  # let the subprocess actually start
    out = reg.stop(wid)
    assert out["stopped"] is True
    # On POSIX, SIGTERM terminates with exit code -15 (or 143 in shells).
    # Allow either; any non-None exit_code is fine.
    assert out["exit_code"] is not None


# ---- interrupt event --------------------------------------------------


def test_interrupt_event_stops_reader():
    """Setting BaseTool._shared_interrupt_event mid-stream should cause
    the reader to stop accepting new events and mark the watcher
    interrupted."""
    event = threading.Event()
    BaseTool.set_shared_interrupt_event(event)

    reg = MonitorRegistry.instance()
    # Subprocess that emits a line every 50ms forever (sort of).
    wid = reg.spawn(
        command='for i in $(seq 1 100); do echo line$i; sleep 0.05; done',
        description="slow",
    )
    time.sleep(0.15)
    event.set()
    # Give the reader a moment to notice and stop.
    time.sleep(0.2)
    info = next(w for w in reg.list_watchers() if w["watcher_id"] == wid)
    assert info["interrupted"] is True
    reg.stop(wid)


# ---- watcher cap ------------------------------------------------------


def test_spawn_beyond_cap_raises():
    reg = MonitorRegistry.instance()
    spawned = []
    try:
        for _ in range(MAX_ACTIVE_WATCHERS):
            spawned.append(reg.spawn(command="sleep 5", description="filler"))
        with pytest.raises(RuntimeError, match="cap reached"):
            reg.spawn(command="sleep 5", description="overflow")
    finally:
        for wid in spawned:
            reg.stop(wid)


# ---- format helper ----------------------------------------------------


def test_format_groups_by_watcher_and_includes_description():
    reg = MonitorRegistry.instance()
    a = reg.spawn(command="echo from-a", description="alpha")
    b = reg.spawn(command="echo from-b", description="beta")
    _wait_for_finished(reg, a)
    _wait_for_finished(reg, b)
    text = format_events_as_system_reminder(reg.drain())
    assert text.startswith("<system-reminder>")
    assert text.endswith("</system-reminder>")
    assert "alpha" in text
    assert "beta" in text
    assert "from-a" in text
    assert "from-b" in text
    assert "Background monitors" in text


def test_format_empty_input_returns_empty_string():
    """Empty drain → empty render. Caller must skip injection on empty
    so we don't add spurious system-reminders."""
    assert format_events_as_system_reminder([]) == ""
