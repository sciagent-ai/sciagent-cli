# Provenance log schema (v1)

This document describes the durable, append-only JSONL log that sciagent
writes for every session. The log is the contract between the LLM that
ran a session and any other LLM (or human) that wants to verify what
happened, after the fact, using only on-disk evidence.

A reader of this document is expected to be an LLM from any provider
(Claude, GPT, Gemini, open-source). Examples below avoid provider-specific
language, type names, and SDK constructs. The log itself contains plain
JSON only.

---

## File layout

```
~/.sciagent/sessions/<session_id>/provenance.jsonl
```

- One JSON object per line, UTF-8 encoded, terminated by `\n`.
- Append-only. The writer never rewrites a previously-emitted line. When
  a later observation invalidates an earlier event, the writer appends a
  `correction` event referencing the original `event_id`.
- A sidecar file `.provenance.lock` lives in the same directory. Concurrent
  writers (and readers) acquire `flock(2)` on it for the duration of one
  line write or one full-file read. Readers using the documented
  `read_events()` API never see a torn line.
- Writes use append-mode I/O followed by `fsync`. A process crash mid-write
  leaves the file with at most one partial line; readers skip malformed
  lines and surface a synthetic `_parse_error` entry so the rest of the
  session is still auditable.
- `<session_id>` is sciagent's session identifier (12 hex characters
  derived from a sha256 prefix). It is the same value used in the per-job
  manifest at `~/.sciagent/tasks/<job_id>.json`.

---

## Common envelope

Every event has the same envelope. Fields beyond the envelope vary by
`event_kind`.

| field            | type    | required | description |
|------------------|---------|----------|-------------|
| `schema_version` | string  | yes      | `"1"`. A future-incompatible schema change bumps this. |
| `event_id`       | string  | yes      | Random UUID4. Stable id for dedup or for `correction.corrects_event_id`. |
| `event_kind`     | string  | yes      | One of: `tool_call`, `tool_result`, `compute_job_launched`, `compute_job_status_changed`, `artifact_produced`, `verification_result`, `correction`. |
| `session_id`     | string  | yes      | Mirrors the directory name. |
| `seq`            | integer | yes      | Monotonic per session. Use this for ordering instead of trusting `ts`. |
| `ts`             | string  | yes      | UTC ISO 8601 with microsecond precision and explicit `+00:00` suffix, e.g. `"2026-04-29T13:14:15.123456+00:00"`. |
| `actor`          | string  | no       | Provider/model that drove the event when known (e.g. `"claude-opus-4-7"`, `"gpt-4o-mini"`). Provider-neutral string; absent for backend / framework events. |

---

## Event kinds

### 1. `tool_call`

Emitted right before an atomic tool dispatches. One per tool call.

| field               | type            | required | description |
|---------------------|-----------------|----------|-------------|
| `tool_call_id`      | string          | yes      | Provider-assigned id (opaque). |
| `tool_name`         | string          | yes      | e.g. `"compute_run"`, `"shell"`, `"web_fetch"`. |
| `arguments`         | object          | yes      | Tool arguments as the LLM provided them. **Truncatable**; see "Bounded growth." |
| `arguments_sha256`  | string (64-hex) | yes      | `sha256(canonical_json(arguments))` of the original (pre-truncation) value — stable id even when `arguments` itself is replaced by a truncation stub. |

**Example:**

```json
{
  "schema_version": "1",
  "event_id": "f1d3a3a4-3a8c-4f0b-9b9f-2f4f8e6e1234",
  "event_kind": "tool_call",
  "session_id": "a1b2c3d4e5f6",
  "seq": 12,
  "ts": "2026-04-29T13:14:15.123456+00:00",
  "actor": "claude-opus-4-7",
  "tool_call_id": "call_018xY",
  "tool_name": "compute_run",
  "arguments": {
    "service": "openfoam-swak4foam",
    "command": "bash Allrun",
    "workspace_source": "s3://sciagent-b8-typical-c"
  },
  "arguments_sha256": "9c2e..."
}
```

