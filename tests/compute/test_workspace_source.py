"""Mocked tests for B5: workspace_source plumbing.

Covers:
- _parse_cloud_uri returns the right (store, bucket) for s3/gs/r2 URIs and
  None for local paths.
- get_workspace_mount with a cloud-URI workspace_source uses the URI's bucket
  and store, and forwards `source` and `persistent=True` onto the StorageMount.
- get_workspace_mount with no workspace_source falls back to the
  session-derived bucket name.
- _build_storage_mounts forwards `persistent` to sky.Storage so workspace
  buckets survive cluster teardown.
- ComputeTool.execute(workspace_source=…) auto-enables the workspace mount
  on skypilot and plumbs the value through to get_workspace_mount.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from sciagent.compute.backends.skypilot import (
    SkyPilotBackend,
    _parse_cloud_uri,
)
from sciagent.compute.job import StorageMode, StorageMount


# ----- _parse_cloud_uri ------------------------------------------------------


@pytest.mark.parametrize(
    "uri,expected",
    [
        ("s3://my-bucket", ("s3", "my-bucket")),
        ("s3://my-bucket/case/foo", ("s3", "my-bucket")),
        ("gs://gcp-bucket/sub", ("gcs", "gcp-bucket")),
        ("r2://r2-bucket", ("r2", "r2-bucket")),
        ("/local/path", (None, None)),
        ("./relative", (None, None)),
        ("", (None, None)),
        (None, (None, None)),
        ("s3://", (None, None)),  # missing bucket
        ("https://example.com/x", (None, None)),  # ambiguous, not handled
    ],
)
def test_parse_cloud_uri(uri, expected):
    assert _parse_cloud_uri(uri) == expected


# ----- get_workspace_mount ---------------------------------------------------


def test_get_workspace_mount_with_s3_uri_uses_uri_bucket():
    """B5: when workspace_source is s3://bucket/..., the StorageMount must
    target that exact bucket — not the synthesized sciagent-workspace-{id}
    name. Otherwise sky.Storage will try to upload into the wrong bucket."""
    backend = SkyPilotBackend()

    mount = backend.get_workspace_mount(
        session_id="abc12345",
        workspace_source="s3://my-paper-data/case-typical-c",
    )

    assert isinstance(mount, StorageMount)
    assert mount.path == "/workspace"
    assert mount.bucket == "my-paper-data"
    assert mount.store == "s3"
    assert mount.mode is StorageMode.MOUNT
    assert mount.source == "s3://my-paper-data/case-typical-c"
    assert mount.persistent is True


def test_get_workspace_mount_no_source_falls_back_to_session_bucket():
    backend = SkyPilotBackend()
    # Avoid hitting the cloud-detection path.
    with patch.object(backend, "get_enabled_store", return_value="s3"):
        mount = backend.get_workspace_mount(session_id="abc12345")

    assert mount.bucket == "sciagent-workspace-abc12345"
    assert mount.store == "s3"
    assert mount.source is None
    assert mount.persistent is True


def test_get_workspace_mount_local_source_keeps_session_bucket():
    """A local path as workspace_source still uses the session-derived bucket
    name; sky syncs the local tree into it on launch."""
    backend = SkyPilotBackend()
    with patch.object(backend, "get_enabled_store", return_value="gcs"):
        mount = backend.get_workspace_mount(
            session_id="abc12345",
            workspace_source="/tmp/local-case",
        )

    assert mount.bucket == "sciagent-workspace-abc12345"
    assert mount.store == "gcs"
    assert mount.source == "/tmp/local-case"
    assert mount.persistent is True


# ----- _build_storage_mounts -------------------------------------------------


def test_build_storage_mounts_forwards_persistent_to_sky_storage():
    """B5: sky.Storage must be constructed with persistent=True so the
    workspace bucket survives cluster teardown. Catches accidental drops of
    the kwarg from the constructor call."""
    backend = SkyPilotBackend()
    mock_sky = MagicMock()
    mock_sky.StorageMode.MOUNT = "MOUNT_SENTINEL"
    mock_sky.StoreType.S3 = "S3_SENTINEL"
    backend._sky = mock_sky

    mount = StorageMount(
        path="/workspace",
        bucket="my-paper-data",
        store="s3",
        mode=StorageMode.MOUNT,
        source="s3://my-paper-data/case-typical-c",
        persistent=True,
    )

    backend._build_storage_mounts([mount])

    mock_sky.Storage.assert_called_once()
    _, kwargs = mock_sky.Storage.call_args
    assert kwargs["name"] == "my-paper-data"
    assert kwargs["source"] == "s3://my-paper-data/case-typical-c"
    assert kwargs["persistent"] is True
    assert kwargs["mode"] == "MOUNT_SENTINEL"
    assert kwargs["stores"] == ["S3_SENTINEL"]


def test_build_storage_mounts_passes_persistent_false_when_set():
    backend = SkyPilotBackend()
    mock_sky = MagicMock()
    mock_sky.StorageMode.MOUNT = "MOUNT_SENTINEL"
    mock_sky.StoreType.S3 = "S3_SENTINEL"
    backend._sky = mock_sky

    mount = StorageMount(
        path="/workspace",
        bucket="ephemeral",
        store="s3",
        persistent=False,
    )
    backend._build_storage_mounts([mount])
    _, kwargs = mock_sky.Storage.call_args
    assert kwargs["persistent"] is False


# ----- ComputeTool.execute plumbing ------------------------------------------


def test_compute_tool_workspace_source_auto_enables_mount_on_skypilot():
    """compute_run(service=…, workspace_source='s3://…', command=…) on
    skypilot must build an input mount at /workspace (back-compat for the
    string form) and an always-on output mount, then surface both in the
    result payload under workspace.inputs / workspace.outputs."""
    from sciagent.tools.atomic.compute import ComputeTool

    ComputeTool._shared_session_id = None
    tool = ComputeTool()

    fake_input_mount = StorageMount(
        path="/workspace",
        bucket="my-paper-data",
        store="s3",
        mode=StorageMode.MOUNT,
        source="s3://my-paper-data/case-typical-c",
        persistent=True,
        kind="input",
    )
    fake_output_mount = StorageMount(
        path="/outputs",
        bucket="sciagent-workspace-test-sess",
        store="s3",
        mode=StorageMode.MOUNT,
        source=None,
        persistent=True,
        kind="output",
    )

    # Stand in for the real router/backend so no sky calls happen.
    fake_skypilot = MagicMock()
    fake_skypilot.name = "skypilot"
    fake_skypilot.build_outputs_mount.return_value = fake_output_mount
    fake_skypilot.build_input_mounts.return_value = [fake_input_mount]
    fake_skypilot.run.return_value = ("sciagent-job-xyz", None)

    fake_router = MagicMock()
    fake_router.list_backends.return_value = ["skypilot"]
    fake_router._backends = {"skypilot": fake_skypilot}
    fake_router.select.return_value = (fake_skypilot, "Using requested backend: skypilot")
    fake_router.estimate_cost.return_value = {"estimated_hourly": 0.10}
    fake_router.run.return_value = "sciagent-job-xyz"

    with patch.object(tool, "_get_router", return_value=fake_router):
        result = tool.execute(
            command="cd /workspace && bash Allrun",
            service="openfoam-swak4foam-2012",
            workspace_source="s3://my-paper-data/case-typical-c",
            backend="skypilot",
        )

    assert result.success is True, result.error

    # Both new mount methods called; workspace_source reached build_input_mounts
    # in normalized list-of-dicts form.
    fake_skypilot.build_outputs_mount.assert_called_once()
    fake_skypilot.build_input_mounts.assert_called_once()
    inputs_arg = fake_skypilot.build_input_mounts.call_args.args[0]
    assert inputs_arg == [
        {"path": "/workspace", "source": "s3://my-paper-data/case-typical-c"}
    ]

    # Result reports both inputs and outputs separately under the workspace key.
    workspace = result.output["workspace"]
    assert workspace["inputs"][0]["path"] == "/workspace"
    assert workspace["inputs"][0]["bucket"] == "my-paper-data"
    assert workspace["inputs"][0]["source"] == "s3://my-paper-data/case-typical-c"
    assert workspace["outputs"][0]["path"] == "/outputs"
    assert workspace["outputs_dir_env"] == "$OUTPUTS_DIR"


def test_compute_tool_no_workspace_source_no_mount_on_local_backend():
    """Sanity guard: workspace_source is skypilot-only; passing it with a
    local-routed CPU job must not crash (mount silently not attached)."""
    from sciagent.tools.atomic.compute import ComputeTool

    ComputeTool._shared_session_id = None
    tool = ComputeTool()

    fake_local = MagicMock()
    fake_local.name = "local"
    fake_local.run.return_value = "local-job-1"

    fake_router = MagicMock()
    fake_router.list_backends.return_value = ["local"]
    fake_router._backends = {"local": fake_local}
    fake_router.select.return_value = (fake_local, "Using local Docker")
    fake_router.estimate_cost.return_value = {"estimated_hourly": 0.0}
    fake_router.run.return_value = "local-job-1"

    with patch.object(tool, "_get_router", return_value=fake_router):
        result = tool.execute(
            command="echo hi",
            image="alpine",
            workspace_source="s3://something/foo",
            backend="local",
        )

    assert result.success is True
    assert "workspace" not in result.output


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
