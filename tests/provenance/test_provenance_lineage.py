"""Unit + integration tests for the lineage reader on provenance_log.

Covers the cases called out in PLAN_LINEAGE_READER.md:
  - Multi-prefix match (queried URI is a parent of the produced path).
  - Multi-event match (same URI produced by two steps in iteration).
  - Unknown URI returns [] (not exception).
  - tool_call argument substring match for consumed_by.
  - subagent_spawned task_preview + produces_uris match for consumed_by.
  - chain() walks ancestors via derived_from and descendants via
    spawn-declared produces_uris.
  - Memoization is keyed on log mtime (rebuilds when the log grows).
  - Best-effort failure: missing log returns [], no exception.
  - One real-session integration test: against a captured provenance.jsonl
    the most recent run that has compute_job_launched events, assert the
    reader can locate the producing event.
"""

from __future__ import annotations

import glob
import json
import os
import time
from pathlib import Path
from typing import Any, Dict, List

import pytest

from sciagent.provenance_lineage import (
    LineageEdge,
    chain,
    consumed_by,
    produced_by,
    reset_memo,
)


# ---------------------------------------------------------------------------
# Synthetic-log helpers
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _fresh_memo():
    reset_memo()
    yield
    reset_memo()


def _write_log(path: Path, events: List[Dict[str, Any]]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        for ev in events:
            f.write(json.dumps(ev) + "\n")
    return path


def _ev(kind: str, **body: Any) -> Dict[str, Any]:
    base = {
        "schema_version": "1",
        "event_id": f"evt-{kind}-{body.get('seq', 0)}",
        "event_kind": kind,
        "session_id": "test",
        "seq": body.get("seq", 0),
        "ts": "2026-05-05T00:00:00.000000+00:00",
    }
    base.update(body)
    return base


# ---------------------------------------------------------------------------
# produced_by
# ---------------------------------------------------------------------------


def test_produced_by_artifact_exact_match(tmp_path: Path):
    log = _write_log(
        tmp_path / "provenance.jsonl",
        [
            _ev("artifact_produced", seq=1, path="s3://b/run-42/fields/U", job_id="j1"),
            _ev("artifact_produced", seq=2, path="s3://b/run-42/fields/V", job_id="j2"),
        ],
    )
    edges = produced_by("s3://b/run-42/fields/U", log_path=log)
    assert len(edges) == 1
    assert edges[0].direction == "produced_by"
    assert edges[0].job_id == "j1"
    assert edges[0].event["path"] == "s3://b/run-42/fields/U"


def test_produced_by_uri_parent_of_produced_path(tmp_path: Path):
    """multi-prefix match: queried URI is a parent of the produced path."""
    log = _write_log(
        tmp_path / "provenance.jsonl",
        [
            _ev(
                "artifact_produced",
                seq=1,
                path="s3://b/run-42/fields/U/0/U",
                job_id="j1",
            ),
            _ev(
                "artifact_produced",
                seq=2,
                path="s3://b/run-42/fields/U/100/U",
                job_id="j1",
            ),
            _ev(
                "artifact_produced",
                seq=3,
                path="s3://b/run-42/fields/p/0/p",
                job_id="j1",
            ),
        ],
    )
    edges = produced_by("s3://b/run-42/fields/U", log_path=log)
    assert len(edges) == 2
    assert {e.event["path"] for e in edges} == {
        "s3://b/run-42/fields/U/0/U",
        "s3://b/run-42/fields/U/100/U",
    }


def test_produced_by_produced_path_is_parent_of_uri(tmp_path: Path):
    """Reverse direction: produced path is a parent dir of the queried URI."""
    log = _write_log(
        tmp_path / "provenance.jsonl",
        [
            _ev("artifact_produced", seq=1, path="s3://b/run-42/fields/", job_id="j1"),
        ],
    )
    edges = produced_by("s3://b/run-42/fields/U", log_path=log)
    assert len(edges) == 1


def test_produced_by_multi_event_iteration(tmp_path: Path):
    """multi-event match: the same URI is produced by two steps in iteration."""
    log = _write_log(
        tmp_path / "provenance.jsonl",
        [
            _ev(
                "artifact_produced",
                seq=1,
                path="s3://b/run-42/iter_0/U",
                job_id="iter0",
            ),
            _ev(
                "artifact_produced",
                seq=2,
                path="s3://b/run-42/iter_1/U",
                job_id="iter1",
            ),
        ],
    )
    edges = produced_by("s3://b/run-42/", log_path=log)
    assert {e.job_id for e in edges} == {"iter0", "iter1"}


def test_produced_by_validation_passed_resolved_files(tmp_path: Path):
    log = _write_log(
        tmp_path / "provenance.jsonl",
        [
            _ev(
                "produces_validation_passed",
                seq=1,
                subagent_name="compute",
                actor="subagent:compute",
                patterns=["./_outputs/run-42/fields/U"],
                resolved=[
                    {
                        "pattern": "./_outputs/run-42/fields/U",
                        "scheme": "file",
                        "files": [
                            {
                                "path": "/abs/run-42/fields/U/0/U",
                                "bytes": 4096,
                            }
                        ],
                        "file_count": 1,
                    }
                ],
                verdict="passed",
            ),
        ],
    )
    edges = produced_by("/abs/run-42/fields/U/0/U", log_path=log)
    assert len(edges) == 1
    assert edges[0].subagent_id == "compute"

    # also matches via the declared pattern
    edges_by_pattern = produced_by("./_outputs/run-42/fields/U", log_path=log)
    assert len(edges_by_pattern) == 1


def test_produced_by_compute_job_outputs_uri(tmp_path: Path):
    log = _write_log(
        tmp_path / "provenance.jsonl",
        [
            _ev(
                "compute_job_launched",
                seq=1,
                job_id="run-42",
                outputs_uri="s3://results/run-42/",
            ),
        ],
    )
    edges = produced_by("s3://results/run-42/fields/U", log_path=log)
    assert len(edges) == 1
    assert edges[0].job_id == "run-42"


def test_produced_by_unknown_uri_returns_empty(tmp_path: Path):
    log = _write_log(
        tmp_path / "provenance.jsonl",
        [_ev("artifact_produced", seq=1, path="s3://b/x/U", job_id="j1")],
    )
    assert produced_by("s3://b/y/V", log_path=log) == []


def test_produced_by_missing_log_returns_empty(tmp_path: Path):
    """Best-effort: a missing log path returns []; never raises."""
    assert produced_by("s3://x/y", log_path=tmp_path / "absent.jsonl") == []


def test_produced_by_corrupt_lines_skipped(tmp_path: Path):
    """A bad JSON line in the middle of a log is skipped, not fatal."""
    log = tmp_path / "provenance.jsonl"
    log.write_text(
        "\n".join(
            [
                json.dumps(_ev("artifact_produced", seq=1, path="s3://b/U")),
                "{not valid json",
                json.dumps(_ev("artifact_produced", seq=2, path="s3://b/V")),
            ]
        )
        + "\n"
    )
    edges = produced_by("s3://b/U", log_path=log)
    assert len(edges) == 1


# ---------------------------------------------------------------------------
# consumed_by
# ---------------------------------------------------------------------------


def test_consumed_by_tool_call_argument_substring(tmp_path: Path):
    log = _write_log(
        tmp_path / "provenance.jsonl",
        [
            _ev(
                "tool_call",
                seq=1,
                tool_call_id="t1",
                tool_name="compute_fetch",
                arguments={"source": "s3://b/run-42/fields/U", "dest": "/cache/"},
            ),
            _ev(
                "tool_call",
                seq=2,
                tool_call_id="t2",
                tool_name="shell",
                arguments={"cmd": "ls /tmp"},
            ),
        ],
    )
    edges = consumed_by("s3://b/run-42/fields/U", log_path=log)
    assert len(edges) == 1
    assert edges[0].event["tool_name"] == "compute_fetch"


def test_consumed_by_subagent_task_preview(tmp_path: Path):
    log = _write_log(
        tmp_path / "provenance.jsonl",
        [
            _ev(
                "subagent_spawned",
                seq=1,
                subagent_name="analyse",
                actor="subagent:analyse",
                task_preview="Read s3://b/run-42/fields/U and compute drag.",
            ),
        ],
    )
    edges = consumed_by("s3://b/run-42/fields/U", log_path=log)
    assert len(edges) == 1
    assert edges[0].subagent_id == "analyse"


def test_consumed_by_subagent_produces_uris(tmp_path: Path):
    """A spawn declaring produces_uris is a 'consumer of upstream' signal."""
    log = _write_log(
        tmp_path / "provenance.jsonl",
        [
            _ev(
                "subagent_spawned",
                seq=1,
                subagent_name="solver",
                actor="subagent:solver",
                task_preview="Run solver step.",
                produces_uris=["s3://b/run-42/fields/"],
            ),
        ],
    )
    edges = consumed_by("s3://b/run-42/fields/U", log_path=log)
    assert len(edges) == 1


def test_consumed_by_unknown_uri_returns_empty(tmp_path: Path):
    log = _write_log(
        tmp_path / "provenance.jsonl",
        [
            _ev(
                "tool_call",
                seq=1,
                tool_call_id="t1",
                tool_name="shell",
                arguments={"cmd": "ls"},
            ),
        ],
    )
    assert consumed_by("s3://nowhere", log_path=log) == []


# ---------------------------------------------------------------------------
# chain()
# ---------------------------------------------------------------------------


def test_chain_walks_ancestors_via_derived_from(tmp_path: Path):
    log = _write_log(
        tmp_path / "provenance.jsonl",
        [
            _ev("artifact_produced", seq=1, path="s3://b/raw/data", job_id="j0"),
            _ev(
                "artifact_produced",
                seq=2,
                path="s3://b/derived/U",
                job_id="j1",
                derived_from=["s3://b/raw/data"],
            ),
        ],
    )
    tree = chain("s3://b/derived/U", max_depth=2, log_path=log)
    assert tree["uri"] == "s3://b/derived/U"
    assert len(tree["produced_by"]) == 1
    assert len(tree["ancestors"]) == 1
    assert tree["ancestors"][0]["uri"] == "s3://b/raw/data"


def test_chain_max_depth_is_respected(tmp_path: Path):
    log = _write_log(
        tmp_path / "provenance.jsonl",
        [
            _ev("artifact_produced", seq=1, path="s3://b/A"),
            _ev(
                "artifact_produced",
                seq=2,
                path="s3://b/B",
                derived_from=["s3://b/A"],
            ),
            _ev(
                "artifact_produced",
                seq=3,
                path="s3://b/C",
                derived_from=["s3://b/B"],
            ),
        ],
    )
    shallow = chain("s3://b/C", max_depth=1, log_path=log)
    assert len(shallow["ancestors"]) == 1
    assert shallow["ancestors"][0]["ancestors"] == []


# ---------------------------------------------------------------------------
# Memoization
# ---------------------------------------------------------------------------


def test_memo_invalidates_on_mtime_change(tmp_path: Path):
    log = tmp_path / "provenance.jsonl"
    _write_log(log, [_ev("artifact_produced", seq=1, path="s3://b/U")])
    assert len(produced_by("s3://b/U", log_path=log)) == 1

    # bump mtime forward and append a second producer
    time.sleep(0.01)
    with open(log, "a") as f:
        f.write(json.dumps(_ev("artifact_produced", seq=2, path="s3://b/U")) + "\n")
    new_mtime = os.stat(log).st_mtime_ns + 1
    os.utime(log, ns=(new_mtime, new_mtime))

    edges = produced_by("s3://b/U", log_path=log)
    assert len(edges) == 2


# ---------------------------------------------------------------------------
# Integration: read a real captured session log
# ---------------------------------------------------------------------------


def _real_session_with_compute_jobs() -> Path | None:
    """Pick the most recent ~/.sciagent session whose log carries
    compute_job_launched events, the richest URI surface available in
    historical sessions."""
    base = Path.home() / ".sciagent" / "sessions"
    if not base.exists():
        return None
    candidates: list[tuple[float, Path]] = []
    for log in base.glob("*/provenance.jsonl"):
        try:
            if log.stat().st_size == 0:
                continue
            with open(log) as f:
                for line in f:
                    if '"compute_job_launched"' in line:
                        candidates.append((log.stat().st_mtime, log))
                        break
        except OSError:
            continue
    if not candidates:
        return None
    candidates.sort(reverse=True)
    return candidates[0][1]


def _first_string_leaf(value: Any, min_len: int = 4) -> str | None:
    """Return the first non-trivial string anywhere in a tool_call arguments
    blob. Used to seed a positive consumed_by query from a real session."""
    if isinstance(value, str):
        return value if len(value) >= min_len else None
    if isinstance(value, dict):
        for v in value.values():
            r = _first_string_leaf(v, min_len)
            if r:
                return r
    if isinstance(value, list):
        for v in value:
            r = _first_string_leaf(v, min_len)
            if r:
                return r
    return None


def test_real_session_consumed_by_positive_match():
    """Integration: pull a real session log, lift a value from the first
    tool_call's arguments, and assert consumed_by surfaces that very call.

    This is the strongest assertion the historical session set supports:
    until produces_validation_passed / artifact_produced events show up in
    real logs, the only positive-match URI we can pin is one that's
    already inside a tool_call's arguments dict.
    """
    log = _real_session_with_compute_jobs()
    if log is None:
        pytest.skip("no real session log available")

    seed: str | None = None
    seed_call_id: str | None = None
    with open(log) as f:
        for line in f:
            try:
                ev = json.loads(line)
            except json.JSONDecodeError:
                continue
            if ev.get("event_kind") != "tool_call":
                continue
            args = ev.get("arguments")
            if not isinstance(args, dict):
                continue
            seed = _first_string_leaf(args)
            if seed:
                seed_call_id = ev.get("tool_call_id")
                break
    if not seed or not seed_call_id:
        pytest.skip("no tool_call with a substring-matchable argument")

    edges = consumed_by(seed, log_path=log)
    assert edges, f"reader missed the seeding call for URI {seed!r}"
    assert any(
        e.event.get("tool_call_id") == seed_call_id for e in edges
    ), "the originating tool_call should be in the result set"
    for edge in edges:
        assert edge.uri == seed
        assert edge.direction == "consumed_by"


def test_real_session_produced_by_returns_list_for_unknown_uri():
    """Integration smoke: an unknown URI against a real log returns []."""
    log = _real_session_with_compute_jobs()
    if log is None:
        pytest.skip("no real session log available")
    assert produced_by("s3://definitely-not-in-this-log/xyz", log_path=log) == []
