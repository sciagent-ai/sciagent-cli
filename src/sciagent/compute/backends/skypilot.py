"""
SkyPilot compute backend for cloud GPU/large jobs.

Requires: pip install skypilot
Cloud credentials must be configured (aws configure, gcloud auth, etc.)
"""

from __future__ import annotations

import shlex
import tempfile
import time
from pathlib import Path
from typing import Optional, Dict, Any, Tuple

from ..job import Job, JobResult, JobStatus, ComputeRequirements, LaunchError
from ..task_index import read_task as _read_task_manifest
from ...provenance_log import get_provenance_log


# Default wall-clock budget for the fail-fast launch poll (B4). Picked to
# match v4.1 §2 B9's "structured error within 60 s" acceptance bar.
_LAUNCH_FAIL_FAST_BUDGET_SEC: float = 60.0
_LAUNCH_FAIL_FAST_POLL_SEC: float = 2.0


# Cloud URI prefix → sciagent store name. Restricted to schemes whose first
# path segment is unambiguously the bucket name. https:// (Azure blob) is
# excluded — extracting an Azure container from a URL is not a one-liner.
_CLOUD_URI_PREFIXES: Dict[str, str] = {
    "s3://": "s3",
    "gs://": "gcs",
    "r2://": "r2",
}


# sky.jobs.ManagedJobStatus -> sciagent JobStatus (v4.1 §1, M1A deliverable).
#
# Keyed by the Sky enum's *value* (the wire string Sky uses internally), not
# the Python member name, so this is robust to Sky renaming a member while
# keeping the same value (e.g. SUBMITTED -> DEPRECATED_SUBMITTED in 0.12.x).
# The original Sky string is preserved verbatim in JobResult.summary so debug
# paths don't lose the FAILED_* variant when we collapse to FAILED.
#
# Decisions:
#   - PENDING / SUBMITTED / STARTING all collapse to PENDING — the agent has
#     no actionable difference between them.
#   - CANCELLING is reported as RUNNING until terminal: the cancel may not
#     succeed, and reporting CANCELLED prematurely would mis-cue the agent.
#   - All FAILED_* variants collapse to FAILED. The variant lives in summary.
_SKY_STATUS_TO_JOB_STATUS: Dict[str, JobStatus] = {
    "PENDING":             JobStatus.PENDING,
    "SUBMITTED":           JobStatus.PENDING,
    "STARTING":            JobStatus.PENDING,
    "RUNNING":             JobStatus.RUNNING,
    "RECOVERING":          JobStatus.RECOVERING,
    "CANCELLING":          JobStatus.RUNNING,
    "SUCCEEDED":           JobStatus.COMPLETED,
    "CANCELLED":           JobStatus.CANCELLED,
    "FAILED":              JobStatus.FAILED,
    "FAILED_SETUP":        JobStatus.FAILED,
    "FAILED_PRECHECKS":    JobStatus.FAILED,
    "FAILED_NO_RESOURCE":  JobStatus.FAILED,
    "FAILED_CONTROLLER":   JobStatus.FAILED,
}


import contextlib as _contextlib
import io as _io
import logging as _logging


@_contextlib.contextmanager
def _silence_sky_chatter():
    """Suppress sky's rich-payload stdout AND its INFO-level logger output
    (the "Considered resources" table) for the duration of a sky API call.

    Both channels are non-load-bearing for sciagent — compute_run returns
    a structured dict that already names the chosen cloud, instance type,
    and cost. The chatter only litters the user's terminal between the
    agent's tool-call lines. Restored on context exit so failures during
    the call don't leave the system in a quiet state.
    """
    sky_logger = _logging.getLogger("sky")
    prev_level = sky_logger.level
    sky_logger.setLevel(_logging.WARNING)
    try:
        with _contextlib.redirect_stdout(_io.StringIO()):
            yield
    finally:
        sky_logger.setLevel(prev_level)


def _map_status(sky_status) -> JobStatus:
    """Map a sky.jobs.ManagedJobStatus to a sciagent JobStatus.

    Accepts either the enum member or its string value/name. Unknown states
    return JobStatus.FAILED — a future Sky upgrade that adds a state without
    a mapping is a *loud* failure, not a silent fall-through to RUNNING.
    Tests iterate the entire enum to catch additions before they ship.
    """
    if sky_status is None:
        return JobStatus.FAILED
    # Accept enum, str-by-value, or str-by-name (debug shapes).
    candidates = []
    value = getattr(sky_status, "value", None)
    if value is not None:
        candidates.append(str(value))
    name = getattr(sky_status, "name", None)
    if name is not None:
        candidates.append(str(name))
    if not candidates:
        candidates.append(str(sky_status))
    for key in candidates:
        if key in _SKY_STATUS_TO_JOB_STATUS:
            return _SKY_STATUS_TO_JOB_STATUS[key]
    return JobStatus.FAILED


