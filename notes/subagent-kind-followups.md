# PR4 (`kind="subagent"`) follow-ups

Deferred items captured during the four-commit subagent-kind PR. Each is
either out-of-scope for the in-flight registry's first non-compute kind or
a "wrapper-too-thin" point that wants a real owner before shipping.

## 1. Startup reaper for stale-running subagent manifests

**What:** when sciagent restarts with a `kind=subagent, state=running`
manifest left over (parent process exited mid-run), the worker thread is
dead and the result is permanently lost. `task_wait` will time out
returning the stale snapshot; `task_list` shows it as still running
indefinitely.

**Proposed shape:** on `SubAgentOrchestrator.__init__` (or as a
standalone `task_index.reap_stale_subagent_manifests()` called once at
startup), scan `list_tasks(kind="subagent", state="running")`. For each
entry whose recorded `owner_pid` is not alive (or doesn't match the
current process and we're starting fresh), call
`update_task_state(id, "failed", result_summary="parent process exited
before completion")`. Mirrors B10's crash-recovery pattern for compute
jobs.

**Why deferred:** scope discipline — this PR is the first non-compute
kind; the reaper is its own observability/recovery feature. Cross-process
subagent persistence (which would actually rescue the in-flight work
rather than mark it failed) is bigger still.

## 2. `subagent_kill` / extending `bg_kill` to cancel a backgrounded subagent

**What:** today `bg_kill` on a `kind=subagent` task_id falls through
`kind_of`'s routing to the local `ProcessManager` path, which returns
"job not found" — correct but unhelpful UX. Real cancellation would set
the SubAgent's `parent_interrupt_event`, wait briefly for the worker
thread to exit, and call
`update_task_state(id, "cancelled", result_summary="user-cancelled")`.

**Open design question:** new `subagent_kill` tool vs. extending
`bg_kill`? Per the consolidation goal (kind-agnostic registry surface
where it makes sense, kind-specific runtime surface where it doesn't),
cancellation is somewhere in between. The interrupt-event mechanism is
subagent-specific; the manifest update is kind-agnostic. Argument for a
new `task_kill` (kind-agnostic, reads `kind` and dispatches to a per-kind
killer) — but that's a deeper refactor than this PR earned.

## 3. Migrate `compute_job` to authoritative `body[]` writes

**What:** PR4 step 1 added authoritative-`body[]` storage as the
precedent for new kinds. `compute_job` continues to write flat top-level
fields (`intent`, `expected_artifacts`, `command`, `image`, `service`,
`timeout_sec`); `_normalize` derives the `body` view from those flat
fields on read. New kinds (subagent today, watch / scheduled later)
write `body` directly.

**Why deferred:** the migration would touch
`compute.py._write_session_manifest` — a frozen surface in the
`feedback_frozen_surface_emission_plumbing.md` sense. The PR4 scope-bend
budget went toward `subagent.py` (the actual feature). Migration is a
pure refactor: write `body` instead of flat keys, update `_normalize` to
prefer authored body for compute_job too, update the body-derivation
tests to assert on the authored shape, and confirm `bg_status`'s
`_LOCAL_PASSTHROUGH_FIELDS` continue to surface the same display fields.
~1 day of work; no behavior change visible to the LLM.

## 4. Subagents observability for non-`general` subagents

**What:** of the registered subagents, only `general`
(`allowed_tools=None`, inherits all tools) sees `task_list` / `task_get`
/ `task_wait`. The `compute` subagent currently has explicit `bg_*`
tools but not `task_*`. Whether each existing subagent should be able to
observe sibling tasks (e.g. should `verifier` see other in-flight
subagents?) is a per-subagent prompt-design call, not code.

**Why deferred:** registry-config change, not code. Per the
`feedback_registry_is_domain_surface.md` memory, the right shape is to
update each `SubAgentConfig.allowed_tools` list when there's evidence a
subagent's task pattern would benefit from the surface — not a blanket
opt-in.

## 5. Race window in concurrent `is_nested=True` SubAgent construction

**What:** `SubAgent.__init__` in nested mode captures
`ComputeTool._shared_session_id` + `provenance_log._active_session_id`
(parent's), constructs `AgentLoop` (which CLOBBERS those globals with
the child's session), then restores the parent values. Two concurrent
nested constructions can race on the global. Today's `spawn_parallel`
already lives with this; PR4's background spawn does no worse (the
construction still happens on the parent thread before the worker
submits) but doesn't fix the underlying issue.

**Why deferred:** pre-existing concurrency hazard, not introduced by
PR4. Real fix is moving session id propagation off the global into a
parameter threaded through `AgentLoop.__init__`. ~1 PR worth of work,
worth doing alongside the consolidation refactor's "in-flight registry
as one store" cleanup.
