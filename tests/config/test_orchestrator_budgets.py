"""Unit tests for OrchestratorConfig kill-switch + verifier-model plumbing (H1 §3.1, §3.5)."""

from __future__ import annotations

import time
import types
from dataclasses import dataclass

import pytest

from sciagent.orchestrator import (
    BudgetExceeded,
    OrchestratorConfig,
    TaskOrchestrator,
)
from sciagent.tools.atomic.todo import TodoTool


def _make_orch(**config_kwargs) -> TaskOrchestrator:
    """Construct a TaskOrchestrator with one trivial pending task and no subagent."""
    todo = TodoTool()
    todo.execute(todos=[{
        "id": "t1",
        "content": "noop",
        "task_type": "general",
        "depends_on": [],
        "status": "pending",
    }])
    # Disable gates we're not testing so the loop is clean.
    base = dict(
        verbose=False,
        enable_data_gate=False,
        enable_exec_gate=False,
        enable_verification=False,
    )
    base.update(config_kwargs)
    return TaskOrchestrator(todo_tool=todo, config=OrchestratorConfig(**base))


def test_wall_time_cap_raises_after_elapsed():
    orch = _make_orch(max_wall_seconds=0)
    orch._start_time = time.time() - 5
    with pytest.raises(BudgetExceeded, match="max_wall_seconds=0 exceeded"):
        orch._check_budgets()


def test_wall_time_cap_passes_before_elapsed():
    orch = _make_orch(max_wall_seconds=3600)
    orch._start_time = time.time()
    orch._check_budgets()  # no raise


def test_wall_time_cap_unset_is_noop():
    orch = _make_orch()  # max_wall_seconds=None
    orch._start_time = time.time() - 1_000_000
    orch._check_budgets()  # still no raise


def test_cost_cap_with_field_present_raises():
    orch = _make_orch(max_cost_usd=1.0)
    orch._start_time = time.time()
    orch._cost_so_far = 2.5
    with pytest.raises(BudgetExceeded, match="max_cost_usd=1.0 exceeded"):
        orch._check_budgets()


def test_cost_cap_without_field_is_noop():
    """Pre-H3, _cost_so_far doesn't exist — the check skips silently."""
    orch = _make_orch(max_cost_usd=1.0)
    orch._start_time = time.time()
    # explicitly do NOT set orch._cost_so_far
    orch._check_budgets()  # no raise


def test_cost_cap_below_threshold_passes():
    orch = _make_orch(max_cost_usd=1.0)
    orch._start_time = time.time()
    orch._cost_so_far = 0.5
    orch._check_budgets()  # no raise


def test_check_budgets_before_execute_all_is_noop():
    """Before execute_all sets _start_time, the check is a no-op."""
    orch = _make_orch(max_wall_seconds=0)
    assert orch._start_time is None
    orch._check_budgets()  # no raise


def test_set_overrides_land_on_orchestrator_config():
    """--set semantics: config built via load_config flows through to OrchestratorConfig."""
    from sciagent.config import load_config
    cfg = load_config(overrides=["orchestrator.max_cost_usd=0.5", "orchestrator.max_wall_seconds=60"])
    assert cfg.orchestrator.max_cost_usd == 0.5
    assert cfg.orchestrator.max_wall_seconds == 60


def test_verifier_model_override_mutates_registered_subagent():
    """When OrchestratorConfig.verifier_model is set, the verifier subagent's model is swapped."""
    from sciagent.subagent import SubAgentOrchestrator

    sub = SubAgentOrchestrator(working_dir=".")
    default_model = sub.registry.get("verifier").model

    todo = TodoTool()
    cfg = OrchestratorConfig(verifier_model="openai/gpt-5-mini", verbose=False)
    TaskOrchestrator(todo_tool=todo, subagent_orchestrator=sub, config=cfg)

    swapped = sub.registry.get("verifier").model
    assert swapped == "openai/gpt-5-mini"
    assert swapped != default_model


def test_verifier_model_unset_keeps_default():
    """No mutation when verifier_model is None (preserves backward-compatible behavior)."""
    from sciagent.defaults import VERIFICATION_MODEL
    from sciagent.subagent import SubAgentOrchestrator

    sub = SubAgentOrchestrator(working_dir=".")
    todo = TodoTool()
    TaskOrchestrator(todo_tool=todo, subagent_orchestrator=sub, config=OrchestratorConfig(verbose=False))

    assert sub.registry.get("verifier").model == VERIFICATION_MODEL


def test_orchestrator_config_resolve_verifier_falls_back_to_default():
    from sciagent.defaults import VERIFICATION_MODEL
    assert OrchestratorConfig().resolve_verifier_model() == VERIFICATION_MODEL
    assert OrchestratorConfig(verifier_model="x/y").resolve_verifier_model() == "x/y"
