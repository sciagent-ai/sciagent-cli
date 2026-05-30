"""Unit tests for H6 — RunCostTracker, budget kill-switch with cluster
cleanup, GPU smoke tests (DESIGN_HARNESS.md §8.6).

No litellm mocking — the LLM-cost path is exercised by feeding the
per-call cost number directly into ``record_llm_call``, which is the same
function ``llm.py:_capture_last_usage`` calls in production. The cluster
cost path stubs sky.cost_report() with synthetic rows so the tests run
offline.
"""

from __future__ import annotations

from typing import Any, Dict, List
from unittest.mock import MagicMock, patch

import pytest

from sciagent.run_cost import (
    RunCostTracker,
    set_active_cost_tracker,
    get_active_cost_tracker,
)
from sciagent.orchestrator import (
    BudgetExceeded,
    OrchestratorConfig,
    TaskOrchestrator,
)
from sciagent.compute.job import Job, ComputeRequirements
from sciagent.compute.router import ComputeRouter
from sciagent.compute.backends.skypilot import SkyPilotBackend
from sciagent.tools.atomic.todo import TodoTool


# ---------------------------------------------------------------------------
# RunCostTracker — axis isolation
# ---------------------------------------------------------------------------


def test_record_llm_call_lands_on_llm_axis_only():
    """A single LLM call's cost goes to llm_cost_usd; compute/storage stay 0."""
    t = RunCostTracker()
    t.record_llm_call(0.42)

    assert t.llm_cost_usd == pytest.approx(0.42)
    assert t.compute_cost_usd == 0.0
    assert t.storage_cost_usd == 0.0
    assert t.total_usd == pytest.approx(0.42)


def test_record_llm_call_tolerates_missing_cost():
    """Providers that don't report cost pass None — the call must no-op."""
    t = RunCostTracker()
    t.record_llm_call(None)
    t.record_llm_call("not a number")  # type: ignore[arg-type]
    t.record_llm_call(-0.01)  # nonsensical; ignore rather than subtract

    assert t.total_usd == 0.0


def test_record_llm_call_accumulates_across_calls():
    """Multiple calls sum on the llm axis; not on compute or storage."""
    t = RunCostTracker()
    for cost in (0.10, 0.20, 0.05):
        t.record_llm_call(cost)

    assert t.llm_cost_usd == pytest.approx(0.35)
    assert t.compute_cost_usd == 0.0


# ---------------------------------------------------------------------------
# RunCostTracker — poll_active_clusters
# ---------------------------------------------------------------------------


class _FakeRouter:
    """Stub for ComputeRouter exposing only the methods the tracker calls."""

    def __init__(self, rows_by_call: List[List[Dict[str, Any]]]):
        # Each entry is the list of rows the next cost_report() invocation
        # should return. Lets a test verify idempotency by feeding the same
        # rows twice.
        self._rows = list(rows_by_call)
        self.calls: List[List[str]] = []

    def cost_report(self, cluster_names=None):
        self.calls.append(list(cluster_names or []))
        if not self._rows:
            return []
        return self._rows.pop(0)


def test_poll_active_clusters_routes_to_compute_axis():
    """sky.cost_report rows land on compute_cost_usd; llm axis untouched."""
    t = RunCostTracker(session_id="sess-1")
    t.record_llm_call(0.05)  # llm axis seeded

    router = _FakeRouter(rows_by_call=[
        [
            {"name": "sciagent-c1", "total_cost": 1.20, "duration": 360},
            {"name": "sciagent-c2", "total_cost": 0.80, "duration": 240},
        ],
    ])

    t.poll_active_clusters(
        cluster_names=["sciagent-c1", "sciagent-c2"],
        router=router,
        provenance_log=None,
    )

    assert t.compute_cost_usd == pytest.approx(2.00)
    assert t.llm_cost_usd == pytest.approx(0.05)
    assert t.total_usd == pytest.approx(2.05)


def test_poll_active_clusters_is_idempotent():
    """Polling twice with the same numbers must NOT double-count.

    Idempotency is non-negotiable per design §8.3 — the aggregator
    recomputes from sky's authoritative numbers each call rather than
    incrementing locally."""
    rows = [
        {"name": "sciagent-c1", "total_cost": 1.20, "duration": 360},
        {"name": "sciagent-c2", "total_cost": 0.80, "duration": 240},
    ]
    t = RunCostTracker(session_id="sess-1")
    router = _FakeRouter(rows_by_call=[rows, list(rows)])

    t.poll_active_clusters(cluster_names=["sciagent-c1", "sciagent-c2"], router=router)
    first = t.compute_cost_usd
    t.poll_active_clusters(cluster_names=["sciagent-c1", "sciagent-c2"], router=router)

    assert t.compute_cost_usd == pytest.approx(first)
    assert t.compute_cost_usd == pytest.approx(2.00)


