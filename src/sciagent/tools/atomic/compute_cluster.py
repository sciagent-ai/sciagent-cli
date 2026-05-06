"""compute_cluster — lifecycle surface for persistent SkyPilot clusters.

One tool, action-dispatched onto Sky's cluster-mode primitives:

  - ``action="status"``       → ``sky.status(cluster_names=[...])`` enriched
                                with sciagent's local cluster manifest.
  - ``action="stop"``         → ``sky.stop(cluster_name)``. Non-destructive:
                                preserves disk + identity for fast restart.
                                The default end-of-task action.
  - ``action="start"``        → ``sky.start(cluster_name)``. Restart a
                                previously stopped cluster, reusing its disk.
  - ``action="down"``         → ``sky.down(cluster_name, graceful=...)``.
                                Destructive — for explicit cleanup only.
  - ``action="autostop"``     → ``sky.autostop(cluster_name, idle_minutes,
                                wait_for, hook)``. Updates the autostop
                                config on a running cluster.
  - ``action="refresh_mounts"`` → ``sky launch --no-setup -c <name>``.
                                Re-syncs ``file_mounts`` and runs the new
                                command without re-running ``setup``.
                                Sky's canonical "iterate on data while
                                reusing a cluster" pattern.

Why a single tool with action-dispatch instead of separate tools: the
agent's tool-count budget matters more than the per-action surface, and
these are conceptually one surface (cluster lifecycle). Keeps the
toolset compact.

Stop vs. down — choosing the right primitive: ``stop`` preserves the
cluster's attached disk and the cluster name; you can ``start`` it again
in seconds with the same identity, which is the right default when
follow-up work might want to re-use the warm container. ``down``
destroys the cluster (the persistent storage mount survives because it's
S3-backed, but on-cluster scratch is gone). Use ``down`` only when the
user explicitly asks for cleanup or a quota policy demands it.
"""

from __future__ import annotations

from typing import Any, Dict, Optional

from ..registry import BaseTool, ToolResult


