# M1A follow-ups (deferred from `m1a-managed-jobs`)

These are gaps surfaced *during* M1A implementation that we deliberately
did not patch in M1A to keep the milestone tight. Each one is either a
"wrapper-too-thin" issue (per v4.2 §C5 / scope-discipline rule) or a
deferred validation step.

## 1. Cross-LLM smoke test

**Status:** deferred per project direction (2026-04-29 conversation: "avoid
cross-llm for now..will test later").

**What the handoff originally required:**
`tests/llm/test_cross_provider_smoke.py` — a ~$0.01 invocation of a tiny
atomic-tool sequence (`compute_run` with `estimate_only=true`) against a
non-Claude provider via LiteLLM (`gpt-4o-mini` or `gemini-2.5-flash`),
gated behind `RUN_CROSS_LLM_TESTS=1`. Was listed in the M1A handoff as
"required to pass for milestone close."

**Why deferred here:** the runtime claim ("LLM-agnostic atomic tool surface,
LiteLLM in place") is unverified for non-Claude providers. The cost is
trivial (~$0.01 per run) but the user requested deferral until cross-LLM
testing happens as a deliberate batch.

**Trigger to land:** when the user runs the first cross-LLM session, OR
when M1B/M2A starts and we need confidence that schema changes haven't
silently regressed non-Claude tool-call shapes.

## 2. Re-validate the schema-shape change (Optional resource defaults) under cross-LLM

**Status:** ride along with #1.

**Why:** `89adb0e1` changed the JSON schemas for `compute_run`'s `cpus`,
`memory_gb`, `gpus`, and `gpu_type` from "default 2/4/0/T4" to *no
default* (Optional with None resolution). Under Claude this works
unchanged because Claude omits fields that have no caller-supplied value.
Other providers (OpenAI especially) sometimes synthesize JSON-schema-
default values verbatim — if they do, the registry hint resolution path
won't trigger and the LLM-driven calls will silently behave like the
M0-style explicit-default calls.

**Trigger to land:** part of #1's first non-Claude session. If a non-
Claude provider trips this, fall back to keeping the JSON schema's
`default: null` explicit (some validators treat absence and null
differently) and rely on `is None` in Python.

## 3. `ComputeRouter.run()` drops `managed_job_id` from SkyPilot's tuple return

**Status:** documented, not a bug; flag for M2A.

**Where:** `src/sciagent/compute/router.py::ComputeRouter.run`.

`SkyPilotBackend.run()` returns `(name, managed_job_id)` (M1A 3b);
`ComputeRouter.run()` returns just the name to keep the cross-backend
contract uniform with `LocalBackend.run() -> str`. ComputeTool today calls
`selected_backend.run(...)` directly to capture the integer for the
manifest write — it bypasses the router for that.

**M2A consideration:** when M2A's runtime substrate (`monitor.py`) calls
the backend directly to update `task_index`, it should call
`SkyPilotBackend.run` / `get_managed_job_id` (not `router.run`) so it
sees the integer. Document this in the M2A interface contract; today's
single caller (ComputeTool) already does the right thing.

## 4. Registry's `workdir:` field is currently advisory / redundant

**Status:** by design after the M1A 3c-revision; capture for future use.

**Where:** `src/sciagent/services/registry.yaml` declares `workdir: /workspace`
on every service. M1A initially read this field to drive an in-container
cd-prepend, but the registry-driven approach was unsafe in two ways:

  - drift between the registry hint and the actual storage-mount path
    (registry says `/workspace`, a future caller mounts at `/data` →
    cd-prepend goes to the wrong place);
  - missed the image-only-with-workspace-mount case (no service → no
    registry lookup → no cd-prepend even when the user mounted data).

The 3c revision drives cd off the **mount path** in `Job.requirements.storage`
instead. The registry's `workdir:` field is now only enforced by convention:
``get_workspace_mount`` happens to default the mount to `/workspace`, which
matches every service's registry declaration. Nothing in sciagent reads
`workdir:` at runtime today.

**Trigger to land:** when the project introduces a service that wants its
workspace mount at a non-`/workspace` path (e.g. an image whose tooling
expects `/data` or `/scratch`). The clean future state: have
`get_workspace_mount(service)` look up the service's registry workdir as
the default mount path. Single source of truth; both sides stay aligned.

**Until then:** the field is harmless but unused.

## 5. Lesson for M1B's provenance schema (record resolved AND original commands)

**Status:** design hint, not a deferred item.

The 3c revision means the backend rewrites the LLM's command before
running it (cd-prepend, timeout-wrap). When M1B lands the durable
provenance log, each cloud-job-launched event should record **both**:

  - ``command_original``: the command as the LLM/atomic-tool layer passed it.
  - ``command_resolved``: the command the backend actually ran on the cluster.

A cross-LLM verifier reading the log needs to see what the agent intended
vs. what was executed. They diverge by mechanical, deterministic rules
(cd, timeout) but the divergence is real and worth surfacing — especially
if a verification step needs to attribute a failure to "agent's logic" vs
"backend wrapping."

## 6. Lesson for M2A's watch_index (paths reference the mount, not /workspace)

**Status:** design hint, not a deferred item.

Watches of kind `artifact_present` (per v4.4 §5.2) target paths inside
the cluster filesystem. After the 3c revision, the mount path is the
canonical anchor, not `/workspace`. M2A's watch records should express
artifact paths *relative to the mount* (or use the actual mount path as
recorded in the task_index manifest), so a future service mounting at
`/data` doesn't quietly miss its artifacts.

## 7. `bg_kill`'s `force=True` is silently ignored for managed jobs

**Status:** minor wrapper-too-thin; flag for M2A or doc-only fix.

**Where:** `src/sciagent/tools/atomic/bg_tools.py::BgKillTool`.

The schema declares `force: bool = false` with the description "Use
SIGKILL instead of SIGTERM (immediate termination, local jobs only)".
For cloud jobs the cancel path is `sky.jobs.cancel(name=...)`, which
doesn't expose a forceful variant — the kwarg is simply ignored. Today's
description says "local jobs only" which is correct, but the description
could be clearer about what happens for cloud (silent no-op vs. error).

**Trigger to land:** if a user reports confusion. Otherwise leave for
M2A's bg-tools sweep.