def test_poll_active_clusters_emits_provenance_row_per_cluster():
    """Each cluster's realized cost lands as a compute_cost_observed event."""
    t = RunCostTracker(session_id="sess-1")
    router = _FakeRouter(rows_by_call=[[
        {"name": "sciagent-c1", "total_cost": 1.20, "duration": 360, "instance_type": "g5.xlarge"},
    ]])
    log = MagicMock()

    t.poll_active_clusters(
        cluster_names=["sciagent-c1"],
        router=router,
        provenance_log=log,
    )

    log.emit_compute_cost_observed.assert_called_once()
    _, kwargs = log.emit_compute_cost_observed.call_args
    assert kwargs["cluster_name"] == "sciagent-c1"
    assert kwargs["cost_usd"] == pytest.approx(1.20)
    assert kwargs["wall_seconds"] == pytest.approx(360.0)
    assert kwargs["cost_source"] == "sky_cost_report"
    assert kwargs["instance_type"] == "g5.xlarge"


def test_poll_active_clusters_empty_when_no_clusters():
    """Nothing to poll → no-op, compute axis stays at 0."""
    t = RunCostTracker(session_id="sess-1")
    router = _FakeRouter(rows_by_call=[[]])
    t.poll_active_clusters(cluster_names=[], router=router)
    assert t.compute_cost_usd == 0.0


def test_poll_active_clusters_swallows_router_exception():
    """A sky.cost_report failure must not crash the orchestrator loop."""
    t = RunCostTracker(session_id="sess-1")

    class _BrokenRouter:
        def cost_report(self, cluster_names=None):
            raise RuntimeError("sky transient failure")

    # Should not raise.
    t.poll_active_clusters(cluster_names=["sciagent-c1"], router=_BrokenRouter())
    assert t.compute_cost_usd == 0.0


# ---------------------------------------------------------------------------
# finalize_storage
# ---------------------------------------------------------------------------


def test_finalize_storage_emits_session_end_fallback():
    """Storage axis ships $0 via session_end_fallback per design §12."""
    t = RunCostTracker(session_id="sess-1")
    log = MagicMock()

    t.finalize_storage(provenance_log=log)

    # Storage row + any cluster fallback rows are all session_end_fallback.
    storage_rows = [
        c for c in log.emit_compute_cost_observed.call_args_list
        if c.kwargs.get("cluster_name") == "__storage__"
    ]
    assert len(storage_rows) == 1
    assert storage_rows[0].kwargs["cost_source"] == "session_end_fallback"
    assert storage_rows[0].kwargs["cost_usd"] == 0.0


def test_finalize_storage_records_fallback_for_unreported_clusters():
    """A cluster we polled but sky never reported on gets a $0 fallback row.

    Lets the bench compute compute_cost_usd = sum(compute_cost_observed)
    as an invariant — a cluster that left no realized row still surfaces."""
    t = RunCostTracker(session_id="sess-1")
    router = _FakeRouter(rows_by_call=[[]])  # sky reports nothing
    t.poll_active_clusters(cluster_names=["sciagent-orphan"], router=router)

    log = MagicMock()
    t.finalize_storage(provenance_log=log)

    cluster_rows = [
        c for c in log.emit_compute_cost_observed.call_args_list
        if c.kwargs.get("cluster_name") == "sciagent-orphan"
    ]
    assert len(cluster_rows) == 1
    assert cluster_rows[0].kwargs["cost_source"] == "session_end_fallback"
    assert cluster_rows[0].kwargs["cost_usd"] == 0.0


# ---------------------------------------------------------------------------
# Orchestrator integration — _check_budgets + cleanup-on-budget-exceeded
# ---------------------------------------------------------------------------


def _orchestrator_with_cap(max_cost_usd: float) -> TaskOrchestrator:
    cfg = OrchestratorConfig(max_cost_usd=max_cost_usd, verbose=False)
    return TaskOrchestrator(TodoTool(), config=cfg)


