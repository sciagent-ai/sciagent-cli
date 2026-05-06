"""Unit tests for the per-session Sky Storage workspace (P0.5).

Covers:
- get_or_create_session_workspace caches the sky.Storage object per session_id
  (process-scope) so repeat calls in the same session don't construct twice.
- Storage is constructed with persistent=True and mode=MOUNT — non-negotiable
  for the durable cross-step contract.
- Bucket name is derived from session_id alone (sciagent-workspace-<sid>).
- resolve_workspace_store honors SCIAGENT_WORKSPACE_STORE env, then falls
  back to the first enabled cloud (cloud-aware, never hardcoded to S3).
- compute_run with workspace_source=None auto-mounts /workspace/ on skypilot
  and surfaces workspace_uri in the result.
- Explicit workspace_source override is respected unchanged (auto-mount
  doesn't fire, workspace_uri is None).
- The auto-mount uses kind="durable" so it does NOT steal CWD from the
  image's WORKDIR (only kind="input" mounts are cd-eligible).
"""

from __future__ import annotations

import os
from unittest.mock import MagicMock, patch

import pytest

from sciagent.compute.backends.skypilot import (
    SkyPilotBackend,
    _SESSION_WORKSPACE_CACHE,
    _build_workspace_uri,
    _pick_primary_input_mount,
    _session_workspace_bucket_name,
)
from sciagent.compute.job import StorageMode, StorageMount


@pytest.fixture(autouse=True)
def _clear_cache():
    """Per-session cache is module-global; reset between tests to avoid
    cross-test bleed."""
    _SESSION_WORKSPACE_CACHE.clear()
    yield
    _SESSION_WORKSPACE_CACHE.clear()


# ----- bucket name + URI helpers --------------------------------------------


def test_session_workspace_bucket_name_format():
    assert _session_workspace_bucket_name("abc12345") == "sciagent-workspace-abc12345"


@pytest.mark.parametrize(
    "store,expected",
    [
        ("s3", "s3://sciagent-workspace-abc/"),
        ("gcs", "gs://sciagent-workspace-abc/"),
        ("azure", "az://sciagent-workspace-abc/"),
        ("r2", "r2://sciagent-workspace-abc/"),
        ("oci", "oci://sciagent-workspace-abc/"),
    ],
)
def test_build_workspace_uri_per_store(store, expected):
    assert _build_workspace_uri(store, "abc") == expected


# ----- resolve_workspace_store ----------------------------------------------


def test_resolve_workspace_store_honors_env_var(monkeypatch):
    """SCIAGENT_WORKSPACE_STORE wins over sky check — explicit user choice."""
    backend = SkyPilotBackend()
    monkeypatch.setenv("SCIAGENT_WORKSPACE_STORE", "gcs")
    with patch.object(backend, "get_enabled_store", return_value="s3"):
        assert backend.resolve_workspace_store() == "gcs"


def test_resolve_workspace_store_accepts_uri_scheme_aliases(monkeypatch):
    """User can spell 'gs' / 'az' (URI scheme); backend normalizes to
    Sky's StoreType names ('gcs' / 'azure')."""
    backend = SkyPilotBackend()
    monkeypatch.setenv("SCIAGENT_WORKSPACE_STORE", "gs")
    with patch.object(backend, "get_enabled_store", return_value="s3"):
        assert backend.resolve_workspace_store() == "gcs"
    monkeypatch.setenv("SCIAGENT_WORKSPACE_STORE", "az")
    with patch.object(backend, "get_enabled_store", return_value="s3"):
        assert backend.resolve_workspace_store() == "azure"


def test_resolve_workspace_store_falls_back_to_enabled_cloud(monkeypatch):
    backend = SkyPilotBackend()
    monkeypatch.delenv("SCIAGENT_WORKSPACE_STORE", raising=False)
    with patch.object(backend, "get_enabled_store", return_value="gcs"):
        assert backend.resolve_workspace_store() == "gcs"


# ----- get_or_create_session_workspace --------------------------------------


