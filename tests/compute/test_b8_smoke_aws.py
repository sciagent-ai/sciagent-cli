"""B8 — OpenFOAM typical_c smoke test on real AWS.

Acceptance bar (v4.1 §2): ``compute_run(service="openfoam-swak4foam-2012",
workspace_source="s3://…", command="bash Allrun")`` reaches COMPLETED
end-to-end via the public ComputeTool API — no hand-written driver.

This test is **PAID**. A single run costs ~$0.10–0.15 on AWS and takes
~10 minutes. CI gates it behind ``RUN_AWS_TESTS=1``; without that env var,
the test is skipped.

To run:

    export RUN_AWS_TESTS=1
    export B8_WORKSPACE_SOURCE=s3://<your-bucket>/<typical_c-case-prefix>
    # Optional overrides:
    export B8_TIMEOUT_SEC=1800     # default 1800
    export B8_CLEANUP=1            # default 1; set 0 to leave cluster up for inspection
    pytest tests/compute/test_b8_smoke_aws.py -v -s

The workspace source must contain a runnable OpenFOAM ``typical_c`` case
(``constant/``, ``system/``, ``Allrun``, etc.). The test invokes
``bash Allrun`` against the mounted workspace.
"""

from __future__ import annotations

import os
import time

import pytest


pytestmark = pytest.mark.skipif(
    os.environ.get("RUN_AWS_TESTS") != "1",
    reason="paid AWS test; set RUN_AWS_TESTS=1 to run",
)


def _required_env(name: str) -> str:
    val = os.environ.get(name)
    if not val:
        pytest.skip(f"set {name} to run this paid AWS test")
    return val


def test_b8_openfoam_typical_c_smoke():
    """Launch the openfoam-swak4foam-2012 service against a real S3 workspace,
    poll for COMPLETED, and assert structured success.

    Cleanup: sky.down is called in the finally block regardless of outcome.
    A failed assertion or timeout still tears down the cluster so a CI
    failure never leaves a billing surprise behind.
    """
    from sciagent.compute.job import JobStatus
    from sciagent.compute.router import ComputeRouter
    from sciagent.tools.atomic.compute import ComputeTool

    workspace_source = _required_env("B8_WORKSPACE_SOURCE")
    timeout_sec = int(os.environ.get("B8_TIMEOUT_SEC", "1800"))
    cleanup_after = os.environ.get("B8_CLEANUP", "1") != "0"

    ComputeTool._shared_session_id = None
    tool = ComputeTool()
    job_id = None
    try:
        result = tool.execute(
            service="openfoam-swak4foam-2012",
            workspace_source=workspace_source,
            command="bash Allrun",
            backend="skypilot",
            background=True,
            timeout_sec=timeout_sec,
            intent={
                "test": "B8",
                "case": "typical_c",
                "service": "openfoam-swak4foam-2012",
            },
            expected_artifacts=[
                "log.icoFoam",
                "postProcessing/probes/0/U",
            ],
        )
        assert result.success, f"launch failed: {result.error}"
        job_id = result.output["job_id"]
        assert job_id, "ComputeTool returned no job_id"
        assert result.output.get("backend") == "skypilot"
        # workspace block must reflect the URI's bucket (not the synthesized one)
        # since workspace_source is an s3:// URI.
        if workspace_source.startswith("s3://"):
            expected_bucket = workspace_source[len("s3://"):].split("/", 1)[0]
            assert result.output["workspace"]["bucket"] == expected_bucket

        # Poll until the job reaches a terminal state. Cap by timeout_sec
        # plus a generous launch overhead margin.
        router = ComputeRouter()
        deadline = time.monotonic() + timeout_sec + 600  # +10 min for provisioning
        last_status = None
        while time.monotonic() < deadline:
            status = router.get_status(job_id)
            last_status = status
            if status.status == JobStatus.COMPLETED:
                break
            if status.status == JobStatus.FAILED:
                pytest.fail(
                    f"job {job_id} FAILED: {status.summary}\n"
                    f"error_preview: {status.error_preview}\n"
                    f"output_file: {status.output_file}"
                )
            time.sleep(30)
        else:
            pytest.fail(
                f"job {job_id} did not reach COMPLETED within "
                f"{timeout_sec + 600}s (last status: {last_status.status if last_status else 'None'})"
            )
    finally:
        if cleanup_after and job_id:
            from sciagent.compute.backends.skypilot import SkyPilotBackend
            try:
                SkyPilotBackend().cleanup(job_id, purge=False)
            except Exception:
                pass


if __name__ == "__main__":
    pytest.main([__file__, "-v", "-s"])