def test_check_budgets_raises_when_total_over_cap():
    """_check_budgets reads cost_tracker.total_usd, not the H1 _cost_so_far stub."""
    orch = _orchestrator_with_cap(max_cost_usd=0.50)
    orch._start_time = 0.0  # must be set before _check_budgets reads
    orch._cost_tracker.record_llm_call(0.60)

    with pytest.raises(BudgetExceeded) as exc:
        orch._check_budgets()
    msg = str(exc.value)
    assert "max_cost_usd" in msg and "0.60" in msg


def test_check_budgets_sums_across_axes():
    """The cap fires on the *total*, not on any single axis."""
    orch = _orchestrator_with_cap(max_cost_usd=0.40)
    orch._start_time = 0.0
    orch._cost_tracker.record_llm_call(0.20)
    orch._cost_tracker.compute_cost_usd = 0.25  # crosses 0.40 only with llm added

    with pytest.raises(BudgetExceeded):
        orch._check_budgets()


def test_check_budgets_no_op_under_cap():
    """Under-cap call must not raise."""
    orch = _orchestrator_with_cap(max_cost_usd=10.00)
    orch._start_time = 0.0
    orch._cost_tracker.record_llm_call(0.05)
    orch._check_budgets()  # no exception


def test_stop_session_clusters_calls_cluster_stop_per_manifest_entry():
    """When the cap fires, every cluster in the session manifest gets sky.stop()
    BEFORE BudgetExceeded propagates."""
    orch = _orchestrator_with_cap(max_cost_usd=0.50)
    orch._cost_tracker.session_id = "sess-budget-test"

    fake_records = [
        {"cluster_name": "sciagent-a", "session_id": "sess-budget-test"},
        {"cluster_name": "sciagent-b", "session_id": "sess-budget-test"},
    ]
    fake_router = MagicMock()
    fake_router.cluster_stop.return_value = True

    with patch(
        "sciagent.compute.cluster_manifest.list_clusters",
        return_value=fake_records,
    ), patch("sciagent.compute.router.ComputeRouter", return_value=fake_router):
        orch._stop_session_clusters("budget exceeded")

    stopped = [c.args[0] for c in fake_router.cluster_stop.call_args_list]
    assert set(stopped) == {"sciagent-a", "sciagent-b"}


def test_stop_session_clusters_swallows_per_cluster_failure():
    """A cluster_stop failure on one cluster must not block stopping the
    others — and must not raise out of _stop_session_clusters."""
    orch = _orchestrator_with_cap(max_cost_usd=0.50)
    orch._cost_tracker.session_id = "sess-budget-test"

    fake_records = [
        {"cluster_name": "sciagent-a", "session_id": "sess-budget-test"},
        {"cluster_name": "sciagent-b", "session_id": "sess-budget-test"},
    ]
    fake_router = MagicMock()
    fake_router.cluster_stop.side_effect = [
        RuntimeError("auth"), True,
    ]

    with patch(
        "sciagent.compute.cluster_manifest.list_clusters",
        return_value=fake_records,
    ), patch("sciagent.compute.router.ComputeRouter", return_value=fake_router):
        orch._stop_session_clusters("budget exceeded")  # no raise

    assert fake_router.cluster_stop.call_count == 2


def test_stop_session_clusters_no_op_without_session_id():
    """No session_id → nothing to look up → no-op."""
    orch = _orchestrator_with_cap(max_cost_usd=0.50)
    orch._cost_tracker.session_id = None

    fake_router = MagicMock()
    with patch("sciagent.compute.router.ComputeRouter", return_value=fake_router):
        orch._stop_session_clusters("budget exceeded")

    fake_router.cluster_stop.assert_not_called()


# ---------------------------------------------------------------------------
# Active-tracker registry — used by llm.py's _capture_last_usage hook
# ---------------------------------------------------------------------------


def test_active_tracker_registry_round_trip():
    set_active_cost_tracker(None)
    assert get_active_cost_tracker() is None
    t = RunCostTracker()
    set_active_cost_tracker(t)
    assert get_active_cost_tracker() is t
    set_active_cost_tracker(None)
    assert get_active_cost_tracker() is None


