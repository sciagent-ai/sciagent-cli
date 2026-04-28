# M0 follow-ups (deferred from `m0-compute-fixes`)

These are gaps surfaced *during* M0 implementation that we deliberately did
not patch in M0 to keep the milestone tight. Each one is a "wrapper-too-thin"
issue (per v4.2 §C5 / scope-discipline rule). They get triaged into M1A,
M2A, or a small standalone PR after M0 merges.

## 1. `_build_task` ignores the registry's `workdir:` field

**Surfaced by:** B8 OpenFOAM smoke run #2 (job `sciagent-job-fe0e4e60`,
2026-04-28). The task ran with sky's default CWD (the cluster's user home),
so `bash Allrun` failed with `bash: Allrun: No such file or directory`
(return code 127) even though the case files were mounted at `/workspace`.

**Workaround in M0:** the B8 test passes `command="cd /workspace && bash Allrun"`
explicitly. Other callers must do the same when they expect to run from
the mount path.

**Proper fix (M2A or standalone):** in
`SkyPilotBackend._build_task`, read the service registry's `workdir:` field
and pass it to `sky.Task(workdir=...)` (or prepend a `cd` to the run
command). The registry already declares `workdir: /workspace` for the
openfoam* family; honoring it would make the OpenFOAM-style `bash Allrun`
invocation work without per-caller cd-prefixing.

**Why deferred:** registry inheritance through `extends:` is also currently
broken (see #2); fixing both at once is M2A-shaped (unified task model).

## 2. Service registry `extends:` chain not honored by `_get_service_resources`

**Surfaced by:** B8 OpenFOAM smoke. `openfoam-swak4foam-2012` extends
`openfoam-swak4foam` extends `openfoam`, but only `openfoam` declares
`resources: {min_memory_gb: 8, recommended_memory_gb: 32, min_cpus: 4}`.
The leaf service inherits *defaults*, not the parent's hints, so a bare
`compute_run(service="openfoam-swak4foam-2012", ...)` runs on c6i.large
(2 vCPUs / 8 GB) — an instance NP=8 MPI ranks would thrash on.

**Workaround in M0:** the B8 test overrides `cpus=8, memory_gb=16` explicitly.

**Proper fix (M2A or standalone):** walk the `extends:` chain in
`tools.atomic.compute._get_service_resources` and merge parent hints
under defaults but over leaf overrides. ~20-line change.

## 3. `bg_status` log-file path uses subprocess ProcessManager paths for compute jobs

Not actually broken, but mentioned for completeness: M2A's structured
logging story should unify how local (ProcessManager) and cloud
(SkyPilot) jobs surface `output_file` paths. Today they live in different
roots (`_logs/background_jobs/` vs `_logs/`).

---

This file is intentionally lightweight. Once M0 merges, these become
inputs to M2A's design conversation (or, if very small, to a one-shot
follow-up PR before M2A starts).