### 2. `tool_result`

Emitted right after a tool returns. Pairs with the matching `tool_call`
via `tool_call_id`.

| field            | type            | required | description |
|------------------|-----------------|----------|-------------|
| `tool_call_id`   | string          | yes      | Same id as the matching `tool_call`. |
| `tool_name`      | string          | yes      | Duplicated for readability when the log is read alone. |
| `success`        | boolean         | yes      | Whether the tool reported success. |
| `output_summary` | object \| string \| null | yes | Summary of the tool's output. Image results record `{"type": "image", "media_type": "...", "size_bytes": N, "file_path": "..."}` — never the base64 payload. **Truncatable.** |
| `error`          | string \| null  | yes      | Human-readable error string, if any. **Truncatable.** |
| `duration_ms`    | integer         | yes      | Wall-clock from call to result. |

**Example:**

```json
{
  "schema_version": "1",
  "event_id": "...",
  "event_kind": "tool_result",
  "session_id": "a1b2c3d4e5f6",
  "seq": 13,
  "ts": "2026-04-29T13:14:16.789012+00:00",
  "actor": "claude-opus-4-7",
  "tool_call_id": "call_018xY",
  "tool_name": "compute_run",
  "success": true,
  "output_summary": {
    "job_id": "sciagent-fe0e4e60",
    "status": "running",
    "backend": "skypilot",
    "managed_job_id": 4231
  },
  "error": null,
  "duration_ms": 1582
}
```

### 3. `compute_job_launched`

Emitted after a successful cloud-job launch. M1B emits this only for the
SkyPilot backend; local-backend symmetry is deferred.

| field                | type                  | required | description |
|----------------------|-----------------------|----------|-------------|
| `job_id`             | string                | yes      | sciagent's human-readable job name (e.g. `"sciagent-<uuid>"`). Same key used in the per-job manifest. |
| `managed_job_id`     | integer \| null       | yes      | SkyPilot's integer id when the controller acknowledged the launch within the fail-fast budget; `null` otherwise. A subsequent `compute_job_status_changed` event may carry the resolved integer. |
| `backend`            | string                | yes      | `"skypilot"` for cloud; `"local"` reserved. |
| `service`            | string \| null        | yes      | sciagent service registry name, or `null` when launched image-only. |
| `image`              | string \| null        | yes      | Resolved Docker image. |
| `command_original`   | string                | yes      | Command as the LLM passed it through `compute_run`. |
| `command_resolved`   | string                | yes      | Command after the backend's deterministic rewrites: a `cd <mount_path> &&` prefix when a storage mount is attached, and a `timeout N bash -c '...'` wrap when `timeout_sec > 0`. The two fields diverge by mechanical rules; preserving both lets a verifier attribute a failure to LLM logic versus backend wrapping. |
| `mount_path`         | string \| null        | yes      | The first storage mount's path inside the cluster (e.g. `"/workspace"`, `"/data"`). `null` when no mount was attached. |
| `mount_bucket`       | string \| null        | yes      | Bucket name backing the mount. |
| `requirements`       | object                | yes      | `{cpus, memory_gb, gpus, gpu_type, timeout_sec}`. Plain JSON; no SDK enums. |
| `intent`             | object \| null        | yes      | Opaque payload recorded verbatim. The writer never validates or normalizes its shape. |
| `expected_artifacts` | array of strings      | yes      | Opaque list of expected output paths recorded verbatim. May be empty. |

**Example:**