def test_llm_hook_feeds_active_tracker():
    """_capture_last_usage (the H3 path) must record cost into the active
    tracker without a constructor wire-through. Provider-agnostic — uses
    a hand-built response shape that mirrors what litellm returns."""
    from sciagent.llm import LLMClient

    client = LLMClient(model="anthropic/claude-haiku-4-5-20251001", api_key="test")

    # Mimic the litellm response shape — usage on .usage, cost on
    # ._hidden_params["response_cost"].
    fake_usage = MagicMock(prompt_tokens=100, completion_tokens=20)
    fake_response = MagicMock()
    fake_response.usage = fake_usage
    fake_response._hidden_params = {"response_cost": 0.0123}

    tracker = RunCostTracker()
    set_active_cost_tracker(tracker)
    try:
        client._capture_last_usage(fake_response, {"model": "anthropic/claude-haiku-4-5-20251001"})
    finally:
        set_active_cost_tracker(None)

    assert tracker.llm_cost_usd == pytest.approx(0.0123)
    assert client._last_usage["cost_usd"] == pytest.approx(0.0123)


def test_llm_hook_noop_without_active_tracker():
    """No registered tracker → cost goes to _last_usage only, no error."""
    from sciagent.llm import LLMClient

    set_active_cost_tracker(None)
    client = LLMClient(model="anthropic/claude-haiku-4-5-20251001", api_key="test")

    fake_response = MagicMock()
    fake_response.usage = MagicMock(prompt_tokens=1, completion_tokens=1)
    fake_response._hidden_params = {"response_cost": 0.001}

    client._capture_last_usage(fake_response, {"model": "anthropic/claude-haiku-4-5-20251001"})
    assert client._last_usage["cost_usd"] == pytest.approx(0.001)


# ---------------------------------------------------------------------------
# GPU smoke tests — catch catalog drift / SkyPilot version regressions.
# Offline: when SkyPilot isn't enabled / the optimizer fails, estimate_cost
# falls back to a static GPU table (skypilot.py:2462-2481) which lives in
# the same price band the smoke test enforces.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "gpu_type,lower,upper",
    [
        ("T4",   0.20, 10.0),
        ("A10G", 0.20, 10.0),
        ("A100", 0.20, 10.0),
    ],
)
def test_estimate_cost_gpu_band(gpu_type, lower, upper):
    """ComputeRouter.estimate_cost on a single-GPU job returns a plausible
    hourly rate. Bounded loosely to catch catalog drift / regressions —
    SkyPilot's published rates for these GPUs all sit comfortably in
    [0.20, 10.00] per the design §8.5 band."""
    job = Job(
        image="python:3.11",
        command="nvidia-smi",
        requirements=ComputeRequirements(cpus=4, memory_gb=16, gpus=1, gpu_type=gpu_type),
    )

    router = ComputeRouter()
    estimate = router.estimate_cost(job, duration_hours=1.0)

    # Local-only environments return {"estimated_hourly": 0, "note": ...}.
    # The smoke test only enforces the band when SkyPilot is active or the
    # fallback static table runs — both produce non-zero on a GPU job.
    if "estimated_hourly" not in estimate or estimate.get("estimated_hourly", 0) == 0:
        pytest.skip("SkyPilot backend not active in this environment")

    hourly = float(estimate["estimated_hourly"])
    assert lower <= hourly <= upper, (
        f"{gpu_type} hourly estimate ${hourly:.2f} outside expected band "
        f"[${lower:.2f}, ${upper:.2f}] — catalog drift or SkyPilot version regression?"
    )


# ---------------------------------------------------------------------------
# SkyPilot backend cost_report wrapper — best-effort, never raises
# ---------------------------------------------------------------------------


def test_backend_cost_report_filters_by_cluster_name():
    """The backend wraps sky.cost_report() and filters rows to the
    requested cluster set."""
    mock_sky = MagicMock()
    mock_sky.cost_report.return_value = [
        {"name": "sciagent-a", "total_cost": 0.50, "duration": 120},
        {"name": "sciagent-b", "total_cost": 0.30, "duration": 90},
        {"name": "other",      "total_cost": 1.00, "duration": 600},
    ]
    backend = SkyPilotBackend.__new__(SkyPilotBackend)
    backend._sky = mock_sky

    rows = backend.cost_report(cluster_names=["sciagent-a", "sciagent-b"])
    names = sorted(r["name"] for r in rows)
    assert names == ["sciagent-a", "sciagent-b"]


def test_backend_cost_report_swallows_sky_exception():
    """A sky-side failure returns [] — never propagates."""
    mock_sky = MagicMock()
    mock_sky.cost_report.side_effect = RuntimeError("sky transient")
    backend = SkyPilotBackend.__new__(SkyPilotBackend)
    backend._sky = mock_sky

    assert backend.cost_report() == []
