# Lineage reader follow-ups (deferred from P0.3)

Surfaced during the P0.3 lineage reader implementation
(`src/sciagent/provenance_lineage.py`, `tests/provenance/test_provenance_lineage.py`).
Each is deliberately out of scope for the P0 ship per
`PLAN_LINEAGE_READER.md`.

## 1. Memoized SQLite view

**Status:** deferred per the plan; v1 reads the whole log in-memory.

**What:** if any single session's `provenance.jsonl` crosses ~50K
events, in-memory parse-and-filter starts to bite. The plan calls for a
sidecar SQLite view with schema
`(event_id, kind, uri, job_id, subagent_id, timestamp, raw_json)`
built on first read.

**Trigger to land:** first measured session that crosses the threshold
(no observed case yet).

## 2. Cross-session lineage

**Status:** deferred. The reader takes a single `session_id` /
`log_path` today.

**What:** reproducibility audits across re-runs want
`produced_by(uri)` to walk a list of session logs and aggregate. API
surface would be `produced_by(uri, *, session_ids=[...])`. Same
in-memory filter, just iterated over multiple files.

**Trigger to land:** the first audit consumer that needs it (P1
verifier refresh is the likely first).

## 3. CLI surface — `sciagent lineage <uri>`

**Status:** deferred. The reader is callable from Python only.

**What:** an offline-debugging CLI that wraps `chain(uri)` and prints
the producer/consumer subtree. Useful when something on disk looks
suspicious and you want to know what step claimed to land it without
booting the agent.

**Trigger to land:** first ad-hoc `grep` session that would have been
served by the CLI.

## 4. Structured tool-schema parser for `consumed_by`

**Status:** v1 uses substring match across `tool_call.arguments`.

**What:** a real per-tool-schema parser would know that
`compute_fetch.source` is the input URI but `compute_fetch.dest` is an
output, and would not produce false positives when a URI happens to
appear inside an unrelated argument string. The plan explicitly puts
this at P1+ — substring is "enough for v1".

**Trigger to land:** first false-positive that bites a real
verifier / iteration loop.

## 5. `outputs_uri` not yet emitted on `compute_job_launched`

**Status:** reader is forward-compatible; emitter side hasn't shipped.

**What:** the reader's `produced_by` matches a compute_job's
`outputs_uri` prefix per the plan, but
`ProvenanceLog.emit_compute_job_launched` doesn't carry that field
today. The branch is harmless (non-matches are skipped) but the lineage
signal stays dark until that field lands.

**Trigger to land:** when the compute layer has a stable URI for
`/outputs/<job_id>/` (depends on the auto-fetch convention being
stable enough to record).

## 6. `subagent_spawned` does not record `produces_uris`

**Status:** reader is forward-compatible; emitter side records
`produces_uris` only in the task_index manifest, not the spawn event.

**What:** `consumed_by` matches `subagent_spawned.produces_uris` when
present (consumer-of-upstream signal), but `_emit_spawned` in
`subagent.py` only writes `subagent_name` + `task_preview`. Adding
`produces_uris` to the spawn event body is a small change — the
manifest already has it.

**Trigger to land:** when the iteration-loop / verifier-refresh
consumer wants this signal. Watch for false negatives in
`consumed_by` traces against subagent boundaries.

## 7. URI canonicalization (cross-cloud + post-materialize)

**Status:** v1 does exact-or-prefix on the literal string.

**What:** `s3://bucket/x` vs `s3a://bucket/x` vs the same path
post-`materialize` as `/cache/x` are three different strings for the
same artifact. A small canonicalizer (scheme normalization +
materialize-mount lookup) would help, but only once a real consumer
hits the gap.

**Trigger to land:** first cross-cloud or pre/post-materialize lineage
miss that bites a verifier.
