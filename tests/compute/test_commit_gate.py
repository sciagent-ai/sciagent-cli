"""Tests for the ask_user commit gate.

The gate fires above ~$5 (configurable) at the tool layer, not the prompt —
the LLM cannot bypass it. In a non-interactive shell, the gate logs a
warning and proceeds (batch-runs-don't-break fallback). When raised via env
high enough that no gate fires, the launch must proceed unchallenged.
"""

from __future__ import annotations

import os
from unittest.mock import MagicMock, patch

import pytest

from sciagent.tools.atomic.compute import (
    ComputeTool,
    _load_commit_threshold_usd,
    _commit_gate_prompt,
    _DEFAULT_COMMIT_THRESHOLD_USD,
)


@pytest.fixture(autouse=True)
def _clean_threshold_env(monkeypatch):
    """Most tests want to assert against the default; clear the env so a
    developer-side override doesn't leak in."""
    monkeypatch.delenv("SCIAGENT_COMPUTE_COMMIT_THRESHOLD_USD", raising=False)
    yield


def test_default_threshold_is_5_dollars():
    assert _load_commit_threshold_usd() == _DEFAULT_COMMIT_THRESHOLD_USD == 5.0


def test_env_override_wins_over_default(monkeypatch):
    monkeypatch.setenv("SCIAGENT_COMPUTE_COMMIT_THRESHOLD_USD", "100")
    assert _load_commit_threshold_usd() == 100.0


def test_non_interactive_shell_proceeds_with_warning(caplog):
    """sys.stdin.isatty() is False under pytest; the gate must fall through
    to None (= caller proceeds with original args) and log a warning. Don't
    break batch / programmatic runs."""
    import logging

    with caplog.at_level(logging.WARNING, logger="sciagent.tools.atomic.compute"):
        decision = _commit_gate_prompt(
            proposed_total_usd=50.0,
            threshold_usd=5.0,
            menu=[{"label": "minimum", "available": True, "num_nodes": 1, "spot": True, "estimated_total_usd": 5.0, "estimated_hourly_usd": 5.0, "instance": "x", "cloud": "aws", "region": "r", "over_budget": False}],
        )
    assert decision is None  # signal: caller proceeds with original args
    assert any("commit threshold" in rec.message.lower() for rec in caplog.records)


def _stub_router_for_skypilot(estimated_total_usd, estimated_hourly_usd=None):
    from sciagent.compute.job import StorageMount, StorageMode

    fake_router = MagicMock()
    fake_skypilot = MagicMock()
    fake_skypilot.name = "skypilot"
    fake_skypilot.run.return_value = ("sciagent-job-1", 1)
    fake_skypilot.build_outputs_mount.return_value = None
    fake_skypilot.build_input_mounts.return_value = []
    # Auto-mounted durable session workspace (P0.5). compute_run now calls
    # this when workspace_source is None; return a real StorageMount so
    # downstream attribute access (kind, store, path) gets strings, not
    # MagicMocks.
    fake_skypilot.build_session_workspace_mount.return_value = StorageMount(
        path="/workspace",
        bucket="sciagent-workspace-test-sess",
        store="s3",
        mode=StorageMode.MOUNT,
        source=None,
        persistent=True,
        kind="durable",
    )
    fake_router._backends = {"skypilot": fake_skypilot}
    fake_router.list_backends.return_value = ["skypilot"]
    fake_router.select.return_value = (fake_skypilot, "test routing")
    if estimated_hourly_usd is None:
        estimated_hourly_usd = estimated_total_usd
    fake_router.estimate_cost.return_value = {
        "estimated_hourly": estimated_hourly_usd,
        "estimated_total": estimated_total_usd,
    }
    fake_router.estimate_menu.return_value = [
        {"label": "minimum", "available": True, "num_nodes": 1, "spot": True, "estimated_total_usd": estimated_total_usd, "estimated_hourly_usd": estimated_hourly_usd, "instance": "x", "cloud": "aws", "region": "r", "over_budget": False},
    ]
    return fake_router, fake_skypilot


def test_gate_does_not_fire_below_threshold():
    """Sub-threshold launches proceed without prompting. estimate_menu is
    never called (no menu rendered for sub-$5 runs)."""
    fake_router, fake_sky = _stub_router_for_skypilot(estimated_total_usd=2.50)
    tool = ComputeTool(working_dir=".")
    tool._router = fake_router

    out = tool.execute(command="echo hi", image="python:3.11", backend="skypilot")

    assert out.success is True
    fake_sky.run.assert_called_once()
    fake_router.estimate_menu.assert_not_called()


