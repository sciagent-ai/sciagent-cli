"""
Background job management tools.

Tools for managing background processes launched via bash(background=True):
- bg_status: Check status of background jobs
- bg_output: Get output from a background job
- bg_wait: Wait for a background job to complete
- bg_kill: Terminate a background job
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Dict, Any, Optional, List


@dataclass
class ToolResult:
    """Result from tool execution."""
    success: bool
    output: Any
    error: Optional[str] = None


def _cluster_name_hint(job_id: str) -> str:
    """Return a hint suffix when job_id is actually a known cluster name.

    Empty string when the id isn't a known cluster — keeps existing error
    messages intact for the common case. The agent's most common confusion
    is feeding compute_run's `cluster_name` into bg_*; this nudges it to
    compute_cluster instead.
    """
    try:
        from sciagent.compute.cluster_manifest import read_cluster
        if read_cluster(job_id) is not None:
            return (
                f" '{job_id}' looks like a cluster_name, not a job_id. "
                f"Try compute_cluster(action='status', cluster_name='{job_id}') "
                f"for cluster state, or compute_cluster(action='logs', "
                f"cluster_name='{job_id}', cluster_job_id=<N>) for per-job logs."
            )
    except Exception:
        pass
    return ""


class BgStatusTool:
    """Check status of background jobs."""

    name = "bg_status"
    description = "Check status of background jobs. Call with no args to list all jobs, or with job_id to check a specific job. Works for both local (bash) and compute (SkyPilot) jobs."

    parameters = {
        "type": "object",
        "properties": {
            "job_id": {
                "type": "string",
                "description": "Specific job ID to check. If omitted, lists all jobs."
            },
            "running_only": {
                "type": "boolean",
                "description": "If true, only show running jobs.",
                "default": False
            }
        },
        "required": []
    }

    def __init__(self, working_dir: str = "."):
        self.working_dir = working_dir

    def _is_compute_job(self, job_id: str) -> bool:
        """Check if job_id is a compute/SkyPilot job.

        Routes via task_index.kind_of so the manifest's kind field wins over
        a prefix collision — a future kind=subagent manifest with a
        sciagent-prefixed id won't get misrouted to the compute path.
        """
        from sciagent.compute.task_index import kind_of

        return kind_of(job_id) == "compute_job"

    def _get_compute_status(self, job_id: str) -> Optional[Dict[str, Any]]:
        """Get joined status from local task_index manifest + SkyPilot router.

        Either side may be missing — see compute.task_index.join_status for
        the four cases. A wholly absent sky_result is recovered to a transient
        PENDING so the formatter has something to show.
        """
        try:
            from sciagent.compute.router import ComputeRouter
            from sciagent.compute.task_index import join_status, read_task
            from sciagent.tools.registry import BaseTool

            router = ComputeRouter()
            try:
                # Pre-flight: a Ctrl+C just before the sky RPC should
                # skip it and fall back to the local manifest only,
                # so the user gets immediate control back instead of
                # waiting for an RPC we'd then ignore anyway.
                if BaseTool.is_interrupted():
                    sky_result = None
                else:
                    sky_result = router.get_status(job_id)
            except Exception:
                sky_result = None

            local = read_task(job_id)

            # Caller treats a wholly missing pair as "not found".
            if sky_result is None and local is None:
                return None

            joined = join_status(job_id=job_id, local=local, sky_result=sky_result)
            joined.setdefault("command", "(compute job)")
            joined.setdefault("working_dir", self.working_dir)
            joined.setdefault("start_time", joined.get("started_at", ""))
            return joined
        except Exception:
            return None

    def execute(self, job_id: str = None, running_only: bool = False) -> ToolResult:
        """Get status of background jobs."""
        from sciagent.process_manager import ProcessManager, JobStatus

        try:
            pm = ProcessManager.get_instance()

            if job_id:
                # Check if it's a compute job (SkyPilot)
                if self._is_compute_job(job_id):
                    status = self._get_compute_status(job_id)
                    if status is None:
                        return ToolResult(
                            success=False,
                            output=None,
                            error=(
                                f"Compute job '{job_id}' not found or SkyPilot not available."
                                + _cluster_name_hint(job_id)
                            )
                        )
                    output = self._format_compute_status(status)
                    return ToolResult(success=True, output=output, error=None)

                # Get specific job status from ProcessManager
                status = pm.get_status(job_id)
                if status is None:
                    return ToolResult(
                        success=False,
                        output=None,
                        error=(
                            f"Job '{job_id}' not found. Use bg_status() to list all jobs."
                            + _cluster_name_hint(job_id)
                        )
                    )

                output = self._format_job_status(status)
                return ToolResult(success=True, output=output, error=None)

            else:
                # List all jobs
                jobs = pm.list_jobs(include_completed=not running_only)

                if not jobs:
                    return ToolResult(
                        success=True,
                        output="No background jobs found.",
                        error=None
                    )

                output = self._format_job_list(jobs)
                return ToolResult(success=True, output=output, error=None)

        except Exception as e:
            return ToolResult(success=False, output=None, error=str(e))

    def _format_job_status(self, status: Dict[str, Any]) -> str:
        """Format a single job status for display."""
        lines = [
            f"Job ID: {status['job_id']}",
            f"Status: {status['status']}",
            f"Command: {status['command'][:100]}{'...' if len(status['command']) > 100 else ''}",
            f"Working Dir: {status['working_dir']}",
            f"Started: {status['start_time']}",
        ]

        if status.get('end_time'):
            lines.append(f"Ended: {status['end_time']}")

        if status.get('exit_code') is not None:
            lines.append(f"Exit Code: {status['exit_code']}")

        if status.get('pid'):
            lines.append(f"PID: {status['pid']}")

        lines.extend([
            f"",
            f"Output files:",
            f"  stdout: {status['stdout_file']}",
            f"  stderr: {status['stderr_file']}",
        ])

        return '\n'.join(lines)

    def _format_job_list(self, jobs: List[Dict[str, Any]]) -> str:
        """Format a list of jobs for display."""
        lines = [f"Background Jobs ({len(jobs)} total):", ""]

        # Group by status
        running = [j for j in jobs if j['status'] == 'running']
        completed = [j for j in jobs if j['status'] == 'completed']
        failed = [j for j in jobs if j['status'] == 'failed']
        killed = [j for j in jobs if j['status'] == 'killed']

        if running:
            lines.append(f"RUNNING ({len(running)}):")
            for job in running:
                cmd_preview = job['command'][:50] + '...' if len(job['command']) > 50 else job['command']
                lines.append(f"  [{job['job_id']}] {cmd_preview}")
            lines.append("")

        if completed:
            lines.append(f"COMPLETED ({len(completed)}):")
            for job in completed[:5]:  # Show last 5
                cmd_preview = job['command'][:50] + '...' if len(job['command']) > 50 else job['command']
                lines.append(f"  [{job['job_id']}] exit={job['exit_code']} {cmd_preview}")
            if len(completed) > 5:
                lines.append(f"  ... and {len(completed) - 5} more")
            lines.append("")

        if failed:
            lines.append(f"FAILED ({len(failed)}):")
            for job in failed[:5]:
                cmd_preview = job['command'][:50] + '...' if len(job['command']) > 50 else job['command']
                lines.append(f"  [{job['job_id']}] exit={job['exit_code']} {cmd_preview}")
            if len(failed) > 5:
                lines.append(f"  ... and {len(failed) - 5} more")
            lines.append("")

        if killed:
            lines.append(f"KILLED ({len(killed)}):")
            for job in killed[:3]:
                cmd_preview = job['command'][:50] + '...' if len(job['command']) > 50 else job['command']
                lines.append(f"  [{job['job_id']}] {cmd_preview}")
            lines.append("")

        return '\n'.join(lines)

    def _format_compute_status(self, status: Dict[str, Any]) -> str:
        """Format compute job status for display.

        Surfaces local task_index fields (intent / expected_artifacts /
        owner_pid / started_at) underneath the cloud-side status when they
        are present in the joined dict.
        """
        lines = [
            f"Compute Job: {status['job_id']}",
            f"Backend: {status.get('backend', 'skypilot')}",
            f"Status: {status['status']}",
            f"Summary: {status.get('summary', '')}",
        ]

        # Local manifest fields (passthrough — opaque shape per v4.2 §C6).
        # Kind/state surface the in-flight registry's view; pre-PR1 manifests
        # default to compute_job/running via join_status's setdefault.
        if status.get("kind"):
            lines.append(f"Kind: {status['kind']}")
        if status.get("state"):
            lines.append(f"State: {status['state']}")
        if status.get("completed_at"):
            lines.append(f"Completed: {status['completed_at']}")
        if status.get("result_summary"):
            lines.append(f"Result: {status['result_summary']}")
        if status.get("managed_job_id") is not None:
            lines.append(f"Managed job id: {status['managed_job_id']}")
        if status.get("session_id"):
            lines.append(f"Session: {status['session_id']}")
        if status.get("owner_pid"):
            lines.append(f"Owner PID: {status['owner_pid']}")
        if status.get("started_at"):
            lines.append(f"Started: {status['started_at']}")
        intent = status.get("intent")
        if intent:
            lines.append(f"Intent: {intent}")
        artifacts = status.get("expected_artifacts")
        if artifacts:
            lines.append(f"Expected artifacts: {artifacts}")

        # Include error content if present (critical for debugging failures)
        error_preview = status.get('error_preview', '')
        if error_preview:
            lines.append("")
            lines.append("Error output:")
            lines.append(error_preview)

        # Include log file path if available (agent can read for full details)
        output_file = status.get('output_file', '')
        if output_file:
            lines.append("")
            lines.append(f"Full logs: {output_file}")

        lines.append("")
        # Managed-jobs CLI hints (M1A): sky logs/down are cluster-mode; the
        # managed-jobs equivalents are sky jobs logs / sky jobs cancel.
        lines.append(f"Use 'sky jobs logs {status['job_id']}' to view full logs.")
        lines.append(
            f"Use 'sky jobs cancel {status['job_id']}' to stop billing."
        )
        return '\n'.join(lines)

    def to_schema(self) -> Dict:
        """Convert to OpenAI-style tool schema."""
        return {
            "name": self.name,
            "description": self.description,
            "parameters": self.parameters
        }


class BgOutputTool:
    """Get output from a background job."""

    name = "bg_output"
    description = "Get stdout/stderr output from a background job. Use tail_lines to get only recent output. For compute jobs, retrieves logs from the cloud cluster."

    parameters = {
        "type": "object",
        "properties": {
            "job_id": {
                "type": "string",
                "description": "The job ID to get output from"
            },
            "stream": {
                "type": "string",
                "enum": ["stdout", "stderr", "both"],
                "description": "Which output stream to retrieve",
                "default": "stdout"
            },
            "tail_lines": {
                "type": "integer",
                "description": "Only return the last N lines. If omitted, returns all output.",
                "default": None
            }
        },
        "required": ["job_id"]
    }

    def __init__(self, working_dir: str = "."):
        self.working_dir = working_dir

    def _is_compute_job(self, job_id: str) -> bool:
        """Check if job_id is a compute/SkyPilot job.

        Routes via task_index.kind_of so the manifest's kind field wins over
        a prefix collision — a future kind=subagent manifest with a
        sciagent-prefixed id won't get misrouted to the compute path.
        """
        from sciagent.compute.task_index import kind_of

        return kind_of(job_id) == "compute_job"

    def _get_compute_output(self, job_id: str, tail_lines: int = None) -> ToolResult:
        """Get logs from a compute/SkyPilot job."""
        # Pre-flight: a Ctrl+C just before this call should cancel before
        # the sky RPC. Once the RPC is in flight we can't preempt it
        # cleanly — it owns the thread — so the best we can do is bail
        # before submitting.
        from sciagent.tools.registry import BaseTool
        if BaseTool.is_interrupted():
            return ToolResult(
                success=True,
                output=f"bg_output on {job_id} cancelled by user before fetch.",
                error=None,
            )
        try:
            from sciagent.compute.backends.skypilot import SkyPilotBackend
            backend = SkyPilotBackend()
            logs = backend.get_logs(job_id, tail=tail_lines or 100)
            return ToolResult(
                success=True,
                output=logs if logs else "(no output available)",
                error=None
            )
        except Exception as e:
            # Include exception type + location so opaque errors like
            # "string indices must be integers, not 'str'" are debuggable
            # without re-running. tb_pretty is bounded to the last 5 frames
            # so we don't blow up the agent's context with a 50-line stack.
            import traceback as _tb
            tb_lines = _tb.format_exception(type(e), e, e.__traceback__)
            tail = "".join(tb_lines[-6:])
            return ToolResult(
                success=False,
                output=None,
                error=(
                    f"bg_output failed for {job_id}: "
                    f"{type(e).__name__}: {e}\n\n"
                    f"Traceback (last frames):\n{tail}\n"
                    f"Fallback: `sky jobs logs {job_id}` via bash."
                ),
            )

    def execute(
        self,
        job_id: str = None,
        stream: str = "stdout",
        tail_lines: int = None
    ) -> ToolResult:
        """Get output from a background job."""
        from sciagent.process_manager import ProcessManager

        if not job_id:
            return ToolResult(
                success=False,
                output=None,
                error="job_id is required. Use bg_status() to list available jobs."
            )

        # Handle compute jobs (SkyPilot)
        if self._is_compute_job(job_id):
            return self._get_compute_output(job_id, tail_lines)

        try:
            pm = ProcessManager.get_instance()

            # Check job exists
            status = pm.get_status(job_id)
            if status is None:
                return ToolResult(
                    success=False,
                    output=None,
                    error=f"Job '{job_id}' not found." + _cluster_name_hint(job_id)
                )

            # Get output
            if stream == "both":
                stdout = pm.get_output(job_id, "stdout", tail_lines)
                stderr = pm.get_output(job_id, "stderr", tail_lines)

                output_parts = []
                if stdout and stdout.strip():
                    output_parts.append(f"=== STDOUT ===\n{stdout}")
                if stderr and stderr.strip():
                    output_parts.append(f"=== STDERR ===\n{stderr}")

                if not output_parts:
                    output = "(no output yet)"
                else:
                    output = "\n\n".join(output_parts)
            else:
                output = pm.get_output(job_id, stream, tail_lines)
                if not output or not output.strip():
                    output = f"(no {stream} output yet)"

            # Add status info
            status_line = f"\n\n[Job {job_id}: {status['status']}]"
            if status['status'] == 'running':
                status_line += " (still running, output may be incomplete)"

            return ToolResult(
                success=True,
                output=output + status_line,
                error=None
            )

        except Exception as e:
            return ToolResult(success=False, output=None, error=str(e))

    def to_schema(self) -> Dict:
        """Convert to OpenAI-style tool schema."""
        return {
            "name": self.name,
            "description": self.description,
            "parameters": self.parameters
        }


class BgWaitTool:
    """Wait for a background job to complete."""

    name = "bg_wait"
    description = (
        "Wait for a background job. For LOCAL (bash) jobs, blocks until the "
        "job completes or the timeout elapses. For CLOUD (SkyPilot) jobs, "
        "default behavior is a one-shot snapshot (M1A non-blocking contract); "
        "pass block=True to long-poll internally (10s interval, up to "
        "`timeout` seconds, default 600s) and return only on terminal status. "
        "Long-poll collapses N polling turns into 1 — biggest token win for "
        "jobs that take more than ~30s. On COMPLETED, auto-pulls "
        "/workspace/_outputs/ from the bucket to local; the file list is "
        "in the result."
    )

    # Interrupt awareness comes from the BaseTool shared event (set by
    # AgentLoop at startup). When the user hits Ctrl+C, the polling loop
    # in _wait_compute_job bails with a structured "interrupted" result
    # instead of holding the agent loop captive for the full timeout.
    # Standalone callers (no AgentLoop) leave the event unset and the
    # tool falls back to plain time.sleep.

    parameters = {
        "type": "object",
        "properties": {
            "job_id": {
                "type": "string",
                "description": "The job ID to wait for"
            },
            "timeout": {
                "type": "integer",
                "description": (
                    "Maximum seconds to wait. For LOCAL jobs: omit for "
                    "indefinite wait. For CLOUD jobs with block=True: "
                    "defaults to 600s. Ignored for CLOUD jobs in default "
                    "(snapshot) mode."
                )
            },
            "block": {
                "type": "boolean",
                "description": (
                    "Cloud jobs only. False (default): return a snapshot — "
                    "honors the M1A non-blocking contract; cheap; agent "
                    "polls explicitly. True: long-poll internally every "
                    "10s up to `timeout`, returning early on terminal. Use "
                    "block=True for jobs you expect to finish within a few "
                    "minutes — saves N polling turns × full context. For "
                    "hours-long simulations, prefer snapshot + sparse "
                    "agent-paced re-checks."
                ),
                "default": False
            }
        },
        "required": ["job_id"]
    }

    def __init__(self, working_dir: str = "."):
        self.working_dir = working_dir

    def _is_compute_job(self, job_id: str) -> bool:
        """Check if job_id is a compute/SkyPilot job.

        Routes via task_index.kind_of so the manifest's kind field wins over
        a prefix collision — a future kind=subagent manifest with a
        sciagent-prefixed id won't get misrouted to the compute path.
        """
        from sciagent.compute.task_index import kind_of

        return kind_of(job_id) == "compute_job"

    # Long-poll cadence for block=True. 10s strikes a balance between
    # responsiveness (terminal status surfaced quickly) and cluster-load
    # (sky.jobs.queue is not free; controllers throttle aggressive callers).
    _BLOCK_POLL_INTERVAL_SEC = 10
    _BLOCK_DEFAULT_TIMEOUT_SEC = 600

    @staticmethod
    def _record_terminal_state(job_id: str, result: Any) -> None:
        """Drive the in-flight registry's lifecycle state from a terminal sky
        status. Best-effort — a failure to update the manifest must not break
        the wait result (the job has already terminated cloud-side; the worst
        case is a stale state field that the next bg_status will refresh).

        Called only from the block=True path; the snapshot path (block=False)
        stays read-only per M1A hard rule #1.
        """
        try:
            from sciagent.compute.job import JobStatus
            from sciagent.compute.task_index import update_task_state

            mapping = {
                JobStatus.COMPLETED: "completed",
                JobStatus.FAILED: "failed",
                JobStatus.CANCELLED: "cancelled",
            }
            lifecycle_state = mapping.get(result.status)
            if lifecycle_state is None:
                return
            summary = (result.error_preview or result.summary or "")[:120]
            update_task_state(
                job_id,
                lifecycle_state,
                result_summary=summary or None,
            )
        except Exception:
            pass

    def _wait_compute_job(self, job_id: str, timeout: int = None, block: bool = False) -> ToolResult:
        """Wait on a cloud job's status.

        M1A hard rule #1 says atomic tools for cloud jobs are non-blocking
        and one-shot. We preserve that as the **default** (block=False)
        because the multi-job / parallel cases need it and M2A's resume
        substrate depends on it.

        Opt-in long-poll (``block=True``) trades that for a token win on
        the common single-job-wait case: instead of N polling turns each
        replaying the full agent context, we make one tool call that
        internally polls every ``_BLOCK_POLL_INTERVAL_SEC`` until terminal
        or until ``timeout``. The job itself still runs on sky's
        controller; the in-process wait is best-effort — if sciagent
        crashes during the wait, the manifest-based resume path picks
        the job up by job_id.

        Returns:
          - terminal (COMPLETED / FAILED / CANCELLED) -> structured result.
          - non-terminal (PENDING / RUNNING / RECOVERING) AND block=False
            -> snapshot pointing the agent at bg_status for re-polling.
          - non-terminal AND block=True AND timeout reached -> snapshot
            telling the caller the wait expired (state is still pending);
            agent can re-issue with a longer timeout or fall back to
            sparse re-polling.
        """
        try:
            from sciagent.compute.job import JobStatus
            from sciagent.compute.router import ComputeRouter

            router = ComputeRouter()

            # When block=True, internally poll until terminal or timeout.
            # We thread a small loop here rather than recurse so a long
            # wait emits a single tool result, not nested ones.
            if block:
                from sciagent.tools.registry import BaseTool

                effective_timeout = timeout if timeout and timeout > 0 else self._BLOCK_DEFAULT_TIMEOUT_SEC
                deadline = time.monotonic() + effective_timeout
                interrupt = BaseTool._shared_interrupt_event

                while True:
                    # Bail before the next sky RPC if Ctrl+C already fired.
                    # Returns a structured "interrupted" result so the agent
                    # knows the wait was cancelled (vs. completed or timed
                    # out) and can decide whether to bg_kill or re-check.
                    if interrupt is not None and interrupt.is_set():
                        return ToolResult(
                            success=True,
                            output=(
                                f"bg_wait on {job_id} interrupted by user. "
                                f"The cloud job is still running on Sky — use "
                                f"bg_status('{job_id}') to check, or "
                                f"bg_kill('{job_id}') to cancel it."
                            ),
                            error=None,
                        )

                    result = router.get_status(job_id)
                    if result.status in (JobStatus.COMPLETED, JobStatus.FAILED, JobStatus.CANCELLED):
                        # PR1 (consolidation): the long-poll path observed a
                        # terminal — write the lifecycle state to the manifest
                        # so cross-session readers don't have to re-query sky.
                        # Snapshot mode (block=False) deliberately skips this
                        # to keep its read-only contract intact.
                        self._record_terminal_state(job_id, result)
                        break
                    if time.monotonic() >= deadline:
                        # Timed out without terminal — return a snapshot
                        # with a clear hint about the budget.
                        return ToolResult(
                            success=True,
                            output=(
                                f"Cloud job {job_id} still {result.status.value} after "
                                f"{effective_timeout}s long-poll budget.\n\n"
                                f"Summary: {result.summary}\n\n"
                                f"Re-issue bg_wait with block=True and a longer timeout, "
                                f"or fall back to sparse bg_status re-checks."
                            ),
                            error=None,
                        )
                    # Sleep up to the next interval, but never past the
                    # deadline. Use Event.wait() instead of time.sleep()
                    # when the interrupt event is plumbed in: wait()
                    # returns True if the event is set, so a Ctrl+C wakes
                    # the wait immediately and the next loop iteration
                    # exits via the interrupt-check branch above.
                    sleep_for = min(self._BLOCK_POLL_INTERVAL_SEC, max(0.0, deadline - time.monotonic()))
                    if sleep_for > 0:
                        if interrupt is not None:
                            if interrupt.wait(sleep_for):
                                # Loop back; the interrupt-check branch
                                # at the top of the while will return.
                                continue
                        else:
                            time.sleep(sleep_for)
                # Fall through with the terminal `result`.
            else:
                # Snapshot path: also honor an already-set interrupt so a
                # user who hit Ctrl+C just before bg_wait was called gets
                # immediate control back instead of one more sky RPC.
                from sciagent.tools.registry import BaseTool

                interrupt = BaseTool._shared_interrupt_event
                if interrupt is not None and interrupt.is_set():
                    return ToolResult(
                        success=True,
                        output=(
                            f"bg_wait on {job_id} interrupted before "
                            f"status query. Use bg_status('{job_id}') to "
                            f"check, or bg_kill('{job_id}') to cancel."
                        ),
                        error=None,
                    )
                result = router.get_status(job_id)

            if result.status == JobStatus.COMPLETED:
                # Auto-pull workspace outputs from the cloud bucket. Folded
                # into bg_wait (rather than exposed as a separate tool) so
                # the agent's mental model is "I run a job and get files
                # back" — no extra tool call. Best-effort: a fetch failure
                # doesn't fail the wait (the job did succeed); the reason
                # is surfaced so the agent can act on it.
                fetch_lines = []
                workspace_session_id: Optional[str] = None
                try:
                    from .compute_fetch import fetch_workspace_outputs
                    from sciagent.compute.task_index import read_task

                    manifest = read_task(job_id) or {}
                    workspace_session_id = manifest.get("session_id")

                    fetched = fetch_workspace_outputs(
                        job_id=job_id,
                        working_dir=self.working_dir,
                    )
                    if fetched.get("ok") and fetched.get("file_count", 0) > 0:
                        fetch_lines = [
                            "",
                            f"Fetched {fetched['file_count']} file(s) "
                            f"({fetched['bytes_total']:,} bytes) from "
                            f"{fetched['bucket']} to {fetched['dest']}/_outputs/:",
                        ]
                        # Token-light: paths only, no contents. Cap at 20
                        # entries so a job that produced hundreds of files
                        # doesn't bloat the wait result.
                        for f in fetched["files"][:20]:
                            fetch_lines.append(f"  - {f['path']} ({f['bytes']:,} B)")
                        if fetched["file_count"] > 20:
                            fetch_lines.append(
                                f"  ... and {fetched['file_count'] - 20} more"
                            )
                    elif fetched.get("ok"):
                        # Sync succeeded but the bucket was empty. The job
                        # finished without writing anything to $OUTPUTS_DIR.
                        # Most common cause: the run command wrote to
                        # /workspace, /tmp, or relative paths instead of
                        # /outputs/<job_id>/. Be explicit about the fix
                        # so the agent doesn't blame mounting / S3 / sky.
                        fetch_lines = [
                            "",
                            "(no files in /outputs/<job_id>/ — job ran but "
                            "didn't write outputs. Cause: the run command "
                            "must `cp` results into $OUTPUTS_DIR (= "
                            f"/outputs/{job_id}/) to be auto-fetched. "
                            f"Anything written elsewhere on the cluster is "
                            f"scratch and gone at teardown.)",
                            f"Diagnose: `sky jobs logs {job_id}` to see "
                            f"what the command actually did.",
                        ]
                    else:
                        # Surface the reason but don't dramatize it.
                        fetch_lines = [
                            "",
                            f"(outputs not auto-fetched: {fetched.get('reason')})",
                        ]
                except Exception as fetch_err:
                    fetch_lines = ["", f"(outputs not auto-fetched: {fetch_err})"]

                # Also pull the durable session workspace (the /workspace/
                # mount). /outputs/<job_id>/ is per-job; /workspace/ is the
                # cross-step bucket and the natural place for the LLM to
                # write artifacts it wants to read back locally. Without
                # this second fetch, produces_uris that point at
                # /workspace/... fail validation because the files only
                # exist in the cloud bucket. Same best-effort handling as
                # the outputs fetch.
                if workspace_session_id:
                    try:
                        from .compute_fetch import fetch_session_workspace

                        ws_fetched = fetch_session_workspace(
                            session_id=workspace_session_id,
                            working_dir=self.working_dir,
                        )
                        if ws_fetched.get("ok") and ws_fetched.get("file_count", 0) > 0:
                            fetch_lines.extend([
                                "",
                                f"Fetched {ws_fetched['file_count']} "
                                f"workspace file(s) "
                                f"({ws_fetched['bytes_total']:,} bytes) "
                                f"from {ws_fetched['bucket']} to "
                                f"{ws_fetched['dest']}/_outputs/workspace/.",
                            ])
                        elif not ws_fetched.get("ok"):
                            fetch_lines.append(
                                f"(workspace not auto-fetched: "
                                f"{ws_fetched.get('reason')})"
                            )
                    except Exception as ws_err:
                        fetch_lines.append(
                            f"(workspace not auto-fetched: {ws_err})"
                        )

                return ToolResult(
                    success=True,
                    output=(
                        f"Compute job {job_id} completed.\n\n"
                        f"Status: {result.status.value}\n"
                        f"Summary: {result.summary}"
                        + "\n".join(fetch_lines)
                        + f"\n\nUse 'sky jobs logs {job_id}' to view full logs.\n"
                        f"Use 'sky jobs cancel {job_id}' if you need to stop a follow-up."
                    ),
                    error=None,
                )

            if result.status == JobStatus.FAILED:
                output_lines = [
                    f"Compute job {job_id} failed.",
                    "",
                    f"Summary: {result.summary}",
                ]
                if result.error_preview:
                    output_lines.extend(["", "Error preview:", result.error_preview])
                if result.output_file:
                    output_lines.extend(["", f"Full logs: {result.output_file}"])
                output_lines.extend(
                    ["", f"Use 'sky jobs logs {job_id}' to view error logs."]
                )
                return ToolResult(
                    success=False,
                    output="\n".join(output_lines),
                    error=result.error_preview or result.summary,
                )

            if result.status == JobStatus.CANCELLED:
                return ToolResult(
                    success=False,
                    output=(
                        f"Compute job {job_id} was cancelled.\n\n"
                        f"Status: {result.status.value}\n"
                        f"Summary: {result.summary}"
                    ),
                    error=f"job {job_id} cancelled",
                )

            # Non-terminal — return a snapshot. ``timeout`` is intentionally
            # ignored for cloud jobs (hard rule #1); the message names the
            # follow-up tools the agent should use to re-check.
            return ToolResult(
                success=True,
                output=(
                    f"Cloud job {job_id} is {result.status.value} (snapshot only — "
                    f"bg_wait does not block for cloud jobs).\n\n"
                    f"Summary: {result.summary}\n\n"
                    f"Re-check with bg_status('{job_id}') or bg_output('{job_id}'); "
                    f"the agent loop continues without waiting."
                ),
                error=None,
            )

        except Exception as e:
            return ToolResult(success=False, output=None, error=str(e))

    def execute(self, job_id: str = None, timeout: int = None, block: bool = False) -> ToolResult:
        """Wait for a background job to complete."""
        from sciagent.process_manager import ProcessManager

        if not job_id:
            return ToolResult(
                success=False,
                output=None,
                error="job_id is required."
            )

        # Handle compute jobs (SkyPilot)
        if self._is_compute_job(job_id):
            return self._wait_compute_job(job_id, timeout, block=block)

        try:
            pm = ProcessManager.get_instance()

            # Check job exists
            initial_status = pm.get_status(job_id)
            if initial_status is None:
                return ToolResult(
                    success=False,
                    output=None,
                    error=f"Job '{job_id}' not found." + _cluster_name_hint(job_id)
                )

            # If already completed, return immediately
            if initial_status['status'] != 'running':
                output = self._format_completion(initial_status)
                return ToolResult(success=True, output=output, error=None)

            # Wait for completion
            final_status = pm.wait(job_id, timeout=timeout)

            if final_status['status'] == 'running':
                # Timeout occurred
                return ToolResult(
                    success=True,
                    output=f"Timeout after {timeout}s. Job {job_id} is still running.\n"
                           f"Use bg_wait(job_id=\"{job_id}\") to continue waiting.",
                    error=None
                )

            output = self._format_completion(final_status)
            return ToolResult(success=True, output=output, error=None)

        except Exception as e:
            return ToolResult(success=False, output=None, error=str(e))

    def _format_completion(self, status: Dict[str, Any]) -> str:
        """Format job completion info."""
        lines = [
            f"Job {status['job_id']} {status['status']}",
            f"",
            f"Exit Code: {status.get('exit_code', 'unknown')}",
            f"Started: {status['start_time']}",
            f"Ended: {status.get('end_time', 'unknown')}",
            f"",
            f"Command: {status['command'][:100]}{'...' if len(status['command']) > 100 else ''}",
            f"",
            f"Use bg_output(job_id=\"{status['job_id']}\") to view full output.",
        ]
        return '\n'.join(lines)

    def to_schema(self) -> Dict:
        """Convert to OpenAI-style tool schema."""
        return {
            "name": self.name,
            "description": self.description,
            "parameters": self.parameters
        }


class BgKillTool:
    """Terminate a background job."""

    name = "bg_kill"
    description = "Terminate a running background job. For compute jobs, this terminates the cloud cluster. Use force=True for SIGKILL instead of SIGTERM (local jobs only)."

    parameters = {
        "type": "object",
        "properties": {
            "job_id": {
                "type": "string",
                "description": "The job ID to terminate"
            },
            "force": {
                "type": "boolean",
                "description": "Use SIGKILL instead of SIGTERM (immediate termination, local jobs only)",
                "default": False
            }
        },
        "required": ["job_id"]
    }

    def __init__(self, working_dir: str = "."):
        self.working_dir = working_dir

    def _is_compute_job(self, job_id: str) -> bool:
        """Check if job_id is a compute/SkyPilot job.

        Routes via task_index.kind_of so the manifest's kind field wins over
        a prefix collision — a future kind=subagent manifest with a
        sciagent-prefixed id won't get misrouted to the compute path.
        """
        from sciagent.compute.task_index import kind_of

        return kind_of(job_id) == "compute_job"

    def _kill_compute_job(self, job_id: str) -> ToolResult:
        """Terminate a compute/SkyPilot cluster."""
        try:
            from sciagent.compute.router import ComputeRouter
            router = ComputeRouter()

            if router.cleanup(job_id):
                # PR1 (consolidation): record the user-driven cancellation in
                # the manifest so cross-session readers see state=cancelled
                # without re-querying sky. Best-effort — a manifest-write
                # failure must not turn a successful kill into an apparent
                # tool failure.
                try:
                    from sciagent.compute.task_index import update_task_state

                    update_task_state(
                        job_id,
                        "cancelled",
                        result_summary="user-cancelled via bg_kill",
                    )
                except Exception:
                    pass
                return ToolResult(
                    success=True,
                    output=f"Compute cluster {job_id} terminated.\n\n"
                           f"Billing has stopped for this cluster.",
                    error=None
                )
            else:
                return ToolResult(
                    success=False,
                    output=None,
                    error=f"Failed to terminate cluster '{job_id}'. It may have already been terminated."
                )
        except Exception as e:
            return ToolResult(success=False, output=None, error=str(e))

    def execute(self, job_id: str = None, force: bool = False) -> ToolResult:
        """Terminate a background job."""
        from sciagent.process_manager import ProcessManager

        if not job_id:
            return ToolResult(
                success=False,
                output=None,
                error="job_id is required."
            )

        # Handle compute jobs (SkyPilot)
        if self._is_compute_job(job_id):
            return self._kill_compute_job(job_id)

        try:
            pm = ProcessManager.get_instance()

            # Check job exists
            status = pm.get_status(job_id)
            if status is None:
                return ToolResult(
                    success=False,
                    output=None,
                    error=f"Job '{job_id}' not found." + _cluster_name_hint(job_id)
                )

            if status['status'] != 'running':
                return ToolResult(
                    success=False,
                    output=None,
                    error=f"Job '{job_id}' is not running (status: {status['status']})"
                )

            # Kill the job
            killed = pm.kill(job_id, force=force)

            if killed:
                signal_type = "SIGKILL" if force else "SIGTERM"
                output = (
                    f"Job {job_id} terminated with {signal_type}.\n"
                    f"\n"
                    f"Command: {status['command'][:100]}{'...' if len(status['command']) > 100 else ''}\n"
                    f"PID: {status.get('pid', 'unknown')}\n"
                    f"\n"
                    f"Use bg_output(job_id=\"{job_id}\") to view any output before termination."
                )
                return ToolResult(success=True, output=output, error=None)
            else:
                return ToolResult(
                    success=False,
                    output=None,
                    error=f"Failed to kill job '{job_id}'. It may have already completed."
                )

        except Exception as e:
            return ToolResult(success=False, output=None, error=str(e))

    def to_schema(self) -> Dict:
        """Convert to OpenAI-style tool schema."""
        return {
            "name": self.name,
            "description": self.description,
            "parameters": self.parameters
        }


# Factory functions for tool discovery
def get_bg_status_tool(working_dir: str = ".") -> BgStatusTool:
    """Factory for bg_status tool."""
    return BgStatusTool(working_dir)


def get_bg_output_tool(working_dir: str = ".") -> BgOutputTool:
    """Factory for bg_output tool."""
    return BgOutputTool(working_dir)


def get_bg_wait_tool(working_dir: str = ".") -> BgWaitTool:
    """Factory for bg_wait tool."""
    return BgWaitTool(working_dir)


def get_bg_kill_tool(working_dir: str = ".") -> BgKillTool:
    """Factory for bg_kill tool."""
    return BgKillTool(working_dir)
