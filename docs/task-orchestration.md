---
layout: default
title: Task Orchestration
nav_order: 6
---

# Task Orchestration

SciAgent tracks long-running work — cloud compute jobs and background subagents — in a single registry called the **task index**. One on-disk format, one set of query tools, one state machine.

This page covers the user-facing surface (the `task_*` and `bg_*` tools, and how background subagents work). For schema details, see `src/sciagent/compute/task_index.py`.

## Task index

Per-task manifest at `~/.sciagent/tasks/<task_id>.json`:

```json
{
  "job_id": "sciagent-abc123",
  "session_id": "abc12345",
  "kind": "compute_job",
  "state": "running",
  "intent": {"paper": "...", "case": "..."},
  "expected_artifacts": [...],
  "owner_pid": 12345,
  "started_at": "2026-04-27T18:32:11Z",
  "command": "bash Allrun"
}
```

The manifest is intentionally permissive — `intent` and `expected_artifacts` are opaque passthrough blobs. The kind/state fields are the part that matters for routing.

### Kinds

| Kind | What it tracks |
|------|----------------|
| `compute_job` | A cloud compute job launched via `compute_run` |
| `subagent` | A subagent run (background or foreground) |

Future kinds (`watch`, `scheduled`) land additively — same registry, same tools, no per-kind tool branching.

### States

```
pending → running → {completed | failed | cancelled | blocked_produce_missing}
                  → {crashed | blocked_resume}      ← resumable, subagent-only
```

| State | Terminal? | Notes |
|-------|-----------|-------|
| `pending` | no | Registered but not yet started |
| `running` | no | Active |
| `completed` | yes | Success |
| `failed` | yes | Real failure (LLM error, tool error, non-zero exit) |
| `cancelled` | yes | User-cancelled |
| `blocked_produce_missing` | yes | Subagent claimed success but its `produces_uris` patterns didn't resolve to artifacts |
| `crashed` | no (resumable) | Run raised before terminal — server disconnect, network drop, transient LLM error |
| `blocked_resume` | no (resumable) | Agent itself decided the work can't finish in this process and asked to be picked up later |

The two resumable states are subagent-specific — see [Checkpoint & resume](#checkpoint--resume) below.

## Tools: kind-agnostic registry view

The `task_*` tools query the registry across kinds and states. Use them for "what's tracked" — anything cross-cutting.

### task_list

Enumerate tracked tasks. Filters compose with AND.

```
task_list()                                    # everything
task_list(kind="compute_job")                  # only cloud jobs
task_list(state="running")                     # only active tasks
task_list(kind="subagent", session_id="abc12345")  # this session's subagents
```

Returns one short block per task with `job_id`, `kind`, `state`, `session_id`, `started_at`, `completed_at`, `result_summary`.

### task_get

Inspect a single task's full manifest.

```
task_get("sciagent-abc123")
```

Returns the on-disk verbatim shape (not normalized), so existing callers don't break when new fields land.

### task_wait

Block until a task reaches a terminal state. Kind-agnostic.

```
task_wait("sciagent-abc123", timeout=1800, poll_interval=5)
```

Works on any kind — compute jobs, subagents, future kinds. Use this when you need to wait on a task without caring whether it's cloud or local.

## Tools: kind-specific runtime view

The `bg_*` tools own the cloud-job runtime surface — status from Sky, log streaming, output fetching, terminal-state polling. Keep using them for cloud-job-specific operations.

| Tool | Purpose |
|------|---------|
| `bg_status` | Sky-side status + sciagent local manifest joined |
| `bg_output` | Stream output from a job (stdout/stderr) |
| `bg_wait` | Block until job reaches terminal state (cloud-job-flavored) |
| `bg_kill` | Cancel a running job |

Rule of thumb:
- **Cross-kind queries / lifecycle waits** → `task_*`
- **Per-cloud-job status, logs, kill** → `bg_*`

## Background subagents

Subagents are normally synchronous — `spawn(...)` returns when the work is done. But for long-running orchestration (e.g., the parent wants to kick off two analyses and supervise both), `spawn(background=True)` returns immediately with a `task_id`.

```python
# From a parent agent's prompt — the agent issues this via the task tool.
# Conceptually:
bg_id = spawn(
    agent_name="analyze",
    task="KDE plot of T field at z=0.1m",
    background=True,
    produces_uris=["./_outputs/kde_z01.png"],
)

# Parent continues. Later:
record = task_get(bg_id)         # snapshot the registry entry
result = task_wait(bg_id, 1800)  # block until terminal
```

The background task is registered in the task index with `kind="subagent"` and a manifest stored at `~/.sciagent/sessions/<session_id>/subagents/<task_id>/`. The parent can poll, wait, or just check in next turn.

### produces_uris validation

If the parent declares `produces_uris=[...]` when spawning, the orchestrator validates after the subagent returns: each pattern must resolve to at least one file with size ≥ `produces_min_bytes` (default 100). If validation fails, the task lands in `blocked_produce_missing` state — the parent can read the manifest and decide whether to retry, redirect, or report.

This is the contract that prevents silent "I claimed success but actually the file isn't there" failures.

## Checkpoint & resume

Subagents checkpoint per-iteration to:

```
~/.sciagent/sessions/<session_id>/subagents/<task_id>/checkpoint.jsonl
~/.sciagent/sessions/<session_id>/subagents/<task_id>/agent_state.json
```

Schema version: `1`. Every iteration appends a checkpoint event (tool calls, hashes, message previews). On crash before terminal state, the entry's `state` is set to `crashed` and the checkpoint persists.

### Resume flow

A fresh `spawn(...)` for a subagent-kind task hashes the task description and looks for a prior `crashed` or `blocked_resume` entry with the same hash. Within the warm-resume window (configurable via `CloudConfig.subagent_warm_resume_seconds`, env `SCIAGENT_SUBAGENT_WARM_RESUME_SECONDS`, or `~/.sciagent/config.yaml` `subagent.warm_resume_seconds`) the orchestrator prompts the parent for a 3-way choice:

| Choice | Effect |
|--------|--------|
| `skip` | Treat the prior run as failed; spawn fresh from zero |
| `use_prior` | Treat the prior run as authoritative; return its last result |
| `retry` | Reload `agent_state.json`, replay from checkpoint, continue running |

The prompt is a real `ask_user` — no silent resumption. The user sees what crashed and decides.

### When to use `blocked_resume`

A subagent voluntarily lands in `blocked_resume` when it realizes the work can't finish in the current process — typically because the parent's token budget is about to run out, or because the subagent is mid-pipeline and the next step needs a different cluster that the parent should provision. The work pauses cleanly and the parent can pick it up later.

## Storage layout

```
~/.sciagent/
├── tasks/
│   ├── sciagent-abc123.json          # compute_job manifest
│   └── subagent-xyz789.json          # subagent manifest
└── sessions/
    └── <session_id>/
        ├── provenance.jsonl          # durable audit log
        └── subagents/
            └── <task_id>/
                ├── checkpoint.jsonl  # per-iteration checkpoint events
                └── agent_state.json  # full state snapshot
```

The task index is global (`~/.sciagent/tasks/`). Per-session state — provenance log, subagent checkpoints — lives under `sessions/<session_id>/`.

## See also

- [Cloud Compute](cloud-compute.md) — `compute_run` produces `compute_job`-kind tasks
- [Tools reference](tools.md) — full signatures for `task_list`, `task_get`, `task_wait`, `bg_*`
- [Provenance log schema](provenance_log_schema.md) — companion durable audit log
- [Architecture → Task index & state surfaces](developers/architecture.md#task-index--state-surfaces)