def _parse_cloud_uri(uri: Optional[str]) -> Tuple[Optional[str], Optional[str]]:
    """Return (store, bucket) for a recognized cloud URI, else (None, None).

    Examples:
        s3://my-bucket           -> ("s3", "my-bucket")
        s3://my-bucket/case/foo  -> ("s3", "my-bucket")
        /local/path              -> (None, None)
        None                     -> (None, None)
    """
    if not uri or not isinstance(uri, str):
        return None, None
    for prefix, store in _CLOUD_URI_PREFIXES.items():
        if uri.startswith(prefix):
            rest = uri[len(prefix):]
            bucket = rest.split("/", 1)[0]
            if bucket:
                return store, bucket
            return None, None
    return None, None


class SkyPilotBackend:
    """Cloud compute via SkyPilot."""

    name = "skypilot"

    def __init__(self):
        self._sky = None  # Lazy import

    def _get_sky(self):
        """Lazy import of skypilot to avoid breaking if not installed."""
        if self._sky is None:
            try:
                import sky
                self._sky = sky
            except ImportError:
                raise RuntimeError(
                    "SkyPilot not installed. Run: pip install 'skypilot[aws]' "
                    "or 'skypilot[gcp]' or 'skypilot[azure]'"
                )
        return self._sky

    def is_available(self) -> bool:
        """Check if SkyPilot is installed and configured with cloud credentials."""
        try:
            sky = self._get_sky()
            # Check if any cloud is enabled for compute
            from sky.clouds import CloudCapability
            enabled_clouds = sky.check.get_cached_enabled_clouds_or_refresh(
                CloudCapability.COMPUTE
            )
            return len(enabled_clouds) > 0
        except Exception:
            return False

    def can_run(self, req: ComputeRequirements) -> bool:
        """SkyPilot can run anything if available."""
        return self.is_available()

    def get_enabled_store(self) -> str:
        """Get the storage type for the first enabled cloud."""
        try:
            sky = self._get_sky()
            from sky.clouds import CloudCapability
            enabled = sky.check.get_cached_enabled_clouds_or_refresh(
                CloudCapability.STORAGE
            )
            if not enabled:
                # Fall back to compute-enabled clouds
                enabled = sky.check.get_cached_enabled_clouds_or_refresh(
                    CloudCapability.COMPUTE
                )

            # Map cloud to store type
            cloud_to_store = {
                "AWS": "s3",
                "GCP": "gcs",
                "Azure": "azure",
                "Cloudflare": "r2",
            }
            for cloud in enabled:
                cloud_name = str(cloud).upper()
                for key, store in cloud_to_store.items():
                    if key.upper() in cloud_name:
                        return store
            return "s3"  # Default
        except Exception:
            return "s3"

    def get_workspace_mount(
        self,
        session_id: str,
        workspace_source: Optional[str] = None,
    ) -> "StorageMount":
        """Get a StorageMount for the session workspace bucket.

        Args:
            session_id: agent session id; used to derive the default bucket name.
            workspace_source: optional URI or local path passed to sky.Storage as
                `source`. When it is a recognized cloud URI (s3://bucket[/...]),
                the bucket name is taken from the URI so sky.Storage doesn't try
                to upload into a different bucket. Local paths fall back to the
                session-derived bucket name and get synced up by Sky on launch.
        """
        from ..job import StorageMount, StorageMode

        store_from_uri, bucket_from_uri = _parse_cloud_uri(workspace_source)
        if bucket_from_uri:
            bucket_name = bucket_from_uri
            store = store_from_uri
        else:
            bucket_name = f"sciagent-workspace-{session_id}"
            store = self.get_enabled_store()

        return StorageMount(
            path="/workspace",
            bucket=bucket_name,
            store=store,
            mode=StorageMode.MOUNT,
            source=workspace_source,
            persistent=True,
        )

    def run(self, job: Job, background: bool = True) -> Tuple[str, Optional[int]]:
        """Launch ``job`` as a SkyPilot managed job.

        Returns ``(name, managed_job_id)``:

          - ``name``: the human-readable identifier sciagent uses everywhere
            else (manifest filename, ``bg_status``, ``sky logs``). Same shape
            as M0's cluster_name, so callers that only care about the LLM-
            facing handle can ``name, _ = backend.run(job)``.
          - ``managed_job_id``: the integer Sky assigns to the managed job.
            Captured opportunistically when the controller acknowledges the
            launch inside the fail-fast budget. ``None`` when the launch is
            still in-flight after the budget elapses — callers can recover
            the integer later by name via ``_get_managed_job_record``.

        Raises:
            LaunchError: when Sky reports FAILED/CANCELLED inside the fail-
                fast budget (B4). Surfaced verbatim so callers can show a
                structured error rather than burning a long poll on a
                launch Sky already rejected.
        """
        sky = self._get_sky()

        task = self._build_task(job)
        name = f"sciagent-{job.id}"

        # sky.jobs.launch is async-first; the controller takes ownership
        # of cluster lifecycle, autostop, and recovery. None of cluster-
        # mode's down=/idle_minutes_to_autostop= apply here.
        #
        # Sky's optimizer phase runs synchronously inside .launch() and
        # writes <sky-payload>"<rich_*>"</sky-payload> markers (rich console)
        # plus a "Considered resources" table (Python logger) to the user's
        # terminal. None of that reaches the LLM (compute_run returns a
        # structured dict), but it litters the agent display. Silence both
        # channels — the interesting bits (cloud, instance type, cost) are
        # already in the cost_estimate dict the tool returns.
        with _silence_sky_chatter():
            request_id = sky.jobs.launch(task, name=name)

        # B4 fail-fast: bail out fast on a controller rejection (bad
        # image_id, missing creds, no capacity). The mechanism is the same
        # as M0 — sky.api_status polled within a 60s budget.
        succeeded = self._await_launch_or_fail(
            request_id=request_id,
            cluster_name=name,
            budget_sec=_LAUNCH_FAIL_FAST_BUDGET_SEC,
        )

        # If the request finished inside the budget we can sky.get the
        # payload without blocking — it returns ``(job_ids, controller_handle)``
        # for managed jobs. Outside the budget we'd have to wait for the
        # controller, which can take minutes for cold provisioning; skip it
        # and let the manifest fill in managed_job_id on a later status query.
        managed_job_id: Optional[int] = None
        if succeeded:
            try:
                payload = sky.get(request_id)
            except Exception:
                payload = None
            if isinstance(payload, tuple) and payload:
                job_ids = payload[0]
                if job_ids:
                    try:
                        managed_job_id = int(job_ids[0])
                    except (TypeError, ValueError):
                        managed_job_id = None

        # M1B: emit a compute_job_launched event so a verifier can see what
        # actually went to the cluster. Records both command_original (what
        # the LLM passed) and command_resolved (what the backend rewrote it
        # to via cd-prepend + timeout-wrap) per m1a-followup #5. mount_path
        # comes from the first storage mount (also m1a-followup #5) so a
        # future service mounting at /data isn't quietly mis-attributed to
        # /workspace. Best-effort: log write failures must not break a
        # successful launch.
        self._emit_launched_event(job, name, managed_job_id)

        if not background:
            # Foreground: drain the request to completion. The controller
            # tail-logs to stdout via stream_and_get; swallow exceptions
            # because a job-level failure is reported through get_status,
            # not by raising here.
            try:
                sky.stream_and_get(request_id)
            except Exception:
                pass

        return name, managed_job_id

    def _emit_launched_event(
        self,
        job: Job,
        name: str,
        managed_job_id: Optional[int],
    ) -> None:
        """Emit a compute_job_launched event, best-effort.

        Skipped silently when no session_id is set on the Job (standalone
        callers without an agent context) or when log write fails — the
        cluster job is already running and the verification record is
        secondary to the launch's success.
        """
        session_id = getattr(job, "session_id", None)
        if not session_id:
            return
        try:
            log = get_provenance_log(session_id)
            storage_mounts = getattr(job.requirements, "storage", None) or []
            mount_path: Optional[str] = None
            mount_bucket: Optional[str] = None
            if storage_mounts:
                first = storage_mounts[0]
                mount_path = getattr(first, "path", None)
                mount_bucket = getattr(first, "bucket", None)
            log.emit_compute_job_launched(
                job_id=name,
                managed_job_id=managed_job_id,
                backend=self.name,
                service=job.service or None,
                image=job.image or None,
                command_original=job.command,
                command_resolved=self.resolve_command(job),
                mount_path=mount_path,
                mount_bucket=mount_bucket,
                requirements={
                    "cpus": job.requirements.cpus,
                    "memory_gb": job.requirements.memory_gb,
                    "gpus": job.requirements.gpus,
                    "gpu_type": job.requirements.gpu_type,
                    "timeout_sec": job.requirements.timeout_sec,
                },
                intent=getattr(job, "intent", None),
                expected_artifacts=getattr(job, "expected_artifacts", None),
            )
        except Exception:
            pass  # Best-effort.

    @staticmethod
    def _extract_launch_error_msg(payload, status_name: str, cluster_name: str) -> str:
        """Pick the most useful error string from a sky api_status payload.

        Sky stores empty payload fields as the literal JSON string ``"null"``
        in the request DB; reading them back you get the four-character
        string, not Python ``None``. Treat ``None`` / ``""`` / ``"null"``
        all as "no info" so we never surface a useless ``LaunchError("null")``.
        """
        def _meaningful(value) -> Optional[str]:
            if value is None:
                return None
            if not isinstance(value, str):
                value = str(value)
            stripped = value.strip()
            if not stripped or stripped.lower() == "null":
                return None
            return stripped

        return (
            _meaningful(getattr(payload, "status_msg", None))
            or _meaningful(getattr(payload, "error", None))
            or f"sky.launch {status_name.lower()} for cluster {cluster_name} "
               f"(no detail provided; check `sky api logs <request_id>`)"
        )

    def _await_launch_or_fail(
        self,
        request_id,
        cluster_name: str,
        budget_sec: float,
        poll_interval_sec: float = _LAUNCH_FAIL_FAST_POLL_SEC,
    ) -> bool:
        """Poll sky.api_status briefly; raise LaunchError on FAILED/CANCELLED.

        B4 fail-fast (v4.2 §C5): sky.stream_and_get has no timeout kwarg, so
        the audit-described mechanism doesn't exist. We use sky.api_status
        polling instead — non-blocking, returns the request's pre-execution
        state, and lets us bail out fast on rejection.

        Returns:
            True  if the request reached SUCCEEDED inside the budget (caller
                  can safely sky.get the payload without blocking).
            False if the budget elapsed with the request still in-flight
                  (legitimate long provisioning; caller proceeds with normal
                  status polling and recovers metadata later).

        Raises:
            LaunchError on FAILED/CANCELLED.
        """
        sky = self._get_sky()
        try:
            from sky.server.requests.requests import RequestStatus
        except Exception:
            # If the import surface drifts on a future Sky upgrade, fall back
            # to compare-by-name on the status enum. Better than crashing.
            RequestStatus = None  # type: ignore

        deadline = time.monotonic() + budget_sec
        while time.monotonic() < deadline:
            try:
                payloads = sky.api_status(request_ids=[request_id])
            except Exception:
                # Transient API hiccup — retry within the budget. Don't let
                # an api_status flake convert into a phantom LaunchError.
                time.sleep(poll_interval_sec)
                continue

            if payloads:
                payload = payloads[0]
                status = getattr(payload, "status", None)
                status_name = getattr(status, "name", None) or str(status)

                if status_name in ("FAILED", "CANCELLED"):
                    msg = self._extract_launch_error_msg(payload, status_name, cluster_name)
                    raise LaunchError(msg, cluster_name=cluster_name)

                if status_name == "SUCCEEDED":
                    return True

            time.sleep(poll_interval_sec)
        # Budget exceeded; treat as a still-launching cluster.
        return False

    @staticmethod
    def resolve_command(job: Job) -> str:
        """Apply the deterministic command rewrites the backend performs
        before launch: storage-mount handling, then timeout-wrap with GNU
        ``timeout`` when ``timeout_sec > 0``.

        Two storage-mount strategies, picked off ``mount.implicit``:

        * Explicit mount (``implicit=False``, e.g. registry service or
          caller passed ``workspace_source=``): the caller's data lives
          in the bucket. Prepend ``cd <mount> &&`` so the run-CWD is
          there, regardless of the image's WORKDIR (rcwa: /opt; openfoam:
          /opt/openfoam11 — without the cd, ``bash Allrun`` against a
          /workspace mount fails with "No such file or directory", as in
          the B8 #2 incident).

        * Implicit mount (``implicit=True``, the default-on workspace we
          attach so outputs survive cluster teardown and bg_wait can
          fetch): the caller's *script* is in the local CWD, which Sky's
          ``workdir=`` field rsynced to ~/sky_workdir/. Cd-into-mount
          would defeat that (script not present at /workspace). Instead,
          inject a tiny prologue that creates the mount's _outputs/ dir
          and symlinks it into the workdir CWD. The user's script can
          then write to relative ``_outputs/foo.txt`` and the bytes land
          in the persistent bucket — bg_wait pulls them back.

        Idempotent against callers that already cd themselves: if the
        command already starts with ``cd ``, we trust them and skip both
        rewrites.

        Extracted so M1B's compute_job_launched event can record exactly
        what the cluster will run (``command_resolved``) alongside the
        original LLM-issued string (``command_original``).
        """
        run_command = job.command
        storage_mounts = getattr(job.requirements, "storage", None) or []
        if storage_mounts and not run_command.lstrip().startswith("cd "):
            mount = storage_mounts[0]
            mount_path = mount.path
            if mount_path:
                if getattr(mount, "implicit", False):
                    # Implicit: keep workdir CWD, symlink ./_outputs/ to a
                    # *job-specific* prefix in the mount. Per-job keying is
                    # the universal layout — single jobs get one extra path
                    # segment (small ergonomic cost), parallel sweeps are
                    # collision-free by construction (each job's job_id is
                    # unique). Bg_wait's auto-fetch pulls the matching
                    # prefix back to ./_outputs/<job_id>/ locally.
                    #
                    # Cross-tool sharing: Job 2 reads Job 1's outputs via
                    # absolute path /workspace/_outputs/<job_1_id>/... — the
                    # agent has job_1_id from the prior launch result.
                    quoted_mount = shlex.quote(mount_path)
                    quoted_job_id = shlex.quote(job.id or "default")
                    run_command = (
                        f"mkdir -p {quoted_mount}/_outputs/{quoted_job_id} && "
                        f"ln -sfn {quoted_mount}/_outputs/{quoted_job_id} ./_outputs && "
                        f"{run_command}"
                    )
                else:
                    # Explicit: cd into the mount as before. Caller's data
                    # lives there (registry service, workspace_source=);
                    # parallel collisions are the caller's responsibility.
                    run_command = f"cd {shlex.quote(mount_path)} && {run_command}"

        timeout_sec = getattr(job.requirements, "timeout_sec", 0) or 0
        if timeout_sec > 0:
            run_command = (
                f"timeout {int(timeout_sec)} bash -c {shlex.quote(run_command)}"
            )
        return run_command

    def _build_task(self, job: Job):
        """Build SkyPilot Task object."""
        sky = self._get_sky()

        # Build resources
        resources_kwargs = {
            "cpus": f"{job.requirements.cpus}+",
            "memory": f"{job.requirements.memory_gb}+",
        }

        # Add GPU if requested
        if job.requirements.gpus > 0:
            gpu_type = job.requirements.gpu_type or "A10G"
            resources_kwargs["accelerators"] = {gpu_type: job.requirements.gpus}

        # Add Docker image if specified
        if job.image:
            resources_kwargs["image_id"] = f"docker:{job.image}"

        resources = sky.Resources(**resources_kwargs)

        # M0 follow-up #1: cd into the workspace mount before running the
        # command. Sky's managed jobs run from the cluster user's home by
        # default, ignoring the image's WORKDIR directive — so without this,
        # ``bash Allrun`` against an /workspace mount fails with "No such
        # file or directory" (B8 #2 incident).
        #
        # We drive off the actual storage-mount path, NOT the registry's
        # ``workdir:`` hint, because the registry's hint and the mount path
        # can drift (registry says /workspace; a future caller mounts at
        # /data). The mount path is the only field guaranteed to point at
        # data the user just attached. Side benefits:
        #   - image-only callers with workspace_source= also get the cd
        #     (they'd be broken by a registry-driven approach since the
        #     registry isn't consulted without a service).
        #   - service-only callers without a mount keep the M0 default
        #     (Sky's home CWD), so images whose Dockerfile WORKDIR isn't
        #     /workspace (e.g. rcwa: /opt) don't regress.
        #
        # Idempotent against M0 cd-prefixed callers: if the command already
        # starts with ``cd ``, we trust the caller and don't double-prepend.
        # B6: enforce ComputeRequirements.timeout_sec on-VM by wrapping the
        # user command with the GNU ``timeout`` utility.
        run_command = self.resolve_command(job)

        # Create task. Pass `workdir=` so SkyPilot rsyncs the caller's local
        # CWD up to the cluster's ~/sky_workdir/ before launch. Without this,
        # `compute_run("python hello.py", backend="skypilot")` fails because
        # hello.py never reaches the cluster — the agent then thrashes
        # (inline `python -c` workarounds, extra cloud jobs to cat outputs).
        # SkyPilot caps workdir at 250MB and honors .gitignore; bulk payloads
        # belong in a storage mount.
        task = sky.Task(
            name=job.id,
            run=run_command,
            workdir=job.working_dir,
        )
        task.set_resources(resources)

        # Add storage mounts if specified
        if job.requirements.storage:
            storage_mounts = self._build_storage_mounts(job.requirements.storage)
            task.set_storage_mounts(storage_mounts)

        return task

    def _build_storage_mounts(self, storage_mounts) -> Dict[str, Any]:
        """Build SkyPilot storage_mounts dict from StorageMount list."""
        sky = self._get_sky()
        file_mounts = {}

        for mount in storage_mounts:
            # Map mode
            mode_map = {
                "MOUNT": sky.StorageMode.MOUNT,
                "COPY": sky.StorageMode.COPY,
                "MOUNT_CACHED": sky.StorageMode.MOUNT_CACHED,
            }
            mode = mode_map.get(mount.mode.value, sky.StorageMode.MOUNT)

            # Map store type to StoreType enum
            stores = None
            if mount.store:
                store_type = getattr(sky.StoreType, mount.store.upper(), None)
                if store_type:
                    stores = [store_type]

            # Create SkyPilot Storage object with all params in constructor.
            # persistent=True keeps the bucket after the cluster is torn down,
            # which is what users expect for a workspace mount.
            storage = sky.Storage(
                name=mount.bucket,
                source=mount.source,
                stores=stores,
                mode=mode,
                persistent=mount.persistent,
            )

            file_mounts[mount.path] = storage

        return file_mounts

    def _build_task_yaml(self, job: Job) -> Dict[str, Any]:
        """Build SkyPilot task definition as YAML dict (for debugging/export)."""
        task = {
            "name": job.id,
            "resources": {
                "cpus": f"{job.requirements.cpus}+",
                "memory": f"{job.requirements.memory_gb}+",
            },
            "run": job.command,
        }

        # Add GPU if requested
        if job.requirements.gpus > 0:
            gpu_type = job.requirements.gpu_type or "A10G"
            task["resources"]["accelerators"] = f"{gpu_type}:{job.requirements.gpus}"

        # Add Docker image
        if job.image:
            task["resources"]["image_id"] = f"docker:{job.image}"

        return task

    def _get_managed_job_queue(self) -> list:
        """Snapshot of all managed jobs visible to the current user.

        sky.jobs.queue_v2 is the supported entry point; sky.jobs.queue is
        deprecated. The async API returns
        ``(records, total, status_counts, total_no_filter)`` — we drop the
        aggregates and hand callers the records list.
        """
        sky = self._get_sky()
        request_id = sky.jobs.queue_v2(refresh=False, skip_finished=False)
        payload = sky.stream_and_get(request_id)
        if isinstance(payload, tuple) and payload:
            return list(payload[0] or [])
        # Defensive: API drift or single-list return.
        if isinstance(payload, list):
            return payload
        return []

    def _get_managed_job_record(self, name: str):
        """Look up a managed job record by the human-readable name.

        Returns the ``ManagedJobRecord`` (attribute-access pydantic model)
        whose ``job_name`` matches, or ``None``. Sky permits multiple jobs
        with the same name historically; sciagent generates a UUID-derived
        name per launch, so the first match is the right one.
        """
        for record in self._get_managed_job_queue():
            rec_name = getattr(record, "job_name", None)
            if rec_name == name:
                return record
        return None

    def get_managed_job_id(self, name: str) -> Optional[int]:
        """Resolve the integer managed_job_id for a launched job, by name.

        Useful when ``run()`` returned ``managed_job_id=None`` because the
        launch was still in-flight when the fail-fast budget elapsed —
        ``compute_run`` then writes the manifest without an integer and
        the next status query learns it.
        """
        record = self._get_managed_job_record(name)
        if record is None:
            return None
        mid = getattr(record, "job_id", None)
        if mid is None:
            return None
        try:
            return int(mid)
        except (TypeError, ValueError):
            return None

    def get_status(self, job_id: str) -> JobResult:
        """Get managed-job status, mapped to sciagent's JobStatus.

        Single layer (no separate cluster-vs-job dance) because the managed-
        jobs controller owns cluster lifecycle. The original Sky enum name
        is preserved verbatim in ``JobResult.summary`` so callers debugging
        a FAILED_NO_RESOURCE vs FAILED_SETUP haven't lost the variant when
        we collapse to JobStatus.FAILED.

        M1B: emits a compute_job_status_changed event when the mapped
        status differs from the last value emitted in this process for
        ``job_id``. Dedup is process-local; the writer suppresses no-op
        repeats. Looking up session_id via the per-job manifest keeps
        the backend's signature (and the cross-backend router contract)
        unchanged.
        """
        try:
            record = self._get_managed_job_record(job_id)
        except Exception as exc:
            # Transient query failure: surface as PENDING so the next poll
            # retries cleanly. Same recovery shape PR #1's B1 fix established.
            return JobResult(
                status=JobStatus.PENDING,
                summary=f"querying job {job_id} ({type(exc).__name__})",
            )

        if record is None:
            return JobResult(
                status=JobStatus.FAILED,
                summary=f"Managed job {job_id} not found in queue",
            )

        sky_status = getattr(record, "status", None)
        sky_status_name = (
            getattr(sky_status, "name", None) or str(sky_status) if sky_status else "UNKNOWN"
        )
        mapped = _map_status(sky_status)
        summary = f"Job {sky_status_name} on {job_id}"

        managed_job_id = None
        try:
            mid = getattr(record, "job_id", None)
            if mid is not None:
                managed_job_id = int(mid)
        except (TypeError, ValueError):
            managed_job_id = None

        if mapped == JobStatus.FAILED:
            # Pull a log tail so the agent gets actionable stderr without a
            # second round-trip. Same shape as M0's failure path.
            error_logs = self.get_logs(job_id, tail=200)
            log_file = self._write_logs_to_file(job_id, error_logs)
            error_preview = self._extract_error_line(error_logs)
            failure_reason = getattr(record, "failure_reason", None) or ""
            if failure_reason and not error_preview:
                error_preview = failure_reason[:500]
            self._emit_status_changed_event(
                job_id=job_id,
                managed_job_id=managed_job_id,
                status=mapped,
                sky_status_raw=sky_status_name,
                error_preview=error_preview or None,
                log_file=log_file or None,
            )
            return JobResult(
                status=mapped,
                summary=summary,
                error_preview=error_preview,
                output_file=log_file,
            )

        self._emit_status_changed_event(
            job_id=job_id,
            managed_job_id=managed_job_id,
            status=mapped,
            sky_status_raw=sky_status_name,
        )
        return JobResult(status=mapped, summary=summary)

    def _emit_status_changed_event(
        self,
        *,
        job_id: str,
        managed_job_id: Optional[int],
        status: JobStatus,
        sky_status_raw: Optional[str],
        error_preview: Optional[str] = None,
        log_file: Optional[str] = None,
    ) -> None:
        """Best-effort emission of compute_job_status_changed.

        session_id comes from the per-job manifest (~/.sciagent/tasks/<job_id>.json)
        which compute.py wrote at launch time. When the manifest is absent
        (orphan jobs, foreign launches) we skip emission silently — there's
        no log to write to without a session.
        """
        try:
            manifest = _read_task_manifest(job_id)
            if not manifest:
                return
            session_id = manifest.get("session_id")
            if not session_id:
                return
            log = get_provenance_log(session_id)
            log.emit_compute_job_status_changed(
                job_id=job_id,
                managed_job_id=managed_job_id,
                status=status.value,
                sky_status_raw=sky_status_raw,
                error_preview=error_preview,
                log_file=log_file,
            )
        except Exception:
            pass  # Best-effort; never break a status query on a log write.

    def get_job_status(self, job_id: str) -> JobResult:
        """Backwards-compatible alias.

        M0 split the cluster-vs-job query in two; managed jobs collapse it
        into a single ``get_status`` call. Existing callers that ask for
        ``get_job_status`` still get the expected JobResult shape.
        """
        return self.get_status(job_id)

    def estimate_cost(self, job: Job, duration_hours: float = 1.0) -> Dict[str, Any]:
        """Estimate cost for running job.

        Args:
            job: The job to estimate cost for
            duration_hours: Estimated duration in hours

        Returns:
            Dict with cost estimation details
        """
        sky = self._get_sky()

        try:
            # Build resources for cost lookup
            task = self._build_task(job)

            # Use optimizer to find cheapest resources
            # This returns (best_resources, cheapest_resources) per cloud
            dag = sky.Dag()
            dag.add(task)

            # Get cost estimate from optimizer. Same chatter as sky.jobs.launch
            # — silence it (the structured return below is what compute_run
            # actually consumes).
            optimizer = sky.Optimizer()
            with _silence_sky_chatter():
                optimized = optimizer.optimize(dag)

            # Extract cost info from optimized task
            if optimized and optimized.tasks:
                opt_task = optimized.tasks[0]
                resources = opt_task.best_resources
                if resources:
                    # get_cost() returns cost per SECOND, multiply by 3600 for hourly
                    cost_per_sec = resources.get_cost(1.0)
                    hourly = cost_per_sec * 3600
                    return {
                        "estimated_hourly": round(hourly, 2),
                        "estimated_total": round(hourly * duration_hours, 2),
                        "duration_hours": duration_hours,
                        "cloud": str(resources.cloud),
                        "instance_type": resources.instance_type,
                        "accelerators": str(resources.accelerators) if resources.accelerators else None,
                        "region": resources.region,
                    }

        except Exception as e:
            pass

        # Fallback: rough estimates based on requirements
        base_hourly = 0.10  # Base CPU cost
        if job.requirements.gpus > 0:
            gpu_costs = {
                "A10G": 1.00,
                "A100": 3.50,
                "V100": 2.50,
                "T4": 0.50,
                "L4": 0.80,
            }
            gpu_type = job.requirements.gpu_type or "A10G"
            base_hourly = gpu_costs.get(gpu_type, 1.50) * job.requirements.gpus

        return {
            "estimated_hourly": round(base_hourly, 4),
            "estimated_total": round(base_hourly * duration_hours, 4),
            "duration_hours": duration_hours,
            "cloud": "unknown",
            "gpu_type": job.requirements.gpu_type or "A10G" if job.requirements.gpus else None,
            "note": "Rough estimate - install skypilot for accurate pricing",
        }

    def cleanup(self, job_id: str, purge: bool = False) -> bool:
        """Cancel a managed job by name.

        For managed jobs the controller owns cluster teardown — cancelling
        the job is what stops billing. ``purge`` is preserved on the
        signature so existing callers don't break, but it's a no-op (no
        per-cluster record to purge in managed-jobs mode).
        """
        sky = self._get_sky()
        try:
            request_id = sky.jobs.cancel(name=job_id)
            sky.stream_and_get(request_id)
            return True
        except Exception:
            return False

    def stop(self, job_id: str) -> bool:
        """Stop is not meaningful for managed jobs — cancellation is.

        Kept for interface compatibility; routes to ``cleanup`` so a caller
        that calls ``stop`` followed by ``cleanup`` doesn't double-fail.
        """
        return self.cleanup(job_id)

    def get_logs(self, job_id: str, tail: int = 100) -> str:
        """Get tail of a managed job's logs.

        Uses ``sky.jobs.tail_logs`` — the managed-jobs equivalent of M0's
        ``sky.tail_logs``. ``follow=False`` is mandatory: a follow=True call
        blocks until the job ends, which would freeze the agent loop on a
        long-running case. ``output_stream=`` captures into an in-memory
        buffer so the function returns once the tail is buffered, not at
        end-of-time.

        Args:
            job_id: Managed-job name (the value passed to ``sky.jobs.launch(name=...)``).
            tail: Number of trailing log lines to return.

        Returns:
            Log content as a string; never None — callers (notably
            ``get_status`` on FAILED) write the return value straight to
            the saved log file used by ``bg_status``.
        """
        sky = self._get_sky()
        import io

        buf = io.StringIO()
        try:
            sky.jobs.tail_logs(
                name=job_id,
                job_id=None,
                follow=False,
                tail=tail,
                output_stream=buf,
            )
            return buf.getvalue()
        except Exception as e:
            partial = buf.getvalue()
            err_line = f"[get_logs error: {type(e).__name__}: {e}]"
            return f"{partial}\n{err_line}" if partial else err_line

    def _write_logs_to_file(self, job_id: str, logs: str) -> str:
        """Write logs to file for agent to read later.

        Args:
            job_id: Job/cluster identifier
            logs: Log content to write

        Returns:
            Path to the log file
        """
        log_dir = Path("_logs")
        log_dir.mkdir(exist_ok=True)
        log_file = log_dir / f"{job_id}.log"
        log_file.write_text(logs)
        return str(log_file)

    def _extract_error_line(self, logs: str, max_chars: int = 500) -> str:
        """Extract key error information from logs for preview.

        Looks for common error patterns and extracts relevant lines.
        Keeps it small for token efficiency.

        Args:
            logs: Full log content
            max_chars: Maximum characters for preview

        Returns:
            Extracted error preview
        """
        if not logs:
            return ""

        lines = logs.strip().split("\n")

        # Look for lines containing error indicators
        error_keywords = [
            "error:", "Error:", "ERROR:",
            "failed", "Failed", "FAILED",
            "exception", "Exception", "EXCEPTION",
            "fatal", "Fatal", "FATAL",
            "no matching manifest",  # Docker architecture errors
            "permission denied",
            "not found",
            "cannot",
        ]

        error_lines = []
        for line in lines:
            for keyword in error_keywords:
                if keyword in line:
                    error_lines.append(line.strip())
                    break

        if error_lines:
            # Return unique error lines, up to max_chars
            seen = set()
            unique_errors = []
            for line in error_lines:
                if line not in seen:
                    seen.add(line)
                    unique_errors.append(line)
            result = "\n".join(unique_errors)
            return result[:max_chars]

        # No error keywords found - return last few lines
        tail_lines = lines[-10:]
        result = "\n".join(tail_lines)
        return result[:max_chars]
