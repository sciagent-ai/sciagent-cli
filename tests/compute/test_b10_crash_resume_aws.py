"""B10 — crash-resume smoke test on real AWS.

Acceptance bar (v4.1 §2 / v4.2 §C1): launch a job, simulate a sciagent crash
(no further interaction with the launched cluster from the launching
process), then prove a fresh process can re-discover the job using:

  - the manifest at ``~/.sciagent/tasks/<job_id>.json`` (B7)
  - ``sky.jobs.queue_v2`` filtered by job_name (M1A — replaces M0's
    ``sky.queue(cluster_name=job_id)`` cluster-mode lookup)

This test is **PAID**. Cost ~$0.05 (small Debian-based container running
sleep) and ~3–5 min wall-clock. CI gates it behind ``RUN_AWS_TESTS=1``.

Image note: SkyPilot starts the container with ``/bin/bash`` as the
entrypoint. Alpine-style images without bash will fail with
``exec: "/bin/bash": stat /bin/bash: no such file or directory`` during
container start, which our fail-fast poll then (correctly) surfaces as
a LaunchError. Use a Debian/Ubuntu-based image — ``python:3.11-slim``
is small, widely cached, and has bash.

To run:

    export RUN_AWS_TESTS=1
    export B10_CLEANUP=1   # default 1; set 0 to inspect the cluster after
    pytest tests/compute/test_b10_crash_resume_aws.py -v -s

Implementation note: pytest cannot kill -9 itself, so we simulate the crash
by writing the manifest, dropping the in-process router/backend references,
and constructing fresh ones to validate resume. The launch-time process is
the "first" sciagent; a brand-new ``ComputeRouter()`` + ``read_task()`` is
the "fresh" process. This catches the same regression class kill -9 would
expose: state held only in the original process is lost.
"""

from __future__ import annotations

import os
import time

import pytest


pytestmark = pytest.mark.skipif(
    os.environ.get("RUN_AWS_TESTS") != "1",
    reason="paid AWS test; set RUN_AWS_TESTS=1 to run",
)


def test_b10_crash_resume_via_manifest_and_sky_queue():
    """Launch a cheap alpine sleep job, drop all in-process handles,
    re-discover via the manifest + sky.queue, assert coherent status."""
    from sciagent.compute.backends.skypilot import SkyPilotBackend
    from sciagent.compute.router import ComputeRouter
    from sciagent.compute.task_index import read_task
    from sciagent.tools.atomic.compute import ComputeTool

    cleanup_after = os.environ.get("B10_CLEANUP", "1") != "0"

    ComputeTool._shared_session_id = None
    tool = ComputeTool()
    job_id = None
    try:
        # First "process": launch an inexpensive job that sleeps long enough
        # for the resume probe to run while it's still on the cluster.
        # python:3.11-slim has bash (alpine doesn't — Sky's container
        # entrypoint is /bin/bash so that combination would launch-fail).
        result = tool.execute(
            image="python:3.11-slim",
            command="sleep 300",
            cpus=2,
            memory_gb=2,
            backend="skypilot",
            background=True,
            timeout_sec=600,
            intent={"test": "B10", "purpose": "crash-resume"},
        )
        # On a launch failure, ComputeTool surfaces the would-be cluster
        # name in output["job_id"] so we can still clean up. Capture it
        # before failing the assert.
        if result.output and result.output.get("job_id"):
            job_id = result.output["job_id"]
        assert result.success, f"launch failed: {result.error}"
        assert job_id

        # Wait briefly for the manifest to land + the cluster to register
        # in sky.queue. The manifest is written synchronously by
        # ComputeTool, but the cluster takes a few seconds to appear.
        time.sleep(15)

        # ---- Simulate the crash: drop every in-process handle. ----
        del tool, result
        # The "fresh" sciagent process below has no Python state from above
        # — only the on-disk manifest and the cloud-side cluster.

        # ---- Fresh process resume path. ----
        manifest = read_task(job_id)
        assert manifest is not None, (
            f"B7 manifest missing at ~/.sciagent/tasks/{job_id}.json — "
            "resume path can't function without it"
        )
        assert manifest["job_id"] == job_id
        assert manifest["intent"]["test"] == "B10"
        assert manifest["intent"]["purpose"] == "crash-resume"
        assert manifest.get("owner_pid"), "manifest missing owner_pid"
        assert manifest.get("started_at"), "manifest missing started_at"

        # Re-discover via sky.jobs.queue_v2 (M1A). The fresh router has no
        # in-process state from the launching process — only the manifest
        # on disk and the controller-side record drives this query.
        fresh_router = ComputeRouter()
        result_status = fresh_router.get_status(job_id)
        assert result_status is not None, "router returned no status for resumed job"

        # The job should be in some non-terminal state at this point
        # (PENDING / RUNNING). FAILED would indicate a launch problem
        # we'd want to surface; COMPLETED would mean the alpine sleep
        # finished faster than expected (network/sleep semantics differ).
        # Either way, the resume path WORKED — we got a coherent status
        # from a completely fresh process.
        from sciagent.compute.job import JobStatus
        assert result_status.status in (
            JobStatus.PENDING,
            JobStatus.RUNNING,
            JobStatus.COMPLETED,
        ), f"unexpected resumed status: {result_status.status} / {result_status.summary}"

    finally:
        if cleanup_after and job_id:
            try:
                SkyPilotBackend().cleanup(job_id, purge=False)
            except Exception:
                pass


if __name__ == "__main__":
    pytest.main([__file__, "-v", "-s"])
