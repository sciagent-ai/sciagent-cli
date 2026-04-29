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
import subprocess
import time
from pathlib import Path

import pytest


pytestmark = pytest.mark.skipif(
    os.environ.get("RUN_AWS_TESTS") != "1",
    reason="paid AWS test; set RUN_AWS_TESTS=1 to run",
)


# Bytes the bucket must hold for B8 to be a smoke run (~$0.10-0.15, ~10 min)
# rather than the production 9-hour, ~$3 case. M0 follow-up #4: the canonical
# typical_c controlDict (endTime 180.1, writeInterval 30) lives outside the
# repo; an unrelated re-upload of the case used to silently flip B8 into the
# expensive shape. The fixture is the smoke variant (endTime 0.05,
# writeInterval 0.05), uploaded unconditionally below so no out-of-band sync
# can drift it back.
_FIXTURE_CONTROLDICT = (
    Path(__file__).parent.parent / "fixtures" / "b8_typical_c_smoke" / "system" / "controlDict"
)


def _required_env(name: str) -> str:
    val = os.environ.get(name)
    if not val:
        pytest.skip(f"set {name} to run this paid AWS test")
    return val


def _ensure_smoke_controldict(workspace_source: str) -> None:
    """Overwrite ``<workspace_source>/system/controlDict`` with the smoke fixture.

    Only runs when ``workspace_source`` is an ``s3://`` URI (the only shape B8
    is documented to take). Skips the test cleanly if the AWS CLI is missing
    or the upload fails — without the smoke variant, B8 would silently turn
    into the 9-hour case it was designed to gate against.
    """
    if not workspace_source.startswith("s3://"):
        pytest.skip(
            "B8 fixture upload only handles s3:// workspace sources; "
            f"got {workspace_source!r}"
        )
    if not _FIXTURE_CONTROLDICT.is_file():
        pytest.fail(f"missing B8 fixture: {_FIXTURE_CONTROLDICT}")

    target = workspace_source.rstrip("/") + "/system/controlDict"
    try:
        subprocess.run(
            ["aws", "s3", "cp", str(_FIXTURE_CONTROLDICT), target],
            check=True,
            capture_output=True,
            text=True,
        )
    except FileNotFoundError:
        pytest.skip("aws CLI not on PATH; required to upload B8 smoke controlDict")
    except subprocess.CalledProcessError as exc:
        pytest.fail(
            f"failed to upload smoke controlDict to {target}: "
            f"rc={exc.returncode} stderr={exc.stderr}"
        )


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

    # Force the bucket to hold the smoke controlDict before launching. The
    # rest of the typical_c case can be whatever the bucket already has —
    # endTime / writeInterval are what determine whether this is a $0.10
    # smoke or a $3 production run.
    _ensure_smoke_controldict(workspace_source)

    ComputeTool._shared_session_id = None
    tool = ComputeTool()
    job_id = None
    try:
        # typical_c's Allrun runs NP=8 MPI ranks. The registry's `extends:`
        # chain isn't honored by _get_service_resources today (a known
        # M0-out-of-scope shortcoming), so we override cpus/memory_gb here
        # to give MPI an instance it isn't fighting against. c6i.2xlarge
        # (8 vCPUs / 16 GB) at ~$0.34/hr fits the ~$0.10-0.15 B8 budget.
        # `cd /workspace && bash Allrun` instead of bare `bash Allrun`:
        # SkyPilotBackend._build_task does not yet honor the registry's
        # `workdir:` field, so the task starts in sky's default CWD (the
        # user home on the cluster), not at the mount path. Captured as
        # an M0 follow-up below; the cd-prefix is the in-flight workaround
        # so this milestone closes.
        result = tool.execute(
            service="openfoam-swak4foam-2012",
            workspace_source=workspace_source,
            command="cd /workspace && bash Allrun",
            backend="skypilot",
            background=True,
            cpus=8,
            memory_gb=16,
            timeout_sec=timeout_sec,
            intent={
                "test": "B8",
                "case": "typical_c",
                "service": "openfoam-swak4foam-2012",
            },
            expected_artifacts=[
                "logs/EXIT_CODE",
                "logs/10_solver.log",
                "postProcessing",
            ],
        )
        # On a launch failure, ComputeTool surfaces the would-be cluster
        # name in output["job_id"] so we can still clean up.
        if result.output and result.output.get("job_id"):
            job_id = result.output["job_id"]
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
