"""cluster_manifest module — best-effort write/read/list/delete.

This is a small module but it's load-bearing for compute_cluster(action=
'status')'s enriched response. Pin its semantics so a future refactor
can't silently break the merge of Sky-side state with sciagent-side
context.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from sciagent.compute.cluster_manifest import (
    cache_job_log,
    delete_cluster,
    list_clusters,
    read_cached_job_log,
    read_cluster,
    write_cluster,
)


@pytest.fixture
def isolated_home(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    return tmp_path


def test_write_then_read_roundtrip(isolated_home):
    write_cluster(
        "cl1",
        autostop_minutes=30,
        session_id="sess",
        service="openfoam",
        last_job_id=1,
    )
    rec = read_cluster("cl1")
    assert rec is not None
    assert rec["cluster_name"] == "cl1"
    assert rec["autostop_minutes"] == 30
    assert rec["service"] == "openfoam"
    assert rec["session_id"] == "sess"
    assert rec["last_job_ids"] == [1]


def test_repeated_writes_preserve_created_at_and_dedupe_job_ids(isolated_home):
    """A cluster's first launch sets created_at; subsequent updates must
    NOT clobber it (we use it for "cluster has been UP for N minutes"
    enrichment). last_job_ids should dedupe + cap at 20."""
    write_cluster("cl", session_id="s", last_job_id=1)
    first = read_cluster("cl")
    assert first is not None
    created = first["created_at"]

    write_cluster("cl", last_job_id=2)
    write_cluster("cl", last_job_id=1)  # dupe — should reorder, not duplicate
    write_cluster("cl", last_job_id=3)

    second = read_cluster("cl")
    assert second["created_at"] == created
    assert second["last_job_ids"] == [2, 1, 3]


def test_partial_update_doesnt_clobber_existing_fields(isolated_home):
    """A status-update call (no service / no autostop_minutes) must not
    erase those fields from the manifest. write_cluster only overwrites
    fields explicitly given a non-None value."""
    write_cluster("cl", autostop_minutes=30, service="openfoam", session_id="s")
    write_cluster("cl", last_job_id=5)  # partial update

    rec = read_cluster("cl")
    assert rec["autostop_minutes"] == 30
    assert rec["service"] == "openfoam"
    assert rec["session_id"] == "s"


def test_read_missing_returns_none(isolated_home):
    assert read_cluster("does-not-exist") is None


def test_delete_removes_manifest(isolated_home):
    write_cluster("cl", session_id="s")
    assert read_cluster("cl") is not None
    assert delete_cluster("cl") is True
    assert read_cluster("cl") is None


def test_list_filters_by_session_id(isolated_home):
    write_cluster("a", session_id="s1")
    write_cluster("b", session_id="s2")
    write_cluster("c", session_id="s1")

    s1_clusters = list_clusters(session_id="s1")
    names = {r["cluster_name"] for r in s1_clusters}
    assert names == {"a", "c"}


def test_list_returns_empty_when_dir_missing(isolated_home):
    """No clusters ever created → no manifest dir → list returns empty,
    not raises."""
    out = list_clusters()
    assert out == []


def test_write_failure_swallowed_silently(isolated_home, monkeypatch):
    """The manifest is best-effort. If the disk is full or the path is
    unwriteable, write_cluster must NOT raise — the cluster is up on Sky
    and a manifest write failure can't break the launch path."""
    def _boom(*a, **kw):
        raise OSError("disk full")

    monkeypatch.setattr("pathlib.Path.write_text", _boom)
    # Must not raise
    write_cluster("cl", session_id="s")


# ---------- log cache ----------


def test_cache_job_log_roundtrip(isolated_home):
    """A cached log can be read back verbatim while shorter than max_lines."""
    log = "line a\nline b\nline c"
    assert cache_job_log("cl", 7, log) is True
    cached = read_cached_job_log("cl", 7)
    assert cached == log


def test_cache_job_log_truncates_to_max_lines(isolated_home):
    """Logs longer than max_lines keep only the trailing N lines —
    forensics cares about what happened LAST, not the boilerplate prologue."""
    lines = [f"line {i}" for i in range(50)]
    big = "\n".join(lines)
    assert cache_job_log("cl", 1, big, max_lines=10) is True
    cached = read_cached_job_log("cl", 1)
    assert cached is not None
    assert cached.splitlines() == lines[-10:]


def test_read_cached_job_log_missing_returns_none(isolated_home):
    """No cache for the (cluster, job_id) pair → None, not raises."""
    assert read_cached_job_log("never-cached", 99) is None


def test_cache_job_log_per_cluster_per_job_id(isolated_home):
    """Two different cluster_job_ids on the same cluster are independent —
    forensics on job 1 must not be clobbered by a later cache of job 2."""
    cache_job_log("cl", 1, "log for job 1")
    cache_job_log("cl", 2, "log for job 2")
    assert read_cached_job_log("cl", 1) == "log for job 1"
    assert read_cached_job_log("cl", 2) == "log for job 2"


def test_cache_job_log_failure_swallowed(isolated_home, monkeypatch):
    """The cache is best-effort like the manifest — disk-full / unwriteable
    must not raise, just return False."""
    def _boom(*a, **kw):
        raise OSError("disk full")

    monkeypatch.setattr("pathlib.Path.write_text", _boom)
    assert cache_job_log("cl", 1, "anything") is False
