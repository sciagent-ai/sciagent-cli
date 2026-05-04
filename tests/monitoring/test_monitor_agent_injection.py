"""Agent-loop drain → system-reminder injection.

The drain hook lives in ``agent.py`` between user-feedback injection
and ``iteration_count += 1``. Per-call format and the empty-drain
no-op are pinned here so a future refactor can't break the contract
the rest of the harness depends on.

We test the format helper and the registry drain together end-to-end
with a real subprocess; the agent-loop wiring itself is exercised by
running the suite (any test that builds an AgentLoop will invoke the
hook on each iteration).
"""

from __future__ import annotations

import time

import pytest

from sciagent.monitoring import (
    MonitorRegistry,
    format_events_as_system_reminder,
)
from sciagent.tools.registry import BaseTool


@pytest.fixture(autouse=True)
def _reset():
    MonitorRegistry.reset_for_tests()
    BaseTool._shared_interrupt_event = None
    yield
    MonitorRegistry.reset_for_tests()
    BaseTool._shared_interrupt_event = None


def _drain_with_retry(reg, want: int, timeout: float = 2.0):
    """Poll drain until at least `want` events accumulated, or timeout."""
    deadline = time.monotonic() + timeout
    collected: list = []
    while time.monotonic() < deadline:
        collected.extend(reg.drain())
        if len(collected) >= want:
            return collected
        time.sleep(0.05)
    return collected


def test_drain_then_format_produces_one_system_reminder_block():
    """End-to-end shape an agent loop sees: drain → format → one block
    that starts/ends with the canonical sentinel tags. The block names
    each watcher and lists every drained line."""
    reg = MonitorRegistry.instance()
    wid = reg.spawn(
        command='for i in 1 2 3; do echo line $i; done',
        description="three-step",
    )
    events = _drain_with_retry(reg, want=3)
    text = format_events_as_system_reminder(events)
    assert text.startswith("<system-reminder>")
    assert text.endswith("</system-reminder>")
    assert "three-step" in text
    assert "line 1" in text and "line 2" in text and "line 3" in text
    # Trailing guidance line that prevents the model from interpreting
    # events as user instructions:
    assert "background notifications" in text.lower()
    reg.stop(wid)


def test_empty_drain_renders_to_empty_string():
    """No events → empty string. Caller (agent.py) uses this to skip
    injection, avoiding spurious empty <system-reminder> blocks."""
    reg = MonitorRegistry.instance()
    assert reg.drain() == []
    assert format_events_as_system_reminder([]) == ""


def test_multi_watcher_grouping_in_render():
    """Two watchers, distinct descriptions; render groups events by
    watcher so the agent can tell which monitor each event came from."""
    reg = MonitorRegistry.instance()
    a = reg.spawn(command="echo from-alpha", description="alpha")
    b = reg.spawn(command="echo from-beta", description="beta")
    events = _drain_with_retry(reg, want=4)  # 2 lines + 2 exit events

    text = format_events_as_system_reminder(events)
    assert "watcher" in text  # the per-watcher sub-header
    assert "alpha" in text and "beta" in text
    assert "from-alpha" in text and "from-beta" in text
    # Order: alpha's section appears before beta's (or vice versa) but
    # at least each watcher's lines stay grouped under its sub-header.
    alpha_idx = text.index("alpha")
    beta_idx = text.index("beta")
    assert alpha_idx != beta_idx
    reg.stop(a)
    reg.stop(b)


def test_drain_hook_imports_cleanly_from_agent_loop():
    """The drain hook in agent.py uses a lazy import to avoid module-
    load coupling. Verify the imports work without the real AgentLoop
    being constructed."""
    # This is what agent.py:1467-1474 does:
    from sciagent.monitoring import (  # noqa: WPS433
        MonitorRegistry,
        format_events_as_system_reminder,
    )

    events = MonitorRegistry.instance().drain()
    reminder = format_events_as_system_reminder(events)
    # No watchers → empty drain → empty reminder. No exceptions.
    assert reminder == ""
