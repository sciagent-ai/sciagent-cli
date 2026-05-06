---
layout: default
title: Cloud Compute
nav_order: 5
---

# Cloud Compute

SciAgent runs scientific simulations on cloud clusters via [SkyPilot](https://skypilot.readthedocs.io/), with a local Docker fallback for small jobs. The same tool surface (`compute_run`, `compute_exec`, `compute_cluster`, `materialize`, `materialize_workspace`) covers both backends — the agent picks the right one based on the job's resource needs.

This page covers the user-facing surface. For internals, see [Architecture → SkyPilot integration](developers/architecture.md#skypilot-integration--cluster-lifecycle).

## When SkyPilot vs local

The compute router (`src/sciagent/compute/router.py`) selects a backend per job:

| Backend | Used when |
|---------|-----------|
| **SkyPilot** | GPU requested, > 16 GB memory, > 8 CPUs, or `backend="skypilot"` explicitly |
| **Local Docker** | Small jobs that fit on the developer's machine |

Both code paths produce the same `JobResult` shape, so downstream agents and analysis don't branch on backend.

## Two execution modes

`compute_run` supports two modes:

- **Managed jobs** (`mode="job"`, default) — Sky launches a transient cluster, runs the command, tears the cluster down on completion. One-shot. Use for "run this and I'll come back when it's done."
- **Cluster mode** (`mode="cluster"`) — Sky launches a persistent cluster you can iterate against (`compute_exec` for follow-up commands, `compute_cluster(action="refresh_mounts")` to point it at new inputs). Use for case-file reproductions, multi-step pipelines, anything you'll probe interactively before the real run.

Cluster mode is the right default for scientific workflows — the cost of a cluster restart usually outweighs the cost of leaving it `stopped` between iterations.

## Cluster lifecycle: stop, not down

Two ways a cluster can leave the active set:

- **`stop`** — preserves the cluster's attached disk and identity. A subsequent `start` restarts it in seconds with the same name. Cheap. **This is the default end-of-task action.**
- **`down`** — destroys the cluster. The S3-backed workspace bucket survives, but on-cluster scratch is gone. **Use only for explicit cleanup or quota-driven cleanup.**

The agent's prompt enforces this — at the end of a compute task it stops, not downs. If you see `down` happening implicitly, that's a bug.

## The session workspace

Every cluster job auto-mounts a per-session durable bucket at `/workspace/`:

```
<cloud>://sciagent-workspace-<session_id>/
```

Where `<cloud>` is whichever provider the job runs on (`s3`, `gs`, `az`, `r2`, `oci`). The bucket survives cluster teardown — outputs persist beyond the cluster that produced them. This is the "data tier" that compute → analyze → verify all share.

**Two data tiers, two tools:**
- `materialize(uri=...)` — pull a specific URI or a job's outputs to local. Cloud-agnostic.
- `materialize_workspace(subpath=..., dest=...)` — pull (a slice of) the session workspace bucket to local. Pairs with the auto-mount; what you write to `/workspace/` in the cluster is reachable via this tool.

`materialize_workspace(list_only=True, subpath="<run-id>/")` is the cheap "what's in the bucket?" probe.

## Tools

### compute_run

Launch a containerized compute job.

```
compute_run(
    service="openfoam-swak4foam-2012",
    command="bash Allrun",
    mode="cluster",
    backend="skypilot",
    cluster_name="cfd-run-1",
    cpus=4,
    memory_gb=32,
    gpus=0,
    timeout_sec=3600,
)
```

Returns: `{job_id, cluster_name, cluster_job_id, backend, ...}`. Background by default — poll with `bg_status` / `task_get` or block with `bg_wait` / `task_wait`.

**Service registry** — pass `service="<name>"` to use a registered scientific image (see `src/sciagent/services/registry.yaml`). Or pass a raw `image="..."` for an arbitrary container.

**Commit gate** — when the optimizer's estimated total exceeds `$5.00` (override via `SCIAGENT_COMPUTE_COMMIT_THRESHOLD_USD` or `~/.sciagent/config.yaml`), the tool prompts the user with a Sky-optimizer menu before launching. Tool-layer gate; the LLM cannot bypass it.

### compute_exec

Run a follow-up command on a warm cluster.

```
compute_exec(cluster_name="cfd-run-1", command="postProcess -func writeCellVolumes")
```

Cluster-mode equivalent of "I want to run another step against the same data without spinning up a new cluster." Returns a `cluster_job_id` you can wait on with `compute_cluster(action="wait_for_job")`.

### compute_cluster

Cluster lifecycle, action-dispatched:

| Action | Effect |
|--------|--------|
| `status` | Sky cluster status (UP/STOPPED/INIT/AUTOSTOPPING/PENDING) + sciagent local manifest |
| `wait_until_up` | Block within one LLM turn until the cluster reaches UP (default 300 s) |
| `wait_for_job` | Block until a `cluster_job_id` reaches terminal state (cluster-mode `bg_wait`) |
| `logs` | Tail of a cluster-mode job's stdout; on-disk cache fallback for post-autostop forensics |
| `stop` | Preserve the cluster (non-destructive). **Default end-of-task action.** |
| `start` | Restart a stopped cluster reusing its disk |
| `down` | **Destroy** the cluster — explicit cleanup only |
| `autostop` | Update idle threshold / wait_for / hook |
| `refresh_mounts` | Re-sync `file_mounts` via `sky launch --no-setup` (Sky's canonical "point a warm cluster at new input data") |

```
compute_cluster(action="status", cluster_name="cfd-run-1")
compute_cluster(action="stop", cluster_name="cfd-run-1")
```

### materialize

Pull outputs from cloud storage to local.

```
materialize(uri="s3://sciagent-workspace-abc123/run-001/fields/", dest="./_outputs/fields/")
materialize(uri="s3://sciagent-workspace-abc123/run-001/", list_only=True)
```

Cloud-agnostic — works with `s3://`, `gs://`, `az://`, `r2://`, `oci://`.

### materialize_workspace

Pull the session workspace bucket (or a slice) to local.

```
materialize_workspace(subpath="run-001/derived/", dest="./_outputs/derived/")
materialize_workspace(list_only=True)
```

Pairs with the cluster auto-mount: anything written to `/workspace/<path>` from a cluster job is reachable via `materialize_workspace(subpath="<path>")` from the agent's local environment.

## Configuration

### Install with cloud extras

```bash
pip install '.[cloud]'        # AWS
pip install '.[cloud-gcp]'    # GCP
pip install '.[cloud-azure]'  # Azure
pip install '.[cloud-all]'    # All three
```

The base install ships without SkyPilot — the `cloud*` extras pull it in.

### Cloud credentials

SciAgent inherits whatever credentials SkyPilot can find. Set up your provider once with the SkyPilot-supported flow (`aws configure`, `gcloud auth application-default login`, `az login`) and `sky check` will confirm.

### Tunables

| Knob | Default | Purpose |
|------|---------|---------|
| `SCIAGENT_COMPUTE_COMMIT_THRESHOLD_USD` | `5.0` | Estimated total ($) above which `compute_run` prompts before launching |
| `~/.sciagent/config.yaml` `compute.commit_threshold_usd` | — | Same gate, persisted in config |
| `compute_cluster(action="autostop", idle_minutes=N)` | provider default | How long a cluster sits idle before auto-stopping |

## End-to-end example

The [Datacenter CFD case study](case-studies/datacenter-cfd.md) is the canonical end-to-end use of cloud compute: spin up a SkyPilot cluster, run OpenFOAM, materialize the result fields locally, KDE-analyze, stop the cluster. Read it for the full pattern.

## See also

- [Tools reference](tools.md) — full signature for every compute tool
- [Task orchestration](task-orchestration.md) — `task_index`, background subagents, the unified runtime registry
- [Provenance log schema](provenance_log_schema.md) — `compute_job_launched`, `compute_job_status_changed` events
- [Architecture → SkyPilot integration](developers/architecture.md#skypilot-integration--cluster-lifecycle)
