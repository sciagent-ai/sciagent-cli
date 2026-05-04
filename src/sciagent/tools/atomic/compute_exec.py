"""compute_exec — run a follow-up command on an existing warm cluster.

The cluster must already be UP — typically launched by a prior
``compute_run(mode="cluster", cluster_name="...")`` call. This tool wraps
``sky.exec(task, cluster_name=...)``, which skips provisioning AND setup
and only ships the new run command (and workdir, if set). Per the
SkyPilot docs, this is the canonical way to iterate fast on a warm
cluster — typical end-to-end is ~10 seconds vs. 3–5 minutes for a fresh
provision.

What ``sky.exec`` does NOT do:
  - It does not change the cluster's ``setup`` script (run only on
    initial launch).
  - It does not change the cluster's ``storage_mounts`` (use
    ``compute_cluster(action="refresh_mounts")`` for that — wraps
    ``sky launch --no-setup`` per Sky's docs).
  - It does not change the cluster's resources (instance type, GPUs,
    num_nodes, disk_size). Resources are immutable per cluster.

What it CAN change between calls:
  - The run command and its envs.
  - The workdir (rsynced afresh per call).
  - Anything writable on the cluster's disk.

Returns the per-cluster integer job_id Sky assigned to this exec
invocation. Use it with ``sky logs <cluster_name> <int_id>`` (CLI) or
``compute_cluster(action="status", cluster_name)`` to see the queue.
"""

from __future__ import annotations

from typing import Any, Dict, Optional

from ..registry import BaseTool, ToolResult


