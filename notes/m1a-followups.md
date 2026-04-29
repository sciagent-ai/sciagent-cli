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

## 4. `bg_kill`'s `force=True` is silently ignored for managed jobs

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