class ComputeClusterTool(BaseTool):
    name = "compute_cluster"
    description = (
        "Manage persistent SkyPilot cluster lifecycle. action='status' "
        "returns Sky's cluster status (UP/STOPPED/INIT/AUTOSTOPPING/PENDING) "
        "plus sciagent's local manifest. action='wait_until_up' blocks "
        "inside ONE LLM turn (default timeout=300s) until the cluster "
        "reaches UP — use this after launch_cluster instead of polling "
        "status across multiple LLM turns. action='wait_for_job' blocks "
        "until a per-cluster job (compute_exec returned cluster_job_id) "
        "reaches terminal state — cluster-mode equivalent of bg_wait. "
        "action='logs' returns the tail of a cluster-mode job's stdout, "
        "with on-disk cache fallback for post-autostop forensics. "
        "action='stop' preserves the cluster (non-destructive) so it can "
        "be restarted; this is the DEFAULT end-of-task action. "
        "action='start' restarts a stopped cluster reusing its disk. "
        "action='down' DESTROYS the cluster — use only for explicit "
        "cleanup, never as a default end-of-task. "
        "action='autostop' updates idle threshold / wait_for / hook. "
        "action='refresh_mounts' re-syncs file_mounts via sky launch "
        "--no-setup — Sky's canonical way to point a warm cluster at new "
        "input data without re-running setup. Resources (instance type, "
        "GPUs, num_nodes, disk_size) are immutable per cluster."
    )

    parameters = {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": [
                    "status", "stop", "start", "down", "autostop",
                    "refresh_mounts", "wait_until_up", "wait_for_job",
                    "logs",
                ],
                "description": "Lifecycle action to perform on the cluster.",
            },
            "cluster_name": {
                "type": "string",
                "description": "Cluster identifier.",
            },
            "graceful": {
                "type": "boolean",
                "description": (
                    "action='down' only. Wait for in-flight jobs to finish "
                    "before teardown (default true)."
                ),
            },
            "idle_minutes": {
                "type": "integer",
                "description": (
                    "action='autostop' only. New idle threshold in minutes."
                ),
            },
            "wait_for": {
                "type": "string",
                "enum": ["jobs", "jobs_and_ssh", "none"],
                "description": (
                    "action='autostop' only. Idle definition. 'jobs' "
                    "(default for sciagent — agent never SSHes), "
                    "'jobs_and_ssh' (Sky's default for human dev), 'none' "
                    "(hard timeout)."
                ),
            },
            "hook": {
                "type": "string",
                "description": (
                    "action='autostop' only. Optional shell snippet to run "
                    "before autostop fires (e.g., flush /scratch to S3)."
                ),
            },
            "command": {
                "type": "string",
                "description": (
                    "action='refresh_mounts' only. Command to run after "
                    "the mount re-sync."
                ),
            },
            "service": {
                "type": "string",
                "description": (
                    "action='refresh_mounts' only. Service name to resolve "
                    "the image for the inline task. Should match the "
                    "cluster's existing service."
                ),
            },
            "image": {
                "type": "string",
                "description": (
                    "action='refresh_mounts' only. Direct Docker image. "
                    "Specify either service OR image, not both."
                ),
            },
            "workspace_source": {
                "description": (
                    "action='refresh_mounts' only. New input mount(s). "
                    "Single string or list of {path, source} dicts, same "
                    "shape as compute_run."
                ),
            },
            "timeout": {
                "type": "number",
                "description": (
                    "action='wait_until_up' / 'wait_for_job' only. Max "
                    "seconds to block inside this single tool call. "
                    "wait_until_up default 300; wait_for_job default 1800. "
                    "Stay under the LLM client timeout (~600s on first "
                    "turn) — for longer waits, call wait_* again."
                ),
            },
            "cluster_job_id": {
                "type": "integer",
                "description": (
                    "action='wait_for_job' / 'logs' only. The per-cluster "
                    "int job_id returned by compute_run(mode='cluster') / "
                    "compute_exec / refresh_mounts."
                ),
            },
            "tail_lines": {
                "type": "integer",
                "description": (
                    "action='logs' only. Number of trailing log lines to "
                    "return. Default 200."
                ),
            },
        },
        "required": ["action", "cluster_name"],
    }

    _CLUSTER_ALIASES = ("cluster_name", "cluster", "name")

    def __init__(self, working_dir: str = "."):
        self._working_dir = working_dir
        self._router = None

    def _get_router(self):
        if self._router is None:
            from sciagent.compute.router import ComputeRouter
            self._router = ComputeRouter()
        return self._router

    def execute(
        self,
        action: str = "",
        cluster_name: str = "",
        graceful: bool = True,
        idle_minutes: Optional[int] = None,
        wait_for: str = "jobs",
        hook: Optional[str] = None,
        command: Optional[str] = None,
        service: Optional[str] = None,
        image: Optional[str] = None,
        workspace_source: Optional[Any] = None,
        timeout: Optional[float] = None,
        cluster_job_id: Optional[int] = None,
        tail_lines: Optional[int] = None,
        **kwargs,
    ) -> ToolResult:
        if not cluster_name:
            for alias in self._CLUSTER_ALIASES[1:]:
                value = kwargs.get(alias)
                if isinstance(value, str) and value.strip():
                    cluster_name = value
                    break

        valid_actions = (
            "status", "wait_until_up", "wait_for_job", "logs",
            "stop", "start", "down", "autostop", "refresh_mounts",
        )
        if not isinstance(action, str) or not action:
            return ToolResult(
                success=False,
                output=None,
                error=(
                    f"action must be a non-empty string. Valid: "
                    f"{', '.join(valid_actions)}. Got: {action!r}."
                ),
            )
        if not isinstance(cluster_name, str) or not cluster_name:
            return ToolResult(
                success=False,
                output=None,
                error=(
                    f"cluster_name must be a non-empty string identifying "
                    f"the cluster (e.g. 'datacenter-cfd'). Got: "
                    f"{cluster_name!r}."
                ),
            )
        if action not in valid_actions:
            return ToolResult(
                success=False,
                output=None,
                error=(
                    f"Unknown action '{action}'. Valid: "
                    f"{', '.join(valid_actions)}."
                ),
            )

        try:
            router = self._get_router()
        except Exception as exc:
            return ToolResult(
                success=False,
                output=None,
                error=f"Compute router unavailable: {exc}",
            )

        if action == "status":
            return self._do_status(router, cluster_name)
        if action == "stop":
            return self._do_stop(router, cluster_name)
        if action == "start":
            return self._do_start(router, cluster_name)
        if action == "down":
            return self._do_down(router, cluster_name, graceful=graceful)
        if action == "autostop":
            return self._do_autostop(
                router,
                cluster_name,
                idle_minutes=idle_minutes,
                wait_for=wait_for,
                hook=hook,
            )
        if action == "refresh_mounts":
            return self._do_refresh_mounts(
                router,
                cluster_name,
                command=command,
                service=service,
                image=image,
                workspace_source=workspace_source,
            )
        if action == "wait_until_up":
            return self._do_wait_until_up(
                router, cluster_name, timeout=timeout
            )
        if action == "wait_for_job":
            return self._do_wait_for_job(
                router,
                cluster_name,
                cluster_job_id=cluster_job_id,
                timeout=timeout,
            )
        if action == "logs":
            return self._do_logs(
                router,
                cluster_name,
                cluster_job_id=cluster_job_id,
                tail_lines=tail_lines,
            )

        # Unreachable: action is validated against valid_actions above.
        return ToolResult(
            success=False,
            output=None,
            error=f"Action '{action}' is not implemented.",
        )

    def _do_status(self, router, cluster_name: str) -> ToolResult:
        try:
            info = router.cluster_status(cluster_name)
        except RuntimeError as exc:
            return ToolResult(success=False, output=None, error=str(exc))
        # Surface the durable session workspace URI alongside the cluster
        # status so the agent's next step (compute_exec, materialize, or
        # produces_uris) doesn't have to recompute the bucket name. The URI
        # is derivable from session_id; we read it from the cluster manifest
        # written at launch.
        manifest = (info or {}).get("manifest") or {}
        session_id = manifest.get("session_id")
        if session_id:
            try:
                from sciagent.compute.backends.skypilot import (
                    SkyPilotBackend,
                    _build_workspace_uri as _bld_ws_uri,
                )
                store = SkyPilotBackend().resolve_workspace_store()
                info["workspace_uri"] = _bld_ws_uri(store, session_id)
            except Exception:
                info["workspace_uri"] = None
        else:
            info["workspace_uri"] = None
        return ToolResult(success=True, output=info)

    def _do_down(self, router, cluster_name: str, graceful: bool) -> ToolResult:
        try:
            ok = router.cluster_down(cluster_name, graceful=graceful)
        except RuntimeError as exc:
            return ToolResult(success=False, output=None, error=str(exc))
        return ToolResult(
            success=ok,
            output={"cluster_name": cluster_name, "down": ok, "graceful": graceful},
            error=None if ok else f"sky.down failed for {cluster_name}",
        )

    def _do_stop(self, router, cluster_name: str) -> ToolResult:
        try:
            ok = router.cluster_stop(cluster_name)
        except RuntimeError as exc:
            return ToolResult(success=False, output=None, error=str(exc))
        return ToolResult(
            success=ok,
            output={
                "cluster_name": cluster_name,
                "stopped": ok,
                "note": (
                    "Cluster preserved; data tier (S3 mount) intact. Use "
                    "action='start' to resume."
                ),
            },
            error=None if ok else f"sky.stop failed for {cluster_name}",
        )

    def _do_start(self, router, cluster_name: str) -> ToolResult:
        try:
            ok = router.cluster_start(cluster_name)
        except RuntimeError as exc:
            return ToolResult(success=False, output=None, error=str(exc))
        return ToolResult(
            success=ok,
            output={"cluster_name": cluster_name, "started": ok},
            error=None if ok else f"sky.start failed for {cluster_name}",
        )

    def _do_autostop(
        self,
        router,
        cluster_name: str,
        idle_minutes: Optional[int],
        wait_for: str,
        hook: Optional[str],
    ) -> ToolResult:
        if idle_minutes is None:
            return ToolResult(
                success=False,
                output=None,
                error="action='autostop' requires idle_minutes.",
            )
        try:
            ok = router.set_cluster_autostop(
                cluster_name=cluster_name,
                idle_minutes=int(idle_minutes),
                wait_for=wait_for,
                hook=hook,
            )
        except RuntimeError as exc:
            return ToolResult(success=False, output=None, error=str(exc))
        return ToolResult(
            success=ok,
            output={
                "cluster_name": cluster_name,
                "idle_minutes": int(idle_minutes),
                "wait_for": wait_for,
                "hook_set": bool(hook),
            },
            error=None if ok else f"sky.autostop failed for {cluster_name}",
        )

    def _do_refresh_mounts(
        self,
        router,
        cluster_name: str,
        command: Optional[str],
        service: Optional[str],
        image: Optional[str],
        workspace_source: Any,
    ) -> ToolResult:
        if not command:
            return ToolResult(
                success=False,
                output=None,
                error=(
                    "action='refresh_mounts' requires command (the run "
                    "command after mounts re-sync). To just refresh mounts "
                    "with no work payload, pass command='true'. To run an "
                    "ad-hoc command on an already-mounted cluster without "
                    "re-syncing inputs, use compute_exec instead."
                ),
            )
        if service and image:
            return ToolResult(
                success=False,
                output=None,
                error="Specify 'service' OR 'image', not both.",
            )

        from sciagent.compute.job import (
            ComputeRequirements,
            Job,
            LaunchError,
        )
        from sciagent.tools.atomic.compute import ComputeTool

        # Build mounts via the SkyPilot backend's helpers (same path as
        # compute_run uses) so storage_mount construction is identical.
        skypilot_backend = router._backends.get("skypilot")
        if skypilot_backend is None:
            return ToolResult(
                success=False,
                output=None,
                error="SkyPilot backend not available.",
            )

        try:
            from sciagent.compute.backends.skypilot import (
                _normalize_workspace_source as _normalize_ws,
            )
            normalized_inputs = _normalize_ws(workspace_source)
        except ValueError as exc:
            return ToolResult(
                success=False,
                output=None,
                error=f"Invalid workspace_source: {exc}",
            )

        session_id = ComputeTool._shared_session_id
        storage_list = []
        outputs_mount = skypilot_backend.build_outputs_mount(session_id) if session_id else None
        if outputs_mount is not None:
            storage_list.append(outputs_mount)
        if normalized_inputs and session_id:
            storage_list.extend(
                skypilot_backend.build_input_mounts(
                    normalized_inputs, session_id=session_id
                )
            )

        resolved_image = (
            f"ghcr.io/sciagent-ai/{service}:latest" if service else (image or "")
        )

        requirements = ComputeRequirements(
            cpus=1, memory_gb=1, gpus=0, gpu_type=None,
        )
        if storage_list:
            requirements.storage = storage_list

        job = Job(
            service=service or "custom",
            image=resolved_image,
            command=command,
            working_dir=self._working_dir,
            requirements=requirements,
            session_id=session_id,
        )

        try:
            cluster, int_job_id = router.refresh_cluster_mounts(
                job=job, cluster_name=cluster_name
            )
        except LaunchError as launch_exc:
            return ToolResult(
                success=False,
                output={
                    "cluster_name": cluster_name,
                    "failure_type": "refresh_mounts_rejected",
                },
                error=f"sky.launch (--no-setup) rejected: {launch_exc}",
            )

        return ToolResult(
            success=True,
            output={
                "cluster_name": cluster,
                "cluster_job_id": int_job_id,
                "action": "refresh_mounts",
                "message": (
                    f"Mounts refreshed on {cluster}; job_id {int_job_id}. "
                    f"Setup was NOT re-run (--no-setup). Tail logs via "
                    f"`sky logs {cluster} {int_job_id}`."
                ),
            },
        )

    def _do_wait_until_up(
        self,
        router,
        cluster_name: str,
        timeout: Optional[float],
    ) -> ToolResult:
        """Block until the cluster reaches UP, terminal-bad, or timeout.

        Folds the agent's status-polling loop into one tool call. Each
        LLM-turn poll costs ~5–30s of thinking + tokens; for a 5-min
        provision that's 10+ wasted turns. This collapses to one.
        """
        try:
            info = router.wait_cluster_up(
                cluster_name=cluster_name,
                timeout=timeout if timeout is not None else 300.0,
            )
        except RuntimeError as exc:
            return ToolResult(success=False, output=None, error=str(exc))
        # Surface as success regardless of ready/timeout so the agent
        # branches on info["ready"] (a real boolean field) instead of
        # having to parse the error path.
        return ToolResult(success=True, output=info)

    def _do_wait_for_job(
        self,
        router,
        cluster_name: str,
        cluster_job_id: Optional[int],
        timeout: Optional[float],
    ) -> ToolResult:
        """Block until a per-cluster job reaches a terminal state. The
        cluster-mode equivalent of bg_wait for managed jobs."""
        if cluster_job_id is None:
            return ToolResult(
                success=False,
                output=None,
                error=(
                    "action='wait_for_job' requires cluster_job_id (the int "
                    "returned by compute_run(mode='cluster') / compute_exec)."
                ),
            )
        try:
            info = router.wait_cluster_job(
                cluster_name=cluster_name,
                cluster_job_id=int(cluster_job_id),
                timeout=timeout if timeout is not None else 1800.0,
            )
        except RuntimeError as exc:
            return ToolResult(success=False, output=None, error=str(exc))
        return ToolResult(success=True, output=info)

    def _do_logs(
        self,
        router,
        cluster_name: str,
        cluster_job_id: Optional[int],
        tail_lines: Optional[int],
    ) -> ToolResult:
        """Tail of a cluster-mode job's stdout, with cache fallback.

        Live path uses ``sky.tail_logs``; falls back to the cluster
        manifest's on-disk cache when the cluster has transitioned out of
        UP (autostop, manual down). The cache is populated by
        ``wait_cluster_job`` at terminal status, and refreshed on every
        successful live fetch — so a `logs` call BEFORE autostop is
        what unlocks post-autostop forensics.
        """
        if cluster_job_id is None:
            return ToolResult(
                success=False,
                output=None,
                error=(
                    "action='logs' requires cluster_job_id (the int "
                    "returned by compute_run(mode='cluster') / compute_exec)."
                ),
            )
        try:
            info = router.tail_cluster_job_logs(
                cluster_name=cluster_name,
                cluster_job_id=int(cluster_job_id),
                tail_lines=int(tail_lines) if tail_lines else 200,
            )
        except RuntimeError as exc:
            return ToolResult(success=False, output=None, error=str(exc))
        # source="missing" is a soft failure: the cluster is gone and no
        # cache exists. Surface as success=False so the agent doesn't
        # treat an empty log_tail as "the job emitted nothing".
        if info.get("source") == "missing":
            return ToolResult(
                success=False,
                output=info,
                error=(
                    f"No logs available for {cluster_name} job "
                    f"{cluster_job_id}: cluster is not UP and no cached "
                    f"log exists. {info.get('hint', '')}"
                ),
            )
        return ToolResult(success=True, output=info)
