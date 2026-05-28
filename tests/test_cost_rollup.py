"""Unit tests for scripts/cost_rollup.py — the H3 deliverable for the
bench's Pareto plot data feed.

The script lives outside the importable ``sciagent`` package, so we load
it via importlib.util from the repo root.
"""

from __future__ import annotations

import csv
import importlib.util
import io
import json
import sys
from pathlib import Path
from types import ModuleType

import pytest


REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT_PATH = REPO_ROOT / "scripts" / "cost_rollup.py"


def _load_script() -> ModuleType:
    spec = importlib.util.spec_from_file_location("cost_rollup", SCRIPT_PATH)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def cost_rollup() -> ModuleType:
    return _load_script()


def _v2_tool_result(
    *,
    session_id: str,
    model: str,
    tool_name: str,
    cost_usd: float | None,
    tokens_in: int | None,
    tokens_out: int | None,
    seq: int,
) -> dict:
    return {
        "schema_version": "2",
        "event_id": f"e{seq}",
        "event_kind": "tool_result",
        "session_id": session_id,
        "seq": seq,
        "ts": "2026-05-28T00:00:00.000000+00:00",
        "tool_call_id": f"c{seq}",
        "tool_name": tool_name,
        "success": True,
        "output_summary": "ok",
        "error": None,
        "duration_ms": 5,
        "cost_usd": cost_usd,
        "tokens_in": tokens_in,
        "tokens_out": tokens_out,
        "model": model,
    }


def _write_jsonl(path: Path, events: list[dict]) -> None:
    with path.open("w", encoding="utf-8") as f:
        for ev in events:
            f.write(json.dumps(ev) + "\n")


def test_rollup_sums_match_per_call_sums(tmp_path: Path, cost_rollup: ModuleType):
    log = tmp_path / "provenance.jsonl"
    events = [
        _v2_tool_result(session_id="s1", model="claude-opus-4-7", tool_name="shell",
                        cost_usd=0.10, tokens_in=100, tokens_out=10, seq=1),
        _v2_tool_result(session_id="s1", model="claude-opus-4-7", tool_name="shell",
                        cost_usd=0.05, tokens_in=50, tokens_out=5, seq=2),
        _v2_tool_result(session_id="s1", model="claude-opus-4-7", tool_name="verify_session",
                        cost_usd=0.02, tokens_in=30, tokens_out=3, seq=3),
        _v2_tool_result(session_id="s2", model="claude-haiku-4-5", tool_name="shell",
                        cost_usd=0.01, tokens_in=20, tokens_out=2, seq=4),
        # Non-tool_result events are ignored.
        {
            "schema_version": "2",
            "event_id": "e5",
            "event_kind": "verification_result",
            "session_id": "s1",
            "seq": 5,
            "ts": "2026-05-28T00:00:00.005000+00:00",
            "gate": "data",
            "task_id": "t1",
            "claim": {},
            "verdict": "verified",
            "confidence": 1.0,
            "evidence": {},
            "issues": [],
            "verifier": "claude-haiku-4-5",
        },
    ]
    _write_jsonl(log, events)

    rows = cost_rollup.rollup_tool_results(cost_rollup._iter_events(log))
    by_key = {(r["session_id"], r["model"], r["tool_name"]): r for r in rows}

    shell_s1 = by_key[("s1", "claude-opus-4-7", "shell")]
    assert shell_s1["count"] == 2
    assert shell_s1["tokens_in"] == 150
    assert shell_s1["tokens_out"] == 15
    assert shell_s1["cost_usd"] == pytest.approx(0.15)

    verify_s1 = by_key[("s1", "claude-opus-4-7", "verify_session")]
    assert verify_s1["count"] == 1
    assert verify_s1["cost_usd"] == pytest.approx(0.02)

    shell_s2 = by_key[("s2", "claude-haiku-4-5", "shell")]
    assert shell_s2["count"] == 1
    assert shell_s2["cost_usd"] == pytest.approx(0.01)

    # Verification_result event must not appear in tool_result rollup.
    assert all(r["tool_name"] != "" for r in rows)
    assert len(rows) == 3


def test_rollup_handles_v1_log_with_missing_cost_fields(tmp_path: Path, cost_rollup: ModuleType):
    """v1 tool_result events have no cost / token fields. Rollup must
    still count them and contribute 0 to the sums — never raise."""
    log = tmp_path / "provenance.jsonl"
    v1_event = {
        "schema_version": "1",
        "event_id": "e1",
        "event_kind": "tool_result",
        "session_id": "v1sess",
        "seq": 1,
        "ts": "2026-05-27T00:00:00.000000+00:00",
        "tool_call_id": "c1",
        "tool_name": "shell",
        "success": True,
        "output_summary": "ok",
        "error": None,
        "duration_ms": 5,
    }
    _write_jsonl(log, [v1_event, v1_event | {"event_id": "e2", "seq": 2, "tool_call_id": "c2"}])

    rows = cost_rollup.rollup_tool_results(cost_rollup._iter_events(log))
    assert len(rows) == 1
    row = rows[0]
    assert row["count"] == 2
    assert row["tokens_in"] == 0
    assert row["tokens_out"] == 0
    assert row["cost_usd"] == 0.0


def test_cli_writes_csv_to_output(tmp_path: Path, cost_rollup: ModuleType):
    log = tmp_path / "provenance.jsonl"
    _write_jsonl(log, [
        _v2_tool_result(session_id="s1", model="claude-opus-4-7", tool_name="shell",
                        cost_usd=0.10, tokens_in=100, tokens_out=10, seq=1),
    ])
    out_csv = tmp_path / "out.csv"
    rc = cost_rollup.main([str(log), "--output", str(out_csv)])
    assert rc == 0

    with out_csv.open() as f:
        reader = csv.DictReader(f)
        rows = list(reader)
    assert len(rows) == 1
    assert rows[0]["session_id"] == "s1"
    assert rows[0]["model"] == "claude-opus-4-7"
    assert rows[0]["tool_name"] == "shell"
    assert rows[0]["count"] == "1"
    assert rows[0]["tokens_in"] == "100"
    assert rows[0]["tokens_out"] == "10"
    assert float(rows[0]["cost_usd"]) == pytest.approx(0.10)


def test_cli_missing_log_returns_nonzero(tmp_path: Path, cost_rollup: ModuleType):
    rc = cost_rollup.main([str(tmp_path / "does-not-exist.jsonl")])
    assert rc != 0
