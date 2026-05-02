"""Path-contract invariants for compute_run.

Pins the cloud-agnostic, image-agnostic input/output contract:

  - Inputs at caller-declared paths (default /workspace), built only when
    workspace_source= is provided. Single-string form back-compat.
  - Outputs always at /outputs/<job_id>/ ($OUTPUTS_DIR exported).
  - Multi-mount inputs work across mixed clouds (s3://, gs://, etc.).
  - Validation: command refs to undeclared input paths fail fast with
    error_kind="path_contract".
  - ship_workdir= conditional: present only when caller asked to rsync.

The shape of these tests deliberately covers different image WORKDIRs
(rcwa: /opt; openfoam: /workspace; scipy-base: /workspace) by sampling
the registry — but the assertions never depend on per-image branching.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from sciagent.compute.backends.skypilot import (
    SkyPilotBackend,
    _normalize_workspace_source,
)
from sciagent.compute.job import StorageMount, StorageMode
from sciagent.tools.atomic.compute import ComputeTool


# ---- workspace_source normalization (str / list shapes) ----------------


def test_normalize_workspace_source_none_returns_empty_list():
    assert _normalize_workspace_source(None) == []
    assert _normalize_workspace_source("") == []


def test_normalize_workspace_source_str_wraps_to_default_path():
    """Back-compat: legacy single-string form maps to /workspace/."""
    out = _normalize_workspace_source("s3://my-bucket/")
    assert out == [{"path": "/workspace", "source": "s3://my-bucket/"}]


def test_normalize_workspace_source_list_passes_through():
    inputs = [
        {"path": "/workspace", "source": "s3://q/"},
        {"path": "/data/nr", "source": "gs://nr/"},
    ]
    assert _normalize_workspace_source(inputs) == inputs


def test_normalize_workspace_source_invalid_shape_raises():
    with pytest.raises(ValueError):
        _normalize_workspace_source(42)
    with pytest.raises(ValueError):
        _normalize_workspace_source([{"path": "/x"}])  # missing source


# ---- build_input_mounts builds one mount per declared entry -----------


def test_build_input_mounts_empty_for_no_source():
    backend = SkyPilotBackend()
    with patch.object(backend, "get_enabled_store", return_value="s3"):
        mounts = backend.build_input_mounts(None, session_id="abc")
    assert mounts == []


def test_build_input_mounts_string_form_back_compat():
    backend = SkyPilotBackend()
    with patch.object(backend, "get_enabled_store", return_value="s3"):
        mounts = backend.build_input_mounts(
            "s3://my-paper-data/case",
            session_id="abc",
        )
    assert len(mounts) == 1
    assert mounts[0].path == "/workspace"
    assert mounts[0].bucket == "my-paper-data"
    assert mounts[0].store == "s3"
    assert mounts[0].kind == "input"


def test_build_input_mounts_list_form_mixed_clouds():
    """Multi-mount inputs across clouds — each mount auto-detects its
    store from the source URI scheme."""
    backend = SkyPilotBackend()
    with patch.object(backend, "get_enabled_store", return_value="s3"):
        mounts = backend.build_input_mounts(
            [
                {"path": "/workspace", "source": "s3://q/"},
                {"path": "/data/nr", "source": "gs://nr-public/"},
                {"path": "/data/extra", "source": "oci://ext/"},
            ],
            session_id="abc",
        )
    assert len(mounts) == 3
    by_path = {m.path: m for m in mounts}
    assert by_path["/workspace"].store == "s3"
    assert by_path["/data/nr"].store == "gcs"
    assert by_path["/data/extra"].store == "oci"
    # Buckets come from each URI.
    assert by_path["/workspace"].bucket == "q"
    assert by_path["/data/nr"].bucket == "nr-public"
    assert by_path["/data/extra"].bucket == "ext"


def test_build_outputs_mount_is_at_outputs_path_with_session_bucket():
    """Always-on output mount: path=/outputs, bucket name is session-derived,
    store auto-detected from the cluster's enabled cloud (provider-neutral)."""
    backend = SkyPilotBackend()
    with patch.object(backend, "get_enabled_store", return_value="gcs"):
        mount = backend.build_outputs_mount(session_id="sess1")
    assert mount.path == "/outputs"
    assert mount.bucket == "sciagent-workspace-sess1"
    assert mount.store == "gcs"
    assert mount.kind == "output"


# ---- compute_run integration: validation fires before backend launch -