def test_get_or_create_session_workspace_persistent_and_mount():
    """Sky Storage MUST be constructed persistent=True + mode=MOUNT.
    Non-negotiable: persistent keeps the bucket past cluster teardown,
    MOUNT streams writes through to the object store so cross-step reads
    see them."""
    backend = SkyPilotBackend()
    mock_sky = MagicMock()
    mock_sky.StoreType.S3 = "S3_SENTINEL"
    mock_sky.StorageMode.MOUNT = "MOUNT_SENTINEL"
    backend._sky = mock_sky

    with patch.object(backend, "resolve_workspace_store", return_value="s3"):
        backend.get_or_create_session_workspace("abc12345")

    mock_sky.Storage.assert_called_once()
    _, kwargs = mock_sky.Storage.call_args
    assert kwargs["name"] == "sciagent-workspace-abc12345"
    assert kwargs["persistent"] is True
    assert kwargs["mode"] == "MOUNT_SENTINEL"
    assert kwargs["source"] is None
    assert kwargs["stores"] == ["S3_SENTINEL"]


def test_get_or_create_session_workspace_caches_per_session():
    backend = SkyPilotBackend()
    mock_sky = MagicMock()
    mock_sky.StoreType.S3 = "S3_SENTINEL"
    mock_sky.StorageMode.MOUNT = "MOUNT_SENTINEL"
    backend._sky = mock_sky

    with patch.object(backend, "resolve_workspace_store", return_value="s3"):
        s1 = backend.get_or_create_session_workspace("abc")
        s2 = backend.get_or_create_session_workspace("abc")
    assert s1 is s2
    # Storage constructed exactly once for the same session_id.
    assert mock_sky.Storage.call_count == 1


def test_get_or_create_session_workspace_distinct_per_session():
    backend = SkyPilotBackend()
    mock_sky = MagicMock()
    mock_sky.StoreType.S3 = "S3_SENTINEL"
    mock_sky.StorageMode.MOUNT = "MOUNT_SENTINEL"
    backend._sky = mock_sky

    with patch.object(backend, "resolve_workspace_store", return_value="s3"):
        backend.get_or_create_session_workspace("a")
        backend.get_or_create_session_workspace("b")
    # Two distinct sessions -> two Storage constructions.
    assert mock_sky.Storage.call_count == 2
    names = {c.kwargs["name"] for c in mock_sky.Storage.call_args_list}
    assert names == {"sciagent-workspace-a", "sciagent-workspace-b"}


# ----- build_session_workspace_mount ----------------------------------------


def test_build_session_workspace_mount_kind_durable():
    """Auto-mount must be kind='durable' so the cd picker SKIPS it. /workspace/
    is a data tier, not a code tier — image WORKDIR must stay intact."""
    backend = SkyPilotBackend()
    with patch.object(backend, "resolve_workspace_store", return_value="s3"):
        mount = backend.build_session_workspace_mount("abc12345")
    assert mount.path == "/workspace"
    assert mount.bucket == "sciagent-workspace-abc12345"
    assert mount.store == "s3"
    assert mount.mode is StorageMode.MOUNT
    assert mount.persistent is True
    assert mount.kind == "durable"


def test_pick_primary_input_mount_skips_durable():
    """Sanity: a durable auto-mount alongside an explicit input mount must
    NOT win the cd slot — the explicit input is the cd target."""
    durable = StorageMount(
        path="/workspace", bucket="sciagent-workspace-x", store="s3",
        kind="durable",
    )
    explicit = StorageMount(
        path="/data/case", bucket="case-bucket", store="s3", kind="input",
    )
    picked = _pick_primary_input_mount([durable, explicit])
    assert picked is explicit


def test_pick_primary_input_mount_durable_only_returns_none():
    """When only a durable mount is attached (no explicit input), the picker
    returns None so the image's WORKDIR is honored."""
    durable = StorageMount(
        path="/workspace", bucket="sciagent-workspace-x", store="s3",
        kind="durable",
    )
    output = StorageMount(
        path="/outputs", bucket="sciagent-workspace-x", store="s3",
        kind="output",
    )
    assert _pick_primary_input_mount([durable, output]) is None


# ----- ComputeTool.execute auto-mount plumbing ------------------------------