def test_gate_fires_above_threshold_in_interactive_shell():
    """When stdin/stdout are a TTY and total > threshold, the prompt fires.
    Simulate the user answering 'n' to confirm the abort path returns a
    structured commit_gate_aborted failure."""
    fake_router, fake_sky = _stub_router_for_skypilot(estimated_total_usd=50.0)
    tool = ComputeTool(working_dir=".")
    tool._router = fake_router

    # Force the gate's interactive-detection True; capture the prompt.
    with patch("sciagent.tools.atomic.compute.sys") as fake_sys, \
         patch("builtins.input", return_value="n") as fake_input:
        fake_sys.stdin.isatty.return_value = True
        fake_sys.stdout.isatty.return_value = True
        # stderr passthrough — print() in the gate writes there.
        import sys as real_sys
        fake_sys.stderr = real_sys.stderr
        out = tool.execute(command="echo hi", image="python:3.11", backend="skypilot")

    fake_input.assert_called_once()
    assert out.success is False
    assert out.output["failure_type"] == "commit_gate_aborted"
    assert out.output["estimated_total_usd"] == 50.0
    assert out.output["threshold_usd"] == 5.0
    fake_sky.run.assert_not_called()
    fake_router.estimate_menu.assert_called_once()  # menu shown to user


def test_gate_fires_then_user_confirms_proceeds_to_launch():
    """User typing 'y' at the prompt continues into selected_backend.run."""
    fake_router, fake_sky = _stub_router_for_skypilot(estimated_total_usd=50.0)
    tool = ComputeTool(working_dir=".")
    tool._router = fake_router

    with patch("sciagent.tools.atomic.compute.sys") as fake_sys, \
         patch("builtins.input", return_value="y"):
        fake_sys.stdin.isatty.return_value = True
        fake_sys.stdout.isatty.return_value = True
        import sys as real_sys
        fake_sys.stderr = real_sys.stderr
        out = tool.execute(command="echo hi", image="python:3.11", backend="skypilot")

    assert out.success is True
    fake_sky.run.assert_called_once()


def test_env_override_high_enough_skips_gate(monkeypatch):
    """SCIAGENT_COMPUTE_COMMIT_THRESHOLD_USD raised above the proposed total
    suppresses the gate entirely — no prompt, straight to launch."""
    monkeypatch.setenv("SCIAGENT_COMPUTE_COMMIT_THRESHOLD_USD", "1000")
    fake_router, fake_sky = _stub_router_for_skypilot(estimated_total_usd=50.0)
    tool = ComputeTool(working_dir=".")
    tool._router = fake_router

    with patch("builtins.input") as fake_input:
        out = tool.execute(command="echo hi", image="python:3.11", backend="skypilot")
        fake_input.assert_not_called()  # never prompted

    assert out.success is True
    fake_sky.run.assert_called_once()
    fake_router.estimate_menu.assert_not_called()


def test_gate_skipped_when_estimate_only():
    """estimate_only is the menu surface itself; the gate must not fire on
    top of it (would be confusing — the user is explicitly asking for the
    menu, not for a launch)."""
    fake_router, fake_sky = _stub_router_for_skypilot(estimated_total_usd=50.0)
    tool = ComputeTool(working_dir=".")
    tool._router = fake_router

    with patch("builtins.input") as fake_input:
        out = tool.execute(
            command="echo hi", image="python:3.11", backend="skypilot",
            estimate_only=True,
        )
        fake_input.assert_not_called()

    assert out.success is True
    assert "options" in out.output
    fake_sky.run.assert_not_called()


def test_non_interactive_50_dollar_launch_does_not_crash():
    """Integration of the fallback contract: in a non-interactive shell, a
    $50 launch logs a warning and proceeds — does NOT crash and does NOT
    return a structured failure. Programmatic API callers and CI batch
    runs depend on this."""
    fake_router, fake_sky = _stub_router_for_skypilot(estimated_total_usd=50.0)
    tool = ComputeTool(working_dir=".")
    tool._router = fake_router

    # pytest stdin/stdout are non-TTYs by default, but be explicit:
    with patch("sciagent.tools.atomic.compute.sys") as fake_sys:
        fake_sys.stdin.isatty.return_value = False
        fake_sys.stdout.isatty.return_value = False
        import sys as real_sys
        fake_sys.stderr = real_sys.stderr
        out = tool.execute(command="echo hi", image="python:3.11", backend="skypilot")

    assert out.success is True, f"non-interactive must not abort: {out.error}"
    fake_sky.run.assert_called_once()