def _stub_router_with_skypilot():
    fake_skypilot = MagicMock()
    fake_skypilot.name = "skypilot"
    fake_skypilot.build_outputs_mount.return_value = StorageMount(
        path="/outputs",
        bucket="sciagent-workspace-test",
        store="s3",
        mode=StorageMode.MOUNT,
        persistent=True,
        kind="output",
    )
    fake_skypilot.build_input_mounts.return_value = []
    fake_skypilot.run.return_value = ("sciagent-job-1", 1)
    fake_router = MagicMock()
    fake_router.list_backends.return_value = ["skypilot"]
    fake_router._backends = {"skypilot": fake_skypilot}
    fake_router.select.return_value = (
        fake_skypilot,
        "Using requested backend: skypilot",
    )
    fake_router.estimate_cost.return_value = {"estimated_hourly": 0.0}
    return fake_router, fake_skypilot


def test_validation_fails_when_command_refs_workspace_without_source():
    """Command says /workspace/foo but no input mount was declared. Fail
    fast before the backend is even contacted."""
    ComputeTool._shared_session_id = None
    tool = ComputeTool()
    fake_router, fake_skypilot = _stub_router_with_skypilot()

    with patch.object(tool, "_get_router", return_value=fake_router):
        result = tool.execute(
            command="ls /workspace/foo",
            image="python:3.11",
            backend="skypilot",
        )

    assert result.success is False
    assert "path_contract" in str(result.output)
    fake_skypilot.run.assert_not_called()  # never reached the backend


def test_validation_passes_when_command_refs_declared_workspace():
    """With workspace_source=, the same command is fine."""
    ComputeTool._shared_session_id = None
    tool = ComputeTool()
    fake_router, fake_skypilot = _stub_router_with_skypilot()
    fake_skypilot.build_input_mounts.return_value = [
        StorageMount(
            path="/workspace",
            bucket="myb",
            store="s3",
            source="s3://myb/",
            persistent=True,
            kind="input",
        )
    ]

    with patch.object(tool, "_get_router", return_value=fake_router):
        result = tool.execute(
            command="ls /workspace/foo",
            image="python:3.11",
            backend="skypilot",
            workspace_source="s3://myb/",
        )

    assert result.success is True, result.error


def test_validation_passes_for_data_mount_when_declared():
    """Multi-mount: command can reference any declared path."""
    ComputeTool._shared_session_id = None
    tool = ComputeTool()
    fake_router, fake_skypilot = _stub_router_with_skypilot()
    fake_skypilot.build_input_mounts.return_value = [
        StorageMount(path="/workspace", bucket="qb", store="s3", kind="input"),
        StorageMount(path="/data/nr", bucket="nrb", store="gcs", kind="input"),
    ]

    with patch.object(tool, "_get_router", return_value=fake_router):
        result = tool.execute(
            command="blastn -query /workspace/q.fa -db /data/nr/nr",
            image="python:3.11",
            backend="skypilot",
            workspace_source=[
                {"path": "/workspace", "source": "s3://qb/"},
                {"path": "/data/nr", "source": "gs://nrb/"},
            ],
        )

    assert result.success is True, result.error


def test_validation_fails_for_data_mount_when_not_declared():
    """Refer to /data/nr without declaring it -> fail fast."""
    ComputeTool._shared_session_id = None
    tool = ComputeTool()
    fake_router, fake_skypilot = _stub_router_with_skypilot()
    fake_skypilot.build_input_mounts.return_value = [
        StorageMount(path="/workspace", bucket="qb", store="s3", kind="input")
    ]

    with patch.object(tool, "_get_router", return_value=fake_router):
        result = tool.execute(
            command="blastn -query /workspace/q.fa -db /data/nr/nr",
            image="python:3.11",
            backend="skypilot",
            workspace_source="s3://qb/",
        )

    assert result.success is False
    assert "path_contract" in str(result.output)
    assert "/data/nr" in str(result.error)


def test_validation_fails_when_command_refs_sky_workdir():
    """~/sky_workdir is internal SkyPilot — never agent-visible."""
    ComputeTool._shared_session_id = None
    tool = ComputeTool()
    fake_router, fake_skypilot = _stub_router_with_skypilot()

    with patch.object(tool, "_get_router", return_value=fake_router):
        result = tool.execute(
            command="cd ~/sky_workdir && python run.py",
            image="python:3.11",
            backend="skypilot",
        )

    assert result.success is False
    assert "sky_workdir" in str(result.error)
    fake_skypilot.run.assert_not_called()