class ComputeExecTool(BaseTool):
    name = "compute_exec"
    description = (
        "Run a follow-up command on an existing warm SkyPilot cluster via "
        "sky.exec — no provisioning, no setup, ~10s end-to-end. The cluster "
        "must already be UP (from a prior compute_run(mode='cluster', "
        "cluster_name=...) call). Use this for the iterate-on-the-same-"
        "cluster loop: probe → fix → retry. To change file_mounts on the "
        "cluster, use compute_cluster(action='refresh_mounts', ...) instead "
        "(wraps sky launch --no-setup). To inspect or tear down the cluster, "
        "use compute_cluster(action='status'|'down', ...). Resources "
        "(instance type, GPUs, num_nodes) are immutable per cluster — "
        "different resources require a fresh cluster_name."
    )

    parameters = {
        "type": "object",
        "properties": {
            "cluster_name": {
                "type": "string",
                "description": (
                    "Existing UP cluster to exec on. Same name passed to "
                    "compute_run(mode='cluster', cluster_name=...)."
                ),
            },
            "command": {
                "type": "string",
                "description": "Command to run on the cluster.",
            },
            "service": {
                "type": "string",
                "description": (
                    "Optional service name from the registry. Used only to "
                    "resolve the image when building the inline task; the "
                    "cluster's actual container/image is set at launch and "
                    "is not changed here."
                ),
            },
            "image": {
                "type": "string",
                "description": (
                    "Optional Docker image (e.g., 'python:3.11'). Same note "
                    "as service: informational only, the cluster's image was "
                    "fixed at launch."
                ),
            },
            "workdir": {
                "type": "string",
                "description": (
                    "Optional local directory to rsync to the cluster for "
                    "this exec. Per Sky's docs this is the only mount that "
                    "can change per exec call."
                ),
            },
            "timeout_sec": {
                "type": "integer",
                "description": (
                    "Optional max runtime (seconds) for the exec'd command. "
                    "Defaults to ComputeRequirements default; pass 0 to "
                    "disable the on-VM timeout wrapper."
                ),
            },
        },
        "required": ["cluster_name", "command"],
    }

    # Tolerate the model reaching for `cluster=` or other obvious aliases
    # so a kwarg-name typo doesn't surface as "unexpected keyword argument".
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
        cluster_name: str = "",
        command: str = "",
        service: Optional[str] = None,
        image: Optional[str] = None,
        workdir: Optional[str] = None,
        timeout_sec: Optional[int] = None,
        **kwargs,
    ) -> ToolResult:
        # Accept cluster name from common aliases.
        if not cluster_name:
            for alias in self._CLUSTER_ALIASES[1:]:
                value = kwargs.get(alias)
                if isinstance(value, str) and value.strip():
                    cluster_name = value
                    break

        if not cluster_name:
            return ToolResult(
                success=False,
                output=None,
                error="cluster_name is required (existing UP sky cluster).",
            )
        if not command:
            return ToolResult(
                success=False,
                output=None,
                error="command is required.",
            )

        # Reject commands that reference ~/sky_workdir/. The compute prompt
        # forbids this and compute_run validates it; compute_exec was the
        # gap where agents could still slip through (and have — the path
        # contract claim "the compute layer rejects commands that mention
        # it" must be true at every entry point, not just compute_run).
        from sciagent.tools.atomic.compute import _FORBIDDEN_PATTERN
        if _FORBIDDEN_PATTERN.search(command):
            return ToolResult(
                success=False,
                output=None,
                error=(
                    "Command references ~/sky_workdir/ but that's internal "
                    "SkyPilot — you cannot cd there or read from it. The "
                    "registry's `workdir` field tells you where your code "
                    "actually lands; use $OUTPUTS_DIR for outputs, the "
                    "declared workspace mount path for inputs."
                ),
            )

        from sciagent.compute.job import (
            ComputeRequirements,
            Job,
            LaunchError,
        )

        # Resolve image (informational — cluster's actual image is fixed
        # at launch). Mirrors compute_run's logic minimally.
        if service and image:
            return ToolResult(
                success=False,
                output=None,
                error="Specify 'service' OR 'image', not both.",
            )
        if service:
            resolved_image = f"ghcr.io/sciagent-ai/{service}:latest"
        elif image:
            resolved_image = image
        else:
            # Neither given: leave as None. The exec'd task doesn't need
            # an image because it inherits the cluster's container.
            resolved_image = None

        # Resources are immutable per cluster, but ComputeRequirements is
        # required by Job. Use the minimum-viable shape; the cluster's
        # actual resources apply during exec.
        requirements_kwargs: Dict[str, Any] = {
            "cpus": 1,
            "memory_gb": 1,
            "gpus": 0,
            "gpu_type": None,
        }
        if timeout_sec is not None:
            requirements_kwargs["timeout_sec"] = int(timeout_sec)
        requirements = ComputeRequirements(**requirements_kwargs)

        # Resolve workdir relative to the agent's project dir, same as
        # compute_run, so a relative "." or "_outputs" doesn't reach Sky
        # as-is (Sky rejects relative paths).
        if workdir is not None:
            from pathlib import Path

            wpath = Path(workdir)
            if not wpath.is_absolute():
                wpath = (Path(self._working_dir) / wpath).absolute()
            workdir = str(wpath)

        from sciagent.tools.atomic.compute import ComputeTool

        session_for_job = ComputeTool._shared_session_id
        job = Job(
            service=service or "custom",
            image=resolved_image or "",
            command=command,
            working_dir=self._working_dir,
            ship_workdir=workdir,
            requirements=requirements,
            session_id=session_for_job,
        )

        try:
            router = self._get_router()
        except Exception as exc:
            return ToolResult(
                success=False,
                output=None,
                error=f"Compute router unavailable: {exc}",
            )

        try:
            cluster, int_job_id = router.exec_on_cluster(
                job=job, cluster_name=cluster_name
            )
        except LaunchError as launch_exc:
            return ToolResult(
                success=False,
                output={
                    "cluster_name": cluster_name,
                    "command": command[:200],
                    "failure_type": "exec_rejected",
                    "hint": (
                        "Confirm the cluster is UP via "
                        f"compute_cluster(action='status', cluster_name='{cluster_name}')."
                    ),
                },
                error=f"sky.exec rejected: {launch_exc}",
            )
        except RuntimeError as exc:
            return ToolResult(
                success=False,
                output=None,
                error=str(exc),
            )

        return ToolResult(
            success=True,
            output={
                "cluster_name": cluster,
                "cluster_job_id": int_job_id,
                "status": "running",
                "backend": "skypilot",
                "mode": "cluster_exec",
                "message": (
                    f"Exec'd on warm cluster {cluster}; per-cluster job_id "
                    f"{int_job_id}. Tail logs via "
                    f"`sky logs {cluster} {int_job_id}` (CLI) or "
                    f"compute_cluster(action='status', cluster_name='{cluster}') "
                    f"to see the queue."
                ),
            },
        )
