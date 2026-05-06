---
layout: default
title: Tools
nav_order: 4
---

# Tools

SciAgent uses tools to interact with files, run commands, query the web, manage long-running work, and orchestrate cloud compute. The agent automatically selects the right tool for each task.

The full tool registry is created in `src/sciagent/tools/registry.py:create_atomic_registry`. The lists below group tools by purpose.

---

## Core

### file_ops
Read/write/edit files. Supports image input (PNG, JPG, GIF, WebP) — the image is passed to the LLM for visual analysis.

```
file_ops(command="view", path="src/main.py")
file_ops(command="view", path="src/main.py", start_line=10, end_line=50)
file_ops(command="write", path="hello.py", content="print('Hello!')")
file_ops(command="str_replace", path="config.py", old_str="DEBUG = False", new_str="DEBUG = True")
file_ops(command="view", path="./plots/results.png")  # Image analysis
```

### bash
Execute shell commands with timeout handling. Long-running commands can be backgrounded with `background=True`.

```
bash(command="ls -la")
bash(command="npm install", timeout=180)
bash(command="python long_script.py", background=True)  # Returns a job_id
```

### search
Find files (glob) or content (grep).

```
search(command="glob", pattern="**/*.py")
search(command="grep", pattern="def main", include="*.py")
search(command="grep", pattern="TODO", path="./src")
```

### web
Search the web (Brave / DuckDuckGo) or fetch page content.

```
web(command="search", query="Python FastAPI tutorial 2024")
web(command="fetch", url="https://fastapi.tiangolo.com/tutorial/")
```

### todo
Track tasks with status and DAG dependencies. Supports artifact-tracking via `produces=` and verification via `verify=True`.

```
todo(command="add", content="Implement authentication")
todo(command="update", task_id="task_1", status="in_progress")
todo(command="list")
```

### ask_user
Request user input for decisions or confirmations.

```
ask_user(
    question="Which solver should I use?",
    options=["MEEP (FDTD)", "RCWA (faster for periodic)"],
    context="MEEP is general but slower."
)
```

### skill
Load specialized workflow definitions (SKILL.md). Auto-triggers based on task description.

```
skill(name="use-service")
skill(name="build-service")
skill(name="code-review")
```

---

## Compute

Cloud and local container job orchestration. See [Cloud Compute](cloud-compute.md) for the full guide.

### compute_run
Launch a containerized compute job. Background by default; the agent picks SkyPilot or local Docker based on requirements.

```
compute_run(
    service="openfoam-swak4foam",
    command="bash Allrun",
    mode="cluster",
    backend="skypilot",
    cluster_name="cfd-run-1",
    cpus=4,
    memory_gb=32,
)
```

Modes: `mode="job"` (managed job — Sky tears the cluster down) or `mode="cluster"` (persistent cluster you can iterate against).

Cost gate: when the optimizer's estimated total exceeds `$5.00`, the tool prompts the user before launching. Override via `SCIAGENT_COMPUTE_COMMIT_THRESHOLD_USD`.

### compute_exec
Run a follow-up command on a warm cluster.

```
compute_exec(cluster_name="cfd-run-1", command="postProcess -func writeCellVolumes")
```

### compute_cluster
Cluster lifecycle, action-dispatched. Default end-of-task is `stop` (preserves the disk for fast restart), **not** `down` (destroys the cluster).

```
compute_cluster(action="status", cluster_name="cfd-run-1")
compute_cluster(action="wait_until_up", cluster_name="cfd-run-1", timeout=300)
compute_cluster(action="wait_for_job", cluster_name="cfd-run-1", cluster_job_id=2, timeout=1800)
compute_cluster(action="logs", cluster_name="cfd-run-1", cluster_job_id=2, tail_lines=200)
compute_cluster(action="stop", cluster_name="cfd-run-1")
compute_cluster(action="start", cluster_name="cfd-run-1")
compute_cluster(action="autostop", cluster_name="cfd-run-1", idle_minutes=10)
compute_cluster(action="refresh_mounts", cluster_name="cfd-run-1")
compute_cluster(action="down", cluster_name="cfd-run-1")  # Destructive
```

### materialize
Pull cloud outputs to local. Cloud-agnostic (`s3://`, `gs://`, `az://`, `r2://`, `oci://`).

```
materialize(uri="s3://sciagent-workspace-abc/run-001/fields/", dest="./_outputs/fields/")
materialize(uri="s3://sciagent-workspace-abc/run-001/", list_only=True)
```

### materialize_workspace
Pull (a slice of) the per-session workspace bucket to local. Pairs with the `/workspace/` auto-mount on cluster jobs.

```
materialize_workspace(subpath="run-001/derived/", dest="./_outputs/derived/")
materialize_workspace(list_only=True)
```

---

## Task & background management

The `task_*` tools view the cross-kind in-flight registry. The `bg_*` tools own the per-cloud-job runtime surface (Sky-side status, log streaming, kill). See [Task Orchestration](task-orchestration.md).

### task_list
Enumerate tracked tasks across kinds (`compute_job`, `subagent`) and states.

```
task_list()                                         # everything
task_list(kind="compute_job", state="running")
task_list(kind="subagent", session_id="abc12345")
```

### task_get
Inspect a single task's full manifest.

```
task_get("sciagent-abc123")
```

### task_wait
Block until a task reaches a terminal state. Kind-agnostic.

```
task_wait("sciagent-abc123", timeout=1800, poll_interval=5)
```

### bg_status
Sky-side cloud-job status joined with sciagent's local manifest.

```
bg_status()                          # all jobs
bg_status(job_id="sciagent-abc123")  # one job
```

### bg_output
Stream output from a cloud job.

```
bg_output(job_id="sciagent-abc123", tail_lines=200)
```

### bg_wait
Block until a cloud job reaches a terminal state.

```
bg_wait(job_id="sciagent-abc123", timeout=1800)
```

### bg_kill
Cancel a running cloud job.

```
bg_kill(job_id="sciagent-abc123")
```

---

## Service discovery

### service_search
Case-insensitive keyword search across the service registry (`src/sciagent/services/registry.yaml`). Cheaper than reading the registry file directly.

```
service_search(keyword="openfoam")
service_search(keyword="bioinformatics")
```

### service_detail
Full details for a service — Dockerfile path, example, extends-chain.

```
service_detail(service="openfoam-swak4foam")
```

---

## Monitoring

### monitor
Spawn a watcher on a long-running subprocess. Each stdout line becomes an event delivered as a `<system-reminder>` on the next agent turn — no LLM round-trip per event.

```
monitor(command="tail -f solver.log", description="OpenFOAM solver progress")
```

### monitor_stop
Terminate a watcher.

```
monitor_stop(watcher_id="...")
```

---

## Verification

### verify_session
Snapshot read of a session's durable provenance log; produces a structured verification report. Cross-LLM friendly — any provider can audit a session it didn't run. See [Provenance Log Schema](provenance_log_schema.md).

```
verify_session(session_id="abc12345")
```

One-shot, non-blocking, snapshot semantics. There is no `wait=` or polling — invoke again to see a fresh snapshot.

---

## Creating custom tools

See [Configuration → Custom Tools](configuration.md#custom-tools) for adding your own tools to the registry.