```json
{
  "schema_version": "1",
  "event_id": "...",
  "event_kind": "compute_job_launched",
  "session_id": "a1b2c3d4e5f6",
  "seq": 14,
  "ts": "2026-04-29T13:14:17.000000+00:00",
  "job_id": "sciagent-fe0e4e60",
  "managed_job_id": 4231,
  "backend": "skypilot",
  "service": "openfoam-swak4foam",
  "image": "ghcr.io/sciagent-ai/openfoam-swak4foam:latest",
  "command_original": "bash Allrun",
  "command_resolved": "timeout 3600 bash -c 'cd /workspace && bash Allrun'",
  "mount_path": "/workspace",
  "mount_bucket": "sciagent-b8-typical-c",
  "requirements": {"cpus": 4, "memory_gb": 32, "gpus": 0, "gpu_type": null, "timeout_sec": 3600},
  "intent": {"paper": "doi:10.example/foo", "case": "typical_c", "run": "smoke"},
  "expected_artifacts": ["postProcessing/probes/0/U", "log.simpleFoam"]
}
```

### 4. `compute_job_status_changed`

Emitted on each observed status transition for a managed job. The writer
deduplicates: a poll that observes the same mapped status as the last
emission for a given `job_id` does not produce a new event. Dedup is
process-local — after a process restart the next poll re-emits the
current status with `status_previous: null`.

| field             | type            | required | description |
|-------------------|-----------------|----------|-------------|
| `job_id`          | string          | yes      | sciagent job name. |
| `managed_job_id`  | integer \| null | yes      | SkyPilot integer when known. |
| `status`          | string (enum)   | yes      | Mapped sciagent status. One of: `pending`, `running`, `recovering`, `cancelled`, `completed`, `failed`. |
| `status_previous` | string \| null  | yes      | Previously emitted mapped status this process saw, or `null` for the first emission. |
| `sky_status_raw`  | string \| null  | yes      | The original SkyPilot status name verbatim (e.g. `"FAILED_NO_RESOURCE"`, `"SUBMITTED"`). Lets a verifier see the variant before sciagent collapsed it. `null` when unavailable. |
| `error_preview`   | string \| null  | yes      | First-line error snippet when `status == "failed"`. |
| `log_file`        | string \| null  | yes      | Local path to a saved log tail when `status == "failed"`. |

**Status semantics:**

- `pending`: any pre-running state (sciagent collapses SkyPilot's
  `PENDING`, `SUBMITTED`, `STARTING` here — the agent has no actionable
  difference between them).
- `running`: actively executing. SkyPilot's `CANCELLING` is also reported
  as `running` until terminal — a cancel may not succeed and reporting
  `cancelled` prematurely would mis-cue the agent.
- `recovering`: SkyPilot is recovering the managed job from a transient
  failure.
- `completed`: SkyPilot's `SUCCEEDED`.
- `cancelled`: terminal cancellation.
- `failed`: any terminal failure. SkyPilot's variants
  (`FAILED_SETUP`, `FAILED_PRECHECKS`, `FAILED_NO_RESOURCE`,
  `FAILED_CONTROLLER`) all collapse here; the variant lives in
  `sky_status_raw`.

### 5. `artifact_produced`

Emitted when a file is observed on disk. M1B emits this for local file
checks (driven by `provenance.ProvenanceChecker._verify_file`) and for
explicit file paths returned in tool results. Emission for cloud-bucket
artifacts discovered post-job by sweeping `expected_artifacts` against
the mount is deferred to M2A; the schema is forward-compatible.

| field                     | type            | required | description |
|---------------------------|-----------------|----------|-------------|
| `path`                    | string          | yes      | Absolute path. Cluster-side absolute when on a mount; local absolute otherwise. |
| `mount_path`              | string \| null  | yes      | Mount root (e.g. `"/workspace"`) when the artifact lives on a known cluster mount. `null` for local artifacts. |
| `path_relative_to_mount`  | string \| null  | yes      | `path` with `mount_path` stripped, when applicable. Convenience field; verifier can derive it from `path` and `mount_path`. |
| `job_id`                  | string \| null  | yes      | Compute job that produced the artifact, when known. |
| `size_bytes`              | integer \| null | yes      | When observable. |
| `sha256`                  | string \| null  | yes      | Optional integrity hash. Computed only for files small enough to read inline; `null` otherwise. |
| `content_type`            | string \| null  | yes      | `"csv"`, `"json"`, etc., when known. |
| `metadata`                | object \| null  | yes      | Validator output (row count, columns, etc.) when available. |