def _make_fake_router(monkeypatch=None):
    """Common router stub for the compute_run plumbing tests."""
    fake_skypilot = MagicMock()
    fake_skypilot.name = "skypilot"
    fake_skypilot.build_outputs_mount.return_value = StorageMount(
        path="/outputs",
        bucket="sciagent-workspace-test-sess",
        store="s3",
        mode=StorageMode.MOUNT,
        source=None,
        persistent=True,
        kind="output",
    )
    fake_skypilot.build_input_mounts.return_value = []
    fake_skypilot.build_session_workspace_mount.return_value = StorageMount(
        path="/workspace",
        bucket="sciagent-workspace-test-sess",
        store="s3",
        mode=StorageMode.MOUNT,
        source=None,
        persistent=True,
        kind="durable",
    )
    fake_skypilot.run.return_value = ("sciagent-job-xyz", None)

    fake_router = MagicMock()
    fake_router.list_backends.return_value = ["skypilot"]
    fake_router._backends = {"skypilot": fake_skypilot}
    fake_router.select.return_value = (fake_skypilot, "Using skypilot")
    fake_router.estimate_cost.return_value = {"estimated_hourly": 0.10}
    return fake_router, fake_skypilot


def test_compute_run_auto_mounts_workspace_when_source_none():
    """workspace_source=None on skypilot -> auto-mount /workspace/ AND
    surface workspace_uri at the top of the result."""
    from sciagent.tools.atomic.compute import ComputeTool

    ComputeTool._shared_session_id = "test-sess"
    tool = ComputeTool()

    fake_router, fake_skypilot = _make_fake_router()
    with patch.object(tool, "_get_router", return_value=fake_router):
        result = tool.execute(
            command="echo hi",
            image="alpine",
            backend="skypilot",
        )

    assert result.success is True, result.error
    fake_skypilot.build_session_workspace_mount.assert_called_once()
    # workspace_uri surfaced at the top level.
    assert result.output["workspace_uri"] == "s3://sciagent-workspace-test-sess/"
    # And the durable mount appears in the workspace-info breakdown.
    assert result.output["workspace"]["durable"][0]["path"] == "/workspace"


def test_compute_run_explicit_workspace_source_skips_auto_mount():
    """Explicit workspace_source must NOT trigger auto-mount; workspace_uri
    stays None so the LLM can't accidentally read it."""
    from sciagent.tools.atomic.compute import ComputeTool

    ComputeTool._shared_session_id = "test-sess"
    tool = ComputeTool()

    fake_router, fake_skypilot = _make_fake_router()
    fake_skypilot.build_input_mounts.return_value = [
        StorageMount(
            path="/workspace",
            bucket="my-paper-data",
            store="s3",
            mode=StorageMode.MOUNT,
            source="s3://my-paper-data/case/",
            persistent=True,
            kind="input",
        )
    ]

    with patch.object(tool, "_get_router", return_value=fake_router):
        result = tool.execute(
            command="cd /workspace && bash Allrun",
            image="alpine",
            workspace_source="s3://my-paper-data/case/",
            backend="skypilot",
        )

    assert result.success is True, result.error
    fake_skypilot.build_session_workspace_mount.assert_not_called()
    assert result.output["workspace_uri"] is None


def test_compute_run_local_backend_no_auto_mount():
    """Auto-mount is skypilot-only; local Docker has its own filesystem
    semantics and never gets a workspace mount."""
    from sciagent.tools.atomic.compute import ComputeTool

    ComputeTool._shared_session_id = "test-sess"
    tool = ComputeTool()

    fake_local = MagicMock()
    fake_local.name = "local"
    fake_local.run.return_value = "local-job-1"

    fake_router = MagicMock()
    fake_router.list_backends.return_value = ["local"]
    fake_router._backends = {"local": fake_local}
    fake_router.select.return_value = (fake_local, "local")
    fake_router.estimate_cost.return_value = {"estimated_hourly": 0.0}

    with patch.object(tool, "_get_router", return_value=fake_router):
        result = tool.execute(
            command="echo hi",
            image="alpine",
            backend="local",
        )

    assert result.success is True
    assert result.output.get("workspace_uri") is None


def test_compute_run_auto_mount_allows_workspace_in_command():
    """The path-contract validator must allow /workspace/ when auto-mount
    is active — otherwise the natural `python ... /workspace/run/data` is
    rejected by the validator that doesn't see an explicit declaration."""
    from sciagent.tools.atomic.compute import ComputeTool

    ComputeTool._shared_session_id = "test-sess"
    tool = ComputeTool()

    fake_router, _ = _make_fake_router()
    with patch.object(tool, "_get_router", return_value=fake_router):
        result = tool.execute(
            command="python -c 'open(\"/workspace/run/data.txt\",\"w\").write(\"x\")'",
            image="alpine",
            backend="skypilot",
        )

    assert result.success is True, result.error


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