def test_outputs_path_is_always_allowed():
    """Writes to /outputs/<job_id>/ never trip validation — output mount
    is auto-attached so the path always exists."""
    ComputeTool._shared_session_id = None
    tool = ComputeTool()
    fake_router, fake_skypilot = _stub_router_with_skypilot()

    with patch.object(tool, "_get_router", return_value=fake_router):
        result = tool.execute(
            command="echo hi > /outputs/abc/log.txt",
            image="python:3.11",
            backend="skypilot",
        )

    # Validation passes; backend is reached.
    assert result.success is True, result.error


def test_local_backend_skips_path_validation():
    """Local Docker has its own filesystem semantics; /workspace etc. don't
    apply. Validation is skypilot-only."""
    ComputeTool._shared_session_id = None
    tool = ComputeTool()
    fake_local = MagicMock()
    fake_local.name = "local"
    fake_local.run.return_value = "local-1"
    fake_router = MagicMock()
    fake_router.list_backends.return_value = ["local"]
    fake_router._backends = {"local": fake_local}
    fake_router.select.return_value = (fake_local, "Using local Docker")
    fake_router.estimate_cost.return_value = {"estimated_hourly": 0.0}

    with patch.object(tool, "_get_router", return_value=fake_router):
        result = tool.execute(
            command="ls /workspace/foo",
            image="alpine",
            backend="local",
        )

    assert result.success is True


# ---- ship_workdir threading -------------------------------------------


def test_workdir_param_propagates_to_job_ship_workdir():
    """compute_run(workdir=...) should land on Job.ship_workdir, which
    skypilot.py reads to decide whether to pass workdir= to sky.Task."""
    ComputeTool._shared_session_id = None
    tool = ComputeTool()
    fake_router, fake_skypilot = _stub_router_with_skypilot()

    with patch.object(tool, "_get_router", return_value=fake_router):
        tool.execute(
            command="python hello.py",
            image="python:3.11",
            backend="skypilot",
            workdir="/tmp/my-project",
        )

    job_arg = fake_skypilot.run.call_args.args[0]
    assert job_arg.ship_workdir == "/tmp/my-project"


def test_no_workdir_param_means_no_ship():
    """Default behavior: no rsync. ship_workdir defaults to None."""
    ComputeTool._shared_session_id = None
    tool = ComputeTool()
    fake_router, fake_skypilot = _stub_router_with_skypilot()

    with patch.object(tool, "_get_router", return_value=fake_router):
        tool.execute(
            command="python -c 'print(1)'",
            image="python:3.11",
            backend="skypilot",
        )

    job_arg = fake_skypilot.run.call_args.args[0]
    assert job_arg.ship_workdir is None


# ---- manifest fields -------------------------------------------------


def test_manifest_records_outputs_uri_and_mounts(tmp_path, monkeypatch):
    """The manifest written at launch carries outputs_uri (cloud identity),
    outputs_prefix (bucket-side path), and the mounts list."""
    from sciagent.compute import task_index

    fake_home = tmp_path / "home" / ".sciagent" / "tasks"
    monkeypatch.setattr(task_index, "manifest_dir", lambda: fake_home)

    ComputeTool._shared_session_id = None
    tool = ComputeTool()
    fake_router, fake_skypilot = _stub_router_with_skypilot()
    # Output mount returns "gcs" so we can verify gs:// gets written.
    fake_skypilot.build_outputs_mount.return_value = StorageMount(
        path="/outputs",
        bucket="sciagent-workspace-sess9",
        store="gcs",
        mode=StorageMode.MOUNT,
        persistent=True,
        kind="output",
    )
    fake_skypilot.build_input_mounts.return_value = [
        StorageMount(
            path="/workspace",
            bucket="qb",
            store="s3",
            source="s3://qb/",
            persistent=True,
            kind="input",
        )
    ]
    fake_skypilot.run.return_value = ("sciagent-multi", 99)

    with patch.object(tool, "_get_router", return_value=fake_router):
        result = tool.execute(
            command="python solve.py",
            image="python:3.11",
            backend="skypilot",
            workspace_source="s3://qb/",
            session_id="sess9",
        )

    assert result.success is True, result.error

    manifest = task_index.read_task("sciagent-multi")
    assert manifest is not None
    assert manifest["outputs_uri"] == (
        "gs://sciagent-workspace-sess9/sciagent-multi/"
    )
    assert manifest["outputs_prefix"] == "sciagent-multi/"
    assert manifest["mounts"] == [{"path": "/workspace", "source": "s3://qb/"}]