### 6. `verification_result`

Emitted by sciagent's existing verification gates: `ProvenanceChecker`
(DATA + EXEC) and the orchestrator's LLM verification gate.

| field        | type            | required | description |
|--------------|-----------------|----------|-------------|
| `gate`       | string (enum)   | yes      | `"data"`, `"exec"`, or `"llm"`. |
| `task_id`    | string \| null  | yes      | Todo task id when the gate ran on a specific task; `null` for session-wide checks. |
| `claim`      | object          | yes      | What was being verified. Shape: `{kind: "data_acquisition" \| "execution" \| "tests_ran" \| "task_outcome", ...details}`. **Truncatable.** |
| `verdict`    | string (enum)   | yes      | `"verified"`, `"refuted"`, `"insufficient"`, or `"warning"`. |
| `confidence` | number \| null  | yes      | 0.0–1.0 for the LLM gate; `null` for DATA/EXEC. |
| `evidence`   | object          | yes      | Summary of which logs/files/checks were consulted. **Truncatable.** |
| `issues`     | array of object | yes      | Each `{severity: "error" \| "warning" \| "info", category: string, message: string}`. May be empty. |
| `verifier`   | string          | yes      | `"provenance_checker"` for DATA/EXEC; the verifier model name (e.g. `"claude-opus-4-7"`, `"gpt-4o-mini"`) for the LLM gate. Provider-neutral. |

### 7. `correction`

Emitted when a later observation supersedes an earlier event. The
original line is never rewritten.

| field               | type   | required | description |
|---------------------|--------|----------|-------------|
| `corrects_event_id` | string | yes      | The `event_id` being corrected. |
| `reason`            | string | yes      | Free-form explanation. |
| `replacement`       | object | yes      | Body fields to use in place of the original. |

A verifier scanning the log linearly and merging corrections produces a
"corrected view" of the session. M1B does not currently emit any
`correction` events; the kind is documented so future readers know how
to handle them.

---

## Bounded growth

- **Hard cap, advisory:** each line ≤ 16 KB after JSON serialization.
- **Per-field cap:** any value listed in the per-event-kind "Truncatable"
  table whose serialized form exceeds 4 KB is replaced with a stub:

  ```json
  {
    "_truncated": true,
    "_original_size": 12345,
    "_preview": "first 256 characters of the original",
    "_sha256": "hex sha256 of the full original"
  }
  ```

- **Load-bearing fields are never truncated.** `command_original`,
  `command_resolved`, `intent`, `expected_artifacts`, `path` — if any
  of these exceeds the cap the line is emitted slightly over budget
  rather than corrupted. The cap is for transparency, not correctness.

| event_kind             | truncatable fields           |
|------------------------|------------------------------|
| `tool_call`            | `arguments`                  |
| `tool_result`          | `output_summary`, `error`    |
| `verification_result`  | `claim`, `evidence`          |

---

## Reading the log

A verifier reading the log cold should:

1. Read the whole file under a shared `flock` on `.provenance.lock`. (The
   sciagent reader API does this for you.)
2. Skip lines marked `_parse_error` but record that they were observed.
3. Sort events by `seq` for ordering. Treat `ts` as informative only —
   monotonic ordering is what `seq` guarantees.
4. Match `tool_call` to `tool_result` by `tool_call_id`.
5. Match `compute_job_launched` to subsequent `compute_job_status_changed`
   and `artifact_produced` events by `job_id`.
6. Apply `correction` events last: each correction's `replacement` body
   shadows the corresponding fields on the event with matching
   `corrects_event_id`.

The cross-LLM verification claim that this schema is built around: a
verifier that reads the log this way, without any access to sciagent's
runtime, can determine which tools ran, what commands actually executed
on which clusters, where outputs landed, and whether the agent's earlier
verification gates produced unbiased verdicts.
