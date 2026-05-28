#!/usr/bin/env python3
"""Aggregate per-call LLM cost / tokens from a provenance.jsonl (schema v2).

Reads a ``provenance.jsonl`` written by sciagent (H3, schema_version="2"),
groups ``tool_result`` events by ``(session_id, model, tool_name)``, and
writes a CSV with summed tokens + cost + call count.

Usage:
    python scripts/cost_rollup.py <log-path> [--output <csv-path>]

Output columns: session_id, model, tool_name, count, tokens_in, tokens_out, cost_usd

v1 logs read cleanly: tool_result events without cost fields contribute
zero to the cost totals and are still counted. Aggregation function
``rollup_tool_results`` is exposed for in-process tests.
"""
from __future__ import annotations

import argparse
import csv
import json
import sys
from pathlib import Path
from typing import Any, Dict, Iterable, List, Tuple

GroupKey = Tuple[str, str, str]  # (session_id, model, tool_name)


def _iter_events(log_path: Path) -> Iterable[Dict[str, Any]]:
    with log_path.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except json.JSONDecodeError:
                continue


def rollup_tool_results(events: Iterable[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Sum cost_usd / tokens_in / tokens_out per (session_id, model, tool_name).

    None token / cost fields contribute 0. ``cost_usd`` is summed as float
    so rounding error stays at the last decimal; callers comparing against
    a per-event sum should allow rounding slack.
    """
    buckets: Dict[GroupKey, Dict[str, Any]] = {}
    for ev in events:
        if ev.get("event_kind") != "tool_result":
            continue
        key: GroupKey = (
            ev.get("session_id") or "",
            ev.get("model") or "",
            ev.get("tool_name") or "",
        )
        row = buckets.setdefault(
            key,
            {
                "session_id": key[0],
                "model": key[1],
                "tool_name": key[2],
                "count": 0,
                "tokens_in": 0,
                "tokens_out": 0,
                "cost_usd": 0.0,
            },
        )
        row["count"] += 1
        row["tokens_in"] += int(ev.get("tokens_in") or 0)
        row["tokens_out"] += int(ev.get("tokens_out") or 0)
        row["cost_usd"] += float(ev.get("cost_usd") or 0.0)
    return list(buckets.values())


def _write_csv(rows: List[Dict[str, Any]], out: Any) -> None:
    writer = csv.writer(out)
    writer.writerow(["session_id", "model", "tool_name", "count", "tokens_in", "tokens_out", "cost_usd"])
    for r in rows:
        writer.writerow(
            [r["session_id"], r["model"], r["tool_name"], r["count"], r["tokens_in"], r["tokens_out"], f"{r['cost_usd']:.6f}"]
        )


def main(argv: List[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Aggregate LLM cost from a provenance.jsonl.")
    parser.add_argument("log_path", type=Path, help="Path to provenance.jsonl")
    parser.add_argument("--output", type=Path, default=None, help="CSV path; stdout if omitted")
    args = parser.parse_args(argv)

    if not args.log_path.exists():
        print(f"error: {args.log_path} does not exist", file=sys.stderr)
        return 2

    rows = rollup_tool_results(_iter_events(args.log_path))
    if args.output is None:
        _write_csv(rows, sys.stdout)
    else:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        with args.output.open("w", encoding="utf-8", newline="") as f:
            _write_csv(rows, f)
    return 0


if __name__ == "__main__":
    sys.exit(main())
