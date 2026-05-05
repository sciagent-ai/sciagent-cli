#!/usr/bin/env python3
"""
Test OpenFOAM -> ParaView workflow on SkyPilot with shared workspace.

Minimal test - relies on container images having proper setup.
"""

import sys
import time
import uuid

sys.path.insert(0, "src")

from sciagent.tools.atomic.compute import ComputeTool


def wait_for_job(compute_tool, job_id: str, timeout_sec: int = 1200) -> bool:
    """Wait for a SkyPilot job to complete."""
    from sciagent.compute.job import JobStatus

    router = compute_tool._get_router()
    start = time.time()

    while time.time() - start < timeout_sec:
        try:
            result = router.get_status(job_id)
            print(f"  [{job_id}] {result.status.value}: {result.summary}")

            if result.status == JobStatus.COMPLETED:
                return True
            elif result.status == JobStatus.FAILED:
                print(f"  Error: {result.error_preview}")
                return False
        except Exception as e:
            print(f"  Error: {e}")

        time.sleep(30)

    print(f"Timeout waiting for {job_id}")
    return False


def cleanup_cluster(job_id: str):
    """Clean up SkyPilot cluster."""
    from sciagent.compute.backends.skypilot import SkyPilotBackend
    backend = SkyPilotBackend()
    backend.cleanup(job_id, purge=True)
    print(f"  Cleaned up: {job_id}")


def main():
    session_id = f"test-{uuid.uuid4().hex[:6]}"
    print(f"Session: {session_id}")

    compute = ComputeTool(session_id=session_id)
    job_ids = []

    try:
        # Job 1: OpenFOAM - cavity case
        print("\n=== OpenFOAM Job ===")
        result1 = compute.execute(
            service="openfoam-swak4foam",
            command="cp -r $FOAM_TUTORIALS/incompressible/icoFoam/cavity/cavity /workspace/cavity && cd /workspace/cavity && blockMesh && icoFoam && ls -la",
            backend="skypilot",
            workspace=True,
            session_id=session_id,
            cpus=4,
            memory_gb=8,
        )

        if not result1.success:
            print(f"Failed: {result1.error}")
            return 1

        job1_id = result1.output["job_id"]
        job_ids.append(job1_id)
        print(f"Started: {job1_id}")

        if not wait_for_job(compute, job1_id):
            return 1

        # Job 2: ParaView - render
        print("\n=== ParaView Job ===")
        result2 = compute.execute(
            service="paraview",
            command="touch /workspace/cavity/cavity.foam && pvpython -c \"from paraview.simple import *; r=OpenFOAMReader(FileName='/workspace/cavity/cavity.foam'); Show(r); SaveScreenshot('/workspace/result.png')\" && ls -la /workspace/",
            backend="skypilot",
            workspace=True,
            session_id=session_id,
            cpus=4,
            memory_gb=8,
        )

        if not result2.success:
            print(f"Failed: {result2.error}")
            return 1

        job2_id = result2.output["job_id"]
        job_ids.append(job2_id)
        print(f"Started: {job2_id}")

        if not wait_for_job(compute, job2_id):
            return 1

        print("\n=== SUCCESS ===")
        return 0

    finally:
        print("\n=== Cleanup ===")
        for job_id in job_ids:
            cleanup_cluster(job_id)


if __name__ == "__main__":
    sys.exit(main())
