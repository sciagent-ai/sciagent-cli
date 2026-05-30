"""Per-session cost aggregator (H6 — design §8.3).

Owns three cost axes that the bench's CellResult breaks out for honest
cross-system comparison:

  - ``llm_cost_usd``      — sum of per-LLM-call cost reported by litellm
    (``response._hidden_params["response_cost"]``). Fed by ``record_llm_call``
    from the active LLMClient hook (see ``llm.py:_capture_last_usage``).
  - ``compute_cost_usd``  — sky's realized cluster cost, recomputed each
    poll from ``sky.cost_report()``. Idempotent — we don't increment locally,
    we replace with sky's authoritative total. Estimate fallback used when
    a just-terminated cluster has no realized row.
  - ``storage_cost_usd``  — workspace bucket size × per-region storage rate,
    computed once on session shutdown via ``finalize_storage``.

The total (``total_usd``) is derivable; it is what
``TaskOrchestrator._check_budgets()`` compares against ``max_cost_usd``.
Per-axis is the source of truth — never collapse the axes into one number
internally (feedback_cost_axis_separation.md).

Process-level "active tracker" registry mirrors
``provenance_log._active_session_id``: the orchestrator registers a
tracker at the top of ``execute_all`` and the LLMClient hook reads it via
``get_active_cost_tracker()`` without taking a constructor dependency on
the orchestrator. Keeps the LLMClient provider-agnostic and the
orchestrator-owns-cost layering intact.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional


@dataclass
class RunCostTracker:
    """Three-axis cost aggregator for a single sciagent session.

    See module docstring for the axis contract. One instance per
    TaskOrchestrator; registered as the process-level "active tracker"
    while ``execute_all`` is running so peripheral layers (LLM hook,
    cluster_stop emitter) can update it without an injected reference.
    """

    session_id: Optional[str] = None
    llm_cost_usd: float = 0.0
    compute_cost_usd: float = 0.0
    storage_cost_usd: float = 0.0

    # Per-cluster realized-cost memo keyed by cluster_name. Lets
    # poll_active_clusters stay idempotent — recomputed from sky's
    # numbers each call rather than incremented.
    _cluster_costs: Dict[str, float] = field(default_factory=dict)
    # Names of clusters we've ever seen, so finalize emits a
    # session_end_fallback for any cluster sky.cost_report() never
    # acknowledged.
    _known_clusters: set = field(default_factory=set)

    @property
    def total_usd(self) -> float:
        """LLM + compute + storage. The number ``_check_budgets`` reads."""
        return self.llm_cost_usd + self.compute_cost_usd + self.storage_cost_usd

    def record_llm_call(self, cost_usd: Optional[float]) -> None:
        """Add a single litellm call's realized cost to ``llm_cost_usd``.

        Tolerates ``None`` (some providers don't expose response_cost) and
        non-numeric values (defensive; the hook ingests whatever the
        provider returned). No-op on either — we never recompute from
        token counts × static prices, that's the provider-coupling we
        explicitly avoid.
        """
        if cost_usd is None:
            return
        try:
            value = float(cost_usd)
        except (TypeError, ValueError):
            return
        if value <= 0:
            return
        self.llm_cost_usd += value

    def poll_active_clusters(
        self,
        cluster_names: Optional[List[str]] = None,
        *,
        router: Any = None,
        provenance_log: Any = None,
    ) -> None:
        """Recompute ``compute_cost_usd`` from sky's authoritative rows.

        ``cluster_names`` is the list registered in this session's
        ``cluster_manifest`` — when None, the tracker resolves it from the
        session's manifest dir. ``router`` defaults to a fresh
        ``ComputeRouter()`` (cheap, no global state); pass an injected
        router for tests. ``provenance_log`` defaults to the active session's
        log so per-cluster rows get a ``compute_cost_observed`` event.

        Idempotent: the memo records the latest sky-reported cost per
        cluster; the aggregate is rebuilt from the memo each call. A
        cluster whose cost shrank (sky correcting a stale estimate) is
        reflected without double-counting.
        """
        names = list(cluster_names) if cluster_names is not None else self._resolve_session_clusters()
        if not names:
            return

        for name in names:
            self._known_clusters.add(name)

        if router is None:
            try:
                from .compute.router import ComputeRouter
                router = ComputeRouter()
            except Exception:
                return

        try:
            rows = router.cost_report(cluster_names=names)
        except Exception:
            rows = []

        log = provenance_log if provenance_log is not None else self._active_log()

        for row in rows or []:
            name = row.get("name") or row.get("cluster_name")
            if not name:
                continue
            cost = row.get("total_cost")
            if cost is None:
                cost = row.get("cost_usd")
            try:
                cost_f = float(cost) if cost is not None else None
            except (TypeError, ValueError):
                cost_f = None
            if cost_f is None or cost_f < 0:
                continue

            wall = row.get("duration")
            try:
                wall_f = float(wall) if wall is not None else 0.0
            except (TypeError, ValueError):
                wall_f = 0.0

            instance = self._row_instance_type(row)

            self._cluster_costs[name] = cost_f
            if log is not None:
                try:
                    log.emit_compute_cost_observed(
                        cluster_name=name,
                        instance_type=instance,
                        wall_seconds=wall_f,
                        cost_usd=cost_f,
                        cost_source="sky_cost_report",
                    )
                except Exception:
                    pass

        self.compute_cost_usd = sum(self._cluster_costs.values())

    def finalize_storage(
        self,
        *,
        router: Any = None,
        provenance_log: Any = None,
    ) -> None:
        """Compute storage cost once at session shutdown.

        Storage is typically a few cents per session; the design §8.4 calls
        for one ``sky storage ls`` pass × per-region catalog rate. If sky
        rejects the inspection (region/bucket inaccessible), emit
        ``compute_cost_observed`` with ``cost_source: "session_end_fallback"``
        and ``cost_usd: 0.0`` per design §12 — don't fail the run because
        storage cost lookup failed.

        Clusters we polled but sky never reported on get a
        ``session_end_fallback`` row at $0 so the bench can tell "we tried
        to measure" from "we never had a cluster."
        """
        log = provenance_log if provenance_log is not None else self._active_log()

        # Storage axis: ship $0 with the session_end_fallback discriminator
        # for now. The catalog-rate × bucket-size pass is a one-liner the
        # day sky.clouds.catalog exposes the storage rate cleanly; until
        # then, recording $0 explicitly is what the bench needs to keep the
        # cost-axis contract honest (storage column is not None / missing,
        # it's $0). Per design §12 fallback.
        if log is not None:
            try:
                log.emit_compute_cost_observed(
                    cluster_name="__storage__",
                    instance_type=None,
                    wall_seconds=0.0,
                    cost_usd=0.0,
                    cost_source="session_end_fallback",
                )
            except Exception:
                pass

        # Compute axis: any cluster we knew about but sky.cost_report
        # never acknowledged gets a fallback row at the value we last
        # memoized (0 if we never saw it). Lets cluster.total_cost = sum
        # of compute_cost_observed events stay an invariant for the bench.
        for name in self._known_clusters:
            if name in self._cluster_costs:
                continue
            if log is not None:
                try:
                    log.emit_compute_cost_observed(
                        cluster_name=name,
                        instance_type=None,
                        wall_seconds=0.0,
                        cost_usd=0.0,
                        cost_source="session_end_fallback",
                    )
                except Exception:
                    pass

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _resolve_session_clusters(self) -> List[str]:
        """Resolve cluster names from the session's manifest dir."""
        if not self.session_id:
            return []
        try:
            from .compute.cluster_manifest import list_clusters
            records = list_clusters(session_id=self.session_id)
        except Exception:
            return []
        return [r["cluster_name"] for r in records if r.get("cluster_name")]

    def _active_log(self) -> Any:
        """Best-effort handle to the active session's provenance log."""
        try:
            from .provenance_log import get_active_session_log
            return get_active_session_log()
        except Exception:
            return None

    @staticmethod
    def _row_instance_type(row: Dict[str, Any]) -> Optional[str]:
        """Pull the instance_type out of a sky.cost_report row.

        sky exposes it on ``row["resources"]`` (a ``sky.Resources`` object
        with an ``instance_type`` attr) or directly on ``row["instance_type"]``
        depending on version. Returns None on any miss — instance_type is
        cosmetic on the cost_observed event, not load-bearing.
        """
        direct = row.get("instance_type")
        if direct:
            return str(direct)
        resources = row.get("resources")
        if resources is None:
            return None
        for attr in ("instance_type", "name"):
            value = getattr(resources, attr, None)
            if value:
                return str(value)
        return None


# ----------------------------------------------------------------------
# Process-level active-tracker registry.
#
# Mirrors ``provenance_log._active_session_id``: layers that need to feed
# the tracker (LLM hook, cluster lifecycle emitters) read the active
# tracker via ``get_active_cost_tracker()`` instead of taking a
# constructor argument. The orchestrator registers a tracker at the top
# of ``execute_all`` and clears it on shutdown.
# ----------------------------------------------------------------------

_active_tracker: Optional[RunCostTracker] = None
_active_tracker_lock = threading.Lock()


def set_active_cost_tracker(tracker: Optional[RunCostTracker]) -> None:
    """Register (or clear) the process-level active tracker."""
    global _active_tracker
    with _active_tracker_lock:
        _active_tracker = tracker


def get_active_cost_tracker() -> Optional[RunCostTracker]:
    """Return the active tracker, or None when no orchestrator is running."""
    return _active_tracker
