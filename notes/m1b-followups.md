# M1B follow-ups (deferred from `m1b-provenance-log`)

These are wrapper-too-thin issues or scope-bend points surfaced *during*
M1B that we deliberately did not patch in M1B to keep the milestone
tight. Each is either a deferred deliverable from the handoff or a
design observation that wants a real owner before it ships.

## 1. Cross-LLM e2e test  *(CLOSED — ran 2026-04-29, passed)*

**Status:** ran on `m1b-provenance-log` with `gpt-4o-mini` per user
authorization. Test passed: the verifier recovered the compute job +
final_status, the artifact path, the data-gate verdict, and the
compute_run tool call from the JSONL alone. Cost ~$0.01.

**Where:** `tests/provenance/test_e2e_cross_llm.py`, gated behind
`RUN_CROSS_LLM_TESTS=1` + `OPENAI_API_KEY`. Default verifier is
`gpt-4o-mini`; override via `CROSS_LLM_VERIFIER_MODEL`.

**Note for future batches:** the M1A cross-provider smoke test
(`m1a-followups.md` #1) is still pending. Same LiteLLM surface; could
batch into one session next time the user opens the cross-LLM
authorization window.

## 2. Cloud-bucket artifact discovery is M2A

**Status:** schema is forward-compatible; emission deferred per scope rule.

**What:** `artifact_produced` events fire today only for local file
checks (driven by `ProvenanceChecker._verify_file`) and for explicit
file paths returned in tool results. A post-job sweep that walks
`expected_artifacts` against the cluster mount and emits
`artifact_produced` per observed file is the M2A `watch_index` /
`kind=artifact_present` story.

The schema already documents `path` as cluster-side absolute and
`mount_path` / `path_relative_to_mount` as the canonical anchors
(m1a-followup #6) so the M2A emission can drop in without a schema
bump.

**Trigger to land:** when M2A's monitor / watch_index is built. At that
point the post-job artifact sweep is one more `kind` of watch record.

## 3. `compute_job_launched` is skypilot-only

**Status:** by design for M1B; Local-backend symmetry deferred.

**What:** the M1B handoff names the SkyPilot backend as the lifecycle-
emission site. `LocalBackend.run()` does not emit
`compute_job_launched`. A verifier reading a session that ran a local
job sees the launch only via the `tool_call` / `tool_result` pair.

**Why deferred:** the M1A scope rule freezes the local-backend launch
contract; M1B respects that. Local-backend symmetry is small and would
not change the M1A surface, but it is M2A-shape (M2A unifies bg_*
across local + cloud per v4.4 §5.7).

**Trigger to land:** M2A's bg_* unification. Same emission shape; just
add the call site in `LocalBackend.run`.

## 4. `verify_session` does not consult the per-job manifest

**Status:** intentional layering; flag for M2A consideration.

**What:** the manifest at `~/.sciagent/tasks/<job_id>.json` carries
load-bearing fields the JSONL also records (`session_id`,
`managed_job_id`, `intent`, `expected_artifacts`). `verify_session`
reads only the JSONL. If the JSONL is truncated / partial /
corrupt-mid-line, the manifest could provide a fallback view of the
launch fields.

**Why deferred:** the M1B contract is "the JSONL is the verification
surface." Cross-checking against the manifest is a second integrity
layer that wants its own design conversation (when do they disagree,
which wins, how does a verifier decide). Today they're written by
adjacent code paths so they should not disagree; M2A's resume contract
makes the manifest authoritative for runtime state.

**Trigger to land:** if a verifier ever observes JSONL/manifest
disagreement in production, OR when M2A's resume contract gives the
manifest a stronger ownership claim.

## 5. Process-local status-change dedup re-emits on restart

**Status:** by design; documented in the schema.

**What:** `compute_job_status_changed` deduplicates within one process
via an in-memory memo. After an agent restart, the next status poll
emits the current status with `status_previous: null` even when the
log already has a prior emission of the same status. The schema names
this trait so a verifier reading the log knows to expect it.

**Why deferred:** seeding the memo from the log tail on first observation
of a job in a process is a small piece of work (~20 LOC) but the user
explicitly preferred process-local for simplicity in the schema review.
Capturing here so that decision is rediscoverable.

**Trigger to land:** if restart noise becomes audible (a session that
restarts often producing many status-stays-same events). Quiet today
because sciagent sessions are long-running.

## 6. Compute.py touch is "frozen-but-extended for emission plumbing"  *(SETTLED 2026-04-29)*

**Status:** flagged at code-review time, user explicitly approved
("add the 3 lines and associated scope. not creeping."). Documented
here for M2A context, not as an open question.

**What:** `tools/atomic/compute.py` is on the M1A frozen-surface list,
but M1B added three lines to the `Job(...)` constructor call to
populate `session_id` / `intent` / `expected_artifacts` so the SkyPilot
backend can emit `compute_job_launched`. Same shape as the existing
`_write_session_manifest` side effect — internal plumbing, no change to
the LLM-facing schema.

**Why kept:** the alternative (a thread-local "launch context" compute.py
would have to write to) was the same scope-bend with more indirection.
Three explicit kwargs in the existing constructor call is honest.

**Note for M2A:** the frozen-surface boundary shifts when M2A's runtime
substrate lands; revisit then whether `compute.py` sits on the new
"frozen" list or on the runtime side.

## 7. `actor` field is optional and inconsistently populated

**Status:** schema-conformant; flag for cross-LLM forensics.

**What:** the schema declares `actor` optional. AgentLoop populates it
with `self.config.model` for `tool_call` / `tool_result`. The orchestrator
populates it with the verifier subagent's model name for LLM-gate
`verification_result`. Backend / framework events (compute_job_launched,
compute_job_status_changed, artifact_produced, DATA / EXEC gate
verification_result) omit it. A cross-LLM verifier doing forensics on
"which model emitted which event" sees the field on agent-driven
events only.

**Why this is here:** the schema review confirmed `actor` should stay
optional. If forensics needs it on more events, the cleanest path is
to make it required on every event type that has a clear "which model"
answer, and explicitly null-allowed elsewhere — that's a schema-bump
conversation.

**Trigger to land:** if a real cross-LLM forensics scenario surfaces a
gap.
