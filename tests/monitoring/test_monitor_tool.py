"""MonitorTool + MonitorStopTool atomic-tool tests.

Thin wrapper assertions: arg validation, alias kwargs, happy path
flowing through to MonitorRegistry.
"""

from __future__ import annotations

import time

import pytest

from sciagent.monitoring import MonitorRegistry
from sciagent.tools.atomic.monitor import MonitorStopTool, MonitorTool
from sciagent.tools.registry import BaseTool


@pytest.fixture(autouse=True)
def _reset():
    MonitorRegistry.reset_for_tests()
    BaseTool._shared_interrupt_event = None
    yield
    MonitorRegistry.reset_for_tests()
    BaseTool._shared_interrupt_event = None


def test_missing_command_returns_error():
    out = MonitorTool().execute()
    assert out.success is False
    assert "command is required" in (out.error or "")


def test_alias_cmd_kwarg_accepted():
    """Model often passes `cmd=` instead of `command=`. Accept it."""
    out = MonitorTool().execute(cmd="echo hi", desc="aliased")
    assert out.success is True
    assert out.output["watcher_id"].startswith("mon_")
    MonitorRegistry.instance().stop(out.output["watcher_id"])


def test_happy_path_returns_watcher_id():
    out = MonitorTool().execute(
        command="for i in 1 2 3; do echo step $i; done",
        description="counter",
    )
    assert out.success is True
    wid = out.output["watcher_id"]
    assert wid.startswith("mon_")
    assert out.output["description"] == "counter"
    assert "monitor_stop" in out.output["message"]
    # Drain should produce events for the lines emitted.
    deadline = time.monotonic() + 2.0
    events = []
    while time.monotonic() < deadline:
        events = MonitorRegistry.instance().drain()
        if events:
            break
        time.sleep(0.05)
    assert any("step 1" in e.line for e in events)
    MonitorRegistry.instance().stop(wid)


def test_default_description_when_omitted():
    """Description is required at the schema level for clarity, but if
    the agent forgets, fall back to a sentinel rather than failing."""
    out = MonitorTool().execute(command="echo hi")
    assert out.success is True
    assert out.output["description"] == "(unlabeled)"
    MonitorRegistry.instance().stop(out.output["watcher_id"])


def test_watcher_cap_returns_structured_error():
    """Beyond MAX_ACTIVE_WATCHERS, the tool returns a clear error so
    the agent knows to call monitor_stop. Doesn't crash the agent."""
    from sciagent.monitoring import MAX_ACTIVE_WATCHERS

    spawned = []
    try:
        for _ in range(MAX_ACTIVE_WATCHERS):
            o = MonitorTool().execute(command="sleep 5", description="filler")
            spawned.append(o.output["watcher_id"])

        out = MonitorTool().execute(command="sleep 5", description="overflow")
        assert out.success is False
        assert out.output["failure_type"] == "watcher_cap_reached"
        assert "cap reached" in (out.error or "")
    finally:
        for wid in spawned:
            MonitorStopTool().execute(watcher_id=wid)


# ---- stop tool ---------------------------------------------------------


def test_stop_missing_id_returns_error():
    out = MonitorStopTool().execute()
    assert out.success is False
    assert "watcher_id" in (out.error or "")


def test_stop_alias_kwarg_accepted():
    spawn = MonitorTool().execute(command="sleep 5", description="x")
    wid = spawn.output["watcher_id"]
    out = MonitorStopTool().execute(id=wid)  # alias for watcher_id
    assert out.success is True
    assert out.output["watcher_id"] == wid


def test_stop_unknown_id_returns_not_stopped():
    out = MonitorStopTool().execute(watcher_id="mon_nope")
    assert out.success is False
    assert out.output["stopped"] is False


def test_stop_finished_watcher_returns_exit_code():
    """A subprocess that exits cleanly: stop is idempotent and reports
    the cached exit code."""
    spawn = MonitorTool().execute(
        command="echo hi; exit 0",
        description="quick",
    )
    wid = spawn.output["watcher_id"]
    # Wait for the subprocess to finish.
    deadline = time.monotonic() + 2.0
    while time.monotonic() < deadline:
        info = MonitorRegistry.instance().list_watchers()
        if any(w["watcher_id"] == wid and w["finished"] for w in info):
            break
        time.sleep(0.02)

    out = MonitorStopTool().execute(watcher_id=wid)
    assert out.success is True
    assert out.output["exit_code"] == 0
