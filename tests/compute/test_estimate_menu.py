"""Tests for the multi-row estimate menu surfaced via compute_run(estimate_only=True).

Covers the slim-P0 contract:
  - Menu returns >=3 rows (we walk 3 node counts x 2 pricing models = 6).
  - Available rows are sorted by estimated_total_usd ascending.
  - over_budget=True when budget_usd is passed and total_usd exceeds it.
  - Failed rows are kept as available=False with a reason — the menu still
    surfaces working rows below the failure.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from sciagent.tools.atomic.compute import ComputeTool
from sciagent.compute.job import Job, ComputeRequirements
from sciagent.compute.backends.skypilot import SkyPilotBackend


def _fake_optimize_one(spec_by_label):
    """Build a stub for SkyPilotBackend._optimize_one that returns the row
    keyed by (num_nodes, use_spot) the test sets up. Lets the test control
    every menu row's hourly + availability without standing up a real Sky
    optimizer."""

    def _stub(self, job):
        n = job.requirements.num_nodes
        spot = job.requirements.use_spot
        return spec_by_label[(n, spot)]

    return _stub


def test_menu_returns_at_least_three_rows_sorted_by_total_cost():
    """The menu walks {1,2,4} x {spot, on-demand}; sorted by total_usd asc."""
    spec = {
        # (num_nodes, use_spot) -> row dict the optimizer would produce
        (1, False): {"available": True, "instance": "m6i.large", "cloud": "aws", "region": "us-east-1", "spot": False, "estimated_hourly_usd": 0.10},
        (1, True):  {"available": True, "instance": "m6i.large", "cloud": "aws", "region": "us-east-1", "spot": True,  "estimated_hourly_usd": 0.03},
        (2, False): {"available": True, "instance": "m6i.large", "cloud": "aws", "region": "us-east-1", "spot": False, "estimated_hourly_usd": 0.20},
        (2, True):  {"available": True, "instance": "m6i.large", "cloud": "aws", "region": "us-east-1", "spot": True,  "estimated_hourly_usd": 0.06},
        (4, False): {"available": True, "instance": "m6i.large", "cloud": "aws", "region": "us-east-1", "spot": False, "estimated_hourly_usd": 0.40},
        (4, True):  {"available": True, "instance": "m6i.large", "cloud": "aws", "region": "us-east-1", "spot": True,  "estimated_hourly_usd": 0.12},
    }
    backend = SkyPilotBackend.__new__(SkyPilotBackend)
    job = Job(image="python:3.11", command="echo", requirements=ComputeRequirements(cpus=2, memory_gb=4))

    with patch.object(SkyPilotBackend, "_optimize_one", _fake_optimize_one(spec)):
        menu = backend.estimate_menu(job, duration_hours=1.0)

    assert len(menu) >= 3, f"menu should expose multiple scale points, got {len(menu)}"
    # All available rows come first, sorted by estimated_total_usd.
    available = [r for r in menu if r.get("available")]
    totals = [r["estimated_total_usd"] for r in available]
    assert totals == sorted(totals), f"available rows must be cost-sorted; got {totals}"
    # Cheapest is the 1-node spot row at $0.03/hr * 1h.
    assert available[0]["num_nodes"] == 1 and available[0]["spot"] is True


def test_menu_over_budget_flag_when_budget_passed():
    """budget_usd flags rows whose estimated_total_usd exceeds it; rows are
    still returned so the LLM sees the tradeoff shape."""
    spec = {
        (1, False): {"available": True, "instance": "m6i.large", "cloud": "aws", "region": "us-east-1", "spot": False, "estimated_hourly_usd": 0.10},
        (1, True):  {"available": True, "instance": "m6i.large", "cloud": "aws", "region": "us-east-1", "spot": True,  "estimated_hourly_usd": 0.03},
        (2, False): {"available": True, "instance": "m6i.large", "cloud": "aws", "region": "us-east-1", "spot": False, "estimated_hourly_usd": 5.00},  # past budget
        (2, True):  {"available": True, "instance": "m6i.large", "cloud": "aws", "region": "us-east-1", "spot": True,  "estimated_hourly_usd": 1.50},
        (4, False): {"available": True, "instance": "m6i.large", "cloud": "aws", "region": "us-east-1", "spot": False, "estimated_hourly_usd": 9.99},  # past budget
        (4, True):  {"available": True, "instance": "m6i.large", "cloud": "aws", "region": "us-east-1", "spot": True,  "estimated_hourly_usd": 3.00},
    }
    backend = SkyPilotBackend.__new__(SkyPilotBackend)
    job = Job(image="python:3.11", command="echo", requirements=ComputeRequirements(cpus=2, memory_gb=4))

    with patch.object(SkyPilotBackend, "_optimize_one", _fake_optimize_one(spec)):
        menu = backend.estimate_menu(job, duration_hours=1.0, budget_usd=4.0)

    # Rows with hourly * 1.0 > 4.0 must be flagged over_budget.
    by_label = {(r["num_nodes"], r.get("spot", False)): r for r in menu if r.get("available")}
    assert by_label[(2, False)]["over_budget"] is True
    assert by_label[(4, False)]["over_budget"] is True
    # Spot rows under budget must NOT be flagged.
    assert by_label[(1, True)]["over_budget"] is False
    assert by_label[(2, True)]["over_budget"] is False


def test_menu_keeps_failed_rows_as_unavailable_with_reason():
    """A scale point that fails the optimizer doesn't break the menu — the
    failure row is appended as available=False so the caller can still see
    the working rows above it."""
    spec = {
        (1, False): {"available": True, "instance": "m6i.large", "cloud": "aws", "region": "us-east-1", "spot": False, "estimated_hourly_usd": 0.10},
        (1, True):  {"available": False, "reason": "no capacity in spot pool"},
        (2, False): {"available": True, "instance": "m6i.large", "cloud": "aws", "region": "us-east-1", "spot": False, "estimated_hourly_usd": 0.20},
        (2, True):  {"available": False, "reason": "no capacity in spot pool"},
        (4, False): {"available": True, "instance": "m6i.large", "cloud": "aws", "region": "us-east-1", "spot": False, "estimated_hourly_usd": 0.40},
        (4, True):  {"available": False, "reason": "no capacity in spot pool"},
    }
    backend = SkyPilotBackend.__new__(SkyPilotBackend)
    job = Job(image="python:3.11", command="echo", requirements=ComputeRequirements(cpus=2, memory_gb=4))

    with patch.object(SkyPilotBackend, "_optimize_one", _fake_optimize_one(spec)):
        menu = backend.estimate_menu(job, duration_hours=2.0)

    available = [r for r in menu if r.get("available")]
    unavailable = [r for r in menu if not r.get("available")]
    assert len(available) == 3
    assert len(unavailable) == 3
    # Unavailable rows carry the optimizer's reason verbatim.
    assert all("reason" in r for r in unavailable)
    # Available rows still carry total_usd = hourly * duration_hours (=2h here).
    assert all(r["estimated_total_usd"] == round(r["estimated_hourly_usd"] * 2.0, 4) for r in available)


def test_compute_run_estimate_only_returns_options_field():
    """compute_run(estimate_only=True) surfaces the menu as `options`. End-to-end
    glue test: backend stub returns 6 rows, the tool's output dict must
    include them under `options`."""
    fake_router = MagicMock()
    fake_skypilot = MagicMock()
    fake_skypilot.name = "skypilot"
    fake_router._backends = {"skypilot": fake_skypilot}
    fake_router.list_backends.return_value = ["skypilot"]
    fake_router.select.return_value = (fake_skypilot, "test routing")
    fake_router.estimate_cost.return_value = {"estimated_hourly": 0.10, "estimated_total": 0.10}
    fake_router.estimate_menu.return_value = [
        {"label": "minimum", "available": True, "num_nodes": 1, "spot": True, "estimated_total_usd": 0.03, "estimated_hourly_usd": 0.03, "instance": "x", "cloud": "aws", "region": "r", "over_budget": False},
        {"label": "intermediate", "available": True, "num_nodes": 2, "spot": False, "estimated_total_usd": 0.20, "estimated_hourly_usd": 0.20, "instance": "y", "cloud": "aws", "region": "r", "over_budget": False},
        {"label": "scale_out", "available": True, "num_nodes": 4, "spot": False, "estimated_total_usd": 0.40, "estimated_hourly_usd": 0.40, "instance": "z", "cloud": "aws", "region": "r", "over_budget": False},
    ]

    tool = ComputeTool(working_dir=".")
    tool._router = fake_router

    out = tool.execute(
        command="echo hi",
        image="python:3.11",
        backend="skypilot",
        estimate_only=True,
        duration_hours=2.0,
        budget_usd=10.0,
    )
    assert out.success is True
    assert "options" in out.output
    assert len(out.output["options"]) == 3
    fake_router.estimate_menu.assert_called_once()
    # duration_hours and budget_usd are forwarded.
    _, kwargs = fake_router.estimate_menu.call_args
    assert kwargs["duration_hours"] == 2.0
    assert kwargs["budget_usd"] == 10.0
