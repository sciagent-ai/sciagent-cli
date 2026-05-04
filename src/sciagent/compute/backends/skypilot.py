"""
SkyPilot compute backend for cloud GPU/large jobs.

Requires: pip install skypilot
Cloud credentials must be configured (aws configure, gcloud auth, etc.)
"""

from __future__ import annotations

import shlex
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Optional, Dict, Any, Iterable, List, Tuple, Union

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
    "oci://": "oci",
}

# sciagent store name → URI scheme used in the manifest's outputs_uri field
# and in compute_fetch's dispatch table. Internal representation only —
# Azure's actual storage URLs (https://<acct>.blob.core.windows.net/...)
# are richer than a single scheme can carry; the fetch path resolves the
# account from SkyPilot's config when dispatching to `az storage`.
_STORE_TO_URI_SCHEME: Dict[str, str] = {
    "s3": "s3",
    "gcs": "gs",
    "azure": "az",
    "r2": "r2",
    "oci": "oci",
}


# Always-on output mount: caller writes results to /outputs/<job_id>/ on
# the cluster (also exposed as $OUTPUTS_DIR). Auto-fetched on terminal
# status. Path is image-agnostic — never collides with image WORKDIRs or
# input mounts.
_OUTPUTS_MOUNT_PATH: str = "/outputs"

# Conventional input mount path. Single-string workspace_source= maps here
# for back-compat. Multi-mount callers can declare any path.
_DEFAULT_INPUT_MOUNT_PATH: str = "/workspace"


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


def _tail_n_lines(text: str, n: int) -> str:
    """Return the last ``n`` lines of ``text``. ``n<=0`` returns the full text."""
    if not text:
        return ""
    if n is None or n <= 0:
        return text
    lines = text.splitlines()
    if len(lines) <= n:
        return text
    return "\n".join(lines[-n:])


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
        gs://my-bucket/dir       -> ("gcs", "my-bucket")
        oci://my-bucket          -> ("oci", "my-bucket")
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
    # az:// is sciagent's internal scheme for Azure-mounted buckets in the
    # manifest. Real Azure URLs (https://<acct>.blob.core.windows.net/<container>/)
    # need account-level config from SkyPilot to resolve; we keep the
    # internal scheme uniform and resolve at fetch time.
    if uri.startswith("az://"):
        rest = uri[len("az://"):]
        bucket = rest.split("/", 1)[0]
        if bucket:
            return "azure", bucket
    return None, None


def _build_outputs_uri(store: str, bucket: str, prefix: str) -> str:
    """Build the manifest's outputs_uri from a store, bucket, and prefix.

    Internal scheme (s3/gs/az/r2/oci) chosen by store-type lookup. Used by
    compute_fetch.py to dispatch to the right cloud CLI without re-reading
    SkyPilot config.
    """
    scheme = _STORE_TO_URI_SCHEME.get(store, store)
    safe_prefix = prefix.lstrip("/")
    return f"{scheme}://{bucket}/{safe_prefix}"


def _pick_primary_input_mount(storage_mounts: Iterable["StorageMount"]):
    """Pick the run-CWD target from a list of storage mounts.

    Rules (image-agnostic):
      - Skip the always-on output mount (kind="output").
      - Prefer an input mount with path /workspace if present (the
        conventional default that string-form workspace_source= maps to).
      - Else, the first input mount in declaration order.
      - Else, None — no cd is prepended; CWD falls through to ship_workdir
        (~/sky_workdir/) if rsynced, else the image's WORKDIR.
    """
    inputs = [m for m in storage_mounts if getattr(m, "kind", "input") != "output"]
    if not inputs:
        return None
    for m in inputs:
        if m.path == _DEFAULT_INPUT_MOUNT_PATH:
            return m
    return inputs[0]


def _normalize_workspace_source(
    workspace_source,
) -> List[Dict[str, Optional[str]]]:
    """Normalize the polymorphic workspace_source= argument to a list of dicts.

    Accepted shapes:
      - None                      -> []
      - "" / empty                -> []
      - "<str>"                   -> [{"path": "/workspace", "source": "<str>"}]
      - [{"path": ..., "source": ...}, ...]   -> as-is (validated)
      - list of (path, source) tuples         -> normalized

    Raises ValueError for unrecognized shapes so the caller can surface a
    structured failure to the agent instead of letting a misuse mount
    silently with default values.
    """
    if workspace_source is None:
        return []
    if isinstance(workspace_source, str):
        if not workspace_source.strip():
            return []
        return [{"path": _DEFAULT_INPUT_MOUNT_PATH, "source": workspace_source}]
    if isinstance(workspace_source, dict):
        # Single dict — wrap into a list.
        workspace_source = [workspace_source]
    if not isinstance(workspace_source, (list, tuple)):
        raise ValueError(
            f"workspace_source must be a str, dict, or list of dicts; "
            f"got {type(workspace_source).__name__}"
        )
    out: List[Dict[str, Optional[str]]] = []
    for entry in workspace_source:
        if isinstance(entry, dict):
            path = entry.get("path") or _DEFAULT_INPUT_MOUNT_PATH
            source = entry.get("source")
            if not source:
                raise ValueError(
                    f"workspace_source entry missing 'source': {entry!r}"
                )
            out.append({"path": path, "source": source})
        elif isinstance(entry, (list, tuple)) and len(entry) == 2:
            path, source = entry
            out.append({"path": path or _DEFAULT_INPUT_MOUNT_PATH, "source": source})
        else:
            raise ValueError(
                f"workspace_source entry must be a dict with 'path' and "
                f"'source' (or a (path, source) tuple); got {entry!r}"
            )
    return out


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

    def build_outputs_mount(
        self,
        session_id: str,
        store_type: Optional[str] = None,
    ) -> "StorageMount":
        """Build the always-on output StorageMount at /outputs/.

        The bucket name is `sciagent-workspace-<session>` (provider-neutral
        — exists in whichever cloud backs the cluster). Store type
        precedence: explicit ``store_type=`` arg > cluster's enabled store.
        Never hardcoded.

        Marked ``kind="output"`` so the prologue logic skips it when
        picking a run-CWD target. Caller writes results to
        ``/outputs/<job_id>/`` (also exposed as ``$OUTPUTS_DIR``).
        Auto-fetched on terminal status.
        """
        from ..job import StorageMount, StorageMode

        store = store_type or self.get_enabled_store()
        bucket_name = f"sciagent-workspace-{session_id}"
        return StorageMount(
            path=_OUTPUTS_MOUNT_PATH,
            bucket=bucket_name,
            store=store,
            mode=StorageMode.MOUNT,
            source=None,
            persistent=True,
            kind="output",
        )

    def build_input_mounts(
        self,
        workspace_source,
        session_id: Optional[str] = None,
        store_type: Optional[str] = None,
    ) -> List["StorageMount"]:
        """Build a list of input StorageMounts from the polymorphic
        ``workspace_source`` argument.

        Accepted shapes (see ``_normalize_workspace_source``):
          - ``None`` / empty       → ``[]`` (no input mount)
          - ``"s3://..."`` (str)   → single mount at ``/workspace/`` (back-compat)
          - ``list[{"path","source"}]`` → one mount per entry, at the given paths

        Each mount's ``store`` is auto-detected from its source URI scheme;
        local paths fall back to the cluster's enabled store. ``store_type=``
        overrides at the call level (rare; intended for tests).
        """
        from ..job import StorageMount, StorageMode

        normalized = _normalize_workspace_source(workspace_source)
        if not normalized:
            return []

        fallback_store = store_type or self.get_enabled_store()
        mounts: List["StorageMount"] = []
        for entry in normalized:
            path = entry["path"]
            source = entry["source"]
            store_from_uri, bucket_from_uri = _parse_cloud_uri(source)
            if bucket_from_uri:
                bucket_name = bucket_from_uri
                store = store_from_uri
            else:
                # Local path or unrecognized URI: fall back to a per-session
                # bucket so SkyPilot can rsync the local source up.
                if session_id:
                    bucket_name = f"sciagent-workspace-{session_id}-input-{len(mounts)}"
                else:
                    bucket_name = f"sciagent-input-{len(mounts)}"
                store = fallback_store
            mounts.append(
                StorageMount(
                    path=path,
                    bucket=bucket_name,
                    store=store,
                    mode=StorageMode.MOUNT,
                    source=source,
                    persistent=True,
                    kind="input",
                )
            )
        return mounts

    def get_workspace_mount(
        self,
        session_id: str,
        workspace_source: Optional[str] = None,
    ) -> "StorageMount":
        """Back-compat shim for the legacy single-mount API.

        Older callers (and tests) ask for one workspace mount at
        ``/workspace/`` — input bucket if ``workspace_source`` is given,
        else a session-derived bucket. The new model splits inputs from
        outputs and supports multi-mount inputs; this method continues to
        return a single mount that mirrors the legacy shape so existing
        tests keep passing during the migration.

        New code should use :meth:`build_outputs_mount` and
        :meth:`build_input_mounts` directly.
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
            path=_DEFAULT_INPUT_MOUNT_PATH,
            bucket=bucket_name,
            store=store,
            mode=StorageMode.MOUNT,
            source=workspace_source,
            persistent=True,
            kind="input",
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

        # Canonicalize: from this point on, job.id IS the cluster name,
        # so the prologue (`mkdir -p /outputs/<job_id>`), the manifest's
        # outputs_uri, and the auto-fetch prefix all agree on one string.
        # Without this the prologue uses the raw uuid (job-abc) while the
        # manifest uses the sciagent-prefixed cluster name (sciagent-job-abc),
        # so user writes go to bucket prefix /job-abc/ but the fetcher looks
        # at /sciagent-job-abc/ and silently returns 0 files.
        if job.id and not job.id.startswith("sciagent-"):
            job.id = f"sciagent-{job.id}"
        name = job.id
        task = self._build_task(job)

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
        mode: str = "managed_jobs",
        cluster_name: Optional[str] = None,
    ) -> None:
        """Emit a compute_job_launched event, best-effort.

        Skipped silently when no session_id is set on the Job (standalone
        callers without an agent context) or when log write fails — the
        cluster job is already running and the verification record is
        secondary to the launch's success.

        ``mode`` distinguishes Sky's two execution surfaces. The default
        ``"managed_jobs"`` matches the existing call from ``run()``;
        cluster-mode call sites pass their specific mode so a verifier
        reading the log can tell whether the integer in ``managed_job_id``
        is a managed-jobs id or a per-cluster job index.
        """
        session_id = getattr(job, "session_id", None)
        if not session_id:
            return
        try:
            log = get_provenance_log(session_id)
            storage_mounts = getattr(job.requirements, "storage", None) or []
            # Record the primary input mount (the cd target). When no input
            # mount is declared, fall back to the always-on output mount
            # so the event still names a real bucket.
            mount_path: Optional[str] = None
            mount_bucket: Optional[str] = None
            primary = _pick_primary_input_mount(storage_mounts)
            if primary is not None:
                mount_path = primary.path
                mount_bucket = primary.bucket
            elif storage_mounts:
                fallback = storage_mounts[0]
                mount_path = getattr(fallback, "path", None)
                mount_bucket = getattr(fallback, "bucket", None)
            # For cluster modes, the int we have IS the per-cluster job id;
            # surface it under cluster_job_id explicitly so new readers don't
            # have to know that managed_job_id was overloaded.
            cluster_job_id = managed_job_id if mode != "managed_jobs" else None
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
                mode=mode,
                cluster_name=cluster_name or (name if mode != "managed_jobs" else None),
                cluster_job_id=cluster_job_id,
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

    # Words that mark "this is the actual reason" lines in sky controller
    # logs. Matched case-insensitively. Hits surface with a 2-line trailing
    # context window; on no hits we fall back to the last 30 lines.
    _LOG_SIGNAL_KEYWORDS = (
        "error",
        "failed",
        "denied",
        "exception",
        "fatal",
        "not found",
        "quota",
        "no matching manifest",
        "permission denied",
        "unauthorized",
        "no capacity",
        "no available",
    )

    @staticmethod
    def _filter_signal_lines(
        text: str,
        max_lines: int = 30,
        context_after: int = 2,
    ) -> str:
        """Pick error-keyword lines (with a small trailing context window)
        from a noisy log; fall back to the last ``max_lines`` if no keywords
        matched. Drops sky's retry chatter so the LaunchError surfaces the
        actual cause, not 30 "tried region X" lines.

        Bounded: never returns more than ``max_lines * 2`` lines.
        """
        keywords = SkyPilotBackend._LOG_SIGNAL_KEYWORDS
        lines = text.splitlines()
        n = len(lines)

        # Find indices of lines containing any signal keyword.
        hits: list = []
        for i, line in enumerate(lines):
            low = line.lower()
            for kw in keywords:
                if kw in low:
                    hits.append(i)
                    break

        if not hits:
            # No signal keyword anywhere — fall through to the tail.
            return "\n".join(lines[-max_lines:])

        # Build a window: each hit + next ``context_after`` lines, deduped.
        wanted: set = set()
        for idx in hits:
            for j in range(idx, min(idx + 1 + context_after, n)):
                wanted.add(j)

        # Cap total surfaced lines so a log full of warnings doesn't blow up.
        ordered = sorted(wanted)
        if len(ordered) > max_lines * 2:
            ordered = ordered[-(max_lines * 2):]
        return "\n".join(lines[i] for i in ordered)

    @staticmethod
    def _tail_sky_api_logs(
        request_id: Any,
        max_lines: int = 30,
        timeout: float = 10.0,
    ) -> str:
        """Shell out to ``sky api logs <request_id>`` and return the
        signal-filtered tail.

        The api_status payload's ``status_msg``/``error`` fields are routinely
        ``null`` even on rejection — the real reason lives in the controller
        logs that ``sky api logs`` exposes. This helper is the same dig the
        operator would do manually, surfaced automatically so the agent's
        LaunchError carries the actual reason instead of "(no detail provided)".

        Returns error-keyword lines + 2-line trailing context (signal-focused);
        falls back to a flat tail when no keywords matched.

        Best-effort: any failure returns "". Bounded by ``timeout`` and
        ``max_lines`` so a hung sky CLI never blocks the launch path.
        """
        if request_id is None:
            return ""
        rid = str(request_id)
        try:
            result = subprocess.run(
                ["sky", "api", "logs", rid],
                capture_output=True,
                text=True,
                timeout=timeout,
            )
        except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
            return ""
        # sky api logs writes the controller-side trace; both stdout and
        # stderr can carry signal depending on the sky version.
        combined = (result.stdout or "") + (result.stderr or "")
        if not combined.strip():
            return ""
        return SkyPilotBackend._filter_signal_lines(combined, max_lines=max_lines)

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

        # Pull the agent's interrupt event lazily so a Ctrl+C during the
        # 60s fail-fast budget wakes immediately instead of looping until
        # the next tick. Standalone callers (no AgentLoop wired) leave
        # the event as None and fall back to plain time.sleep.
        from sciagent.tools.registry import BaseTool
        interrupt_event = BaseTool._shared_interrupt_event

        deadline = time.monotonic() + budget_sec
        while time.monotonic() < deadline:
            if interrupt_event is not None and interrupt_event.is_set():
                # Treat user-cancellation as a structured launch rejection
                # so callers (compute_run / compute_exec / launch_cluster)
                # surface it cleanly instead of looping further.
                raise LaunchError(
                    f"sky.launch interrupted by user before terminal status "
                    f"(request_id={request_id}). The request may still be "
                    f"in-flight on Sky's controller — check `sky api status` "
                    f"or wait for autostop.",
                    cluster_name=cluster_name,
                    request_id=str(request_id) if request_id is not None else None,
                )
            try:
                payloads = sky.api_status(request_ids=[request_id])
            except Exception:
                # Transient API hiccup — retry within the budget. Don't let
                # an api_status flake convert into a phantom LaunchError.
                if interrupt_event is not None:
                    if interrupt_event.wait(poll_interval_sec):
                        continue
                else:
                    time.sleep(poll_interval_sec)
                continue

            if payloads:
                payload = payloads[0]
                status = getattr(payload, "status", None)
                status_name = getattr(status, "name", None) or str(status)

                if status_name in ("FAILED", "CANCELLED"):
                    msg = self._extract_launch_error_msg(payload, status_name, cluster_name)
                    # If sky reported FAILED with no usable status_msg/error,
                    # the real reason lives in `sky api logs <request_id>`.
                    # Pull the tail automatically so the agent gets the
                    # actual cause (image pull failure, capacity, auth, etc.)
                    # instead of an opaque "(no detail provided)" message.
                    if "no detail provided" in msg:
                        log_tail = self._tail_sky_api_logs(request_id)
                        if log_tail:
                            msg = (
                                f"sky.launch {status_name.lower()} for cluster "
                                f"{cluster_name} (request_id={request_id}). "
                                f"Controller log tail:\n{log_tail}"
                            )
                        else:
                            # Append the request_id even when log fetch fails
                            # so the operator can dig manually with one command.
                            msg = (
                                f"{msg.rstrip('.)')} request_id={request_id}; "
                                f"manually: sky api logs {request_id})"
                            )
                    raise LaunchError(
                        msg,
                        cluster_name=cluster_name,
                        request_id=str(request_id) if request_id is not None else None,
                    )

                if status_name == "SUCCEEDED":
                    return True

            # Wait until the next poll, but wake immediately on Ctrl+C
            # so the user doesn't have to sit through the rest of the
            # poll interval before bail-out.
            if interrupt_event is not None:
                if interrupt_event.wait(poll_interval_sec):
                    continue
            else:
                time.sleep(poll_interval_sec)
        # Budget exceeded; treat as a still-launching cluster.
        return False

    @staticmethod
    def resolve_command(job: Job) -> str:
        """Apply the deterministic command rewrites the backend performs
        before launch.

        Layered prologue (image-agnostic, applied in order, then the user
        command, then optionally wrapped in ``timeout``):

          1. ``mkdir -p /outputs/<job_id>`` — ensure the per-job output
             subdir exists inside the always-on output mount.
          2. ``export OUTPUTS_DIR=/outputs/<job_id>`` — ergonomic env var
             for the user command. Writing to ``$OUTPUTS_DIR/foo.txt``
             lands in the auto-fetched bucket.
          3. ``cd <primary input mount path>`` — only when an input mount
             is declared (caller passed ``workspace_source=``). Skipped
             entirely when there are no input mounts: CWD then falls
             through to ``ship_workdir`` (~/sky_workdir/) if rsynced, else
             the image's WORKDIR. Sciagent never invents a CWD.

        Idempotent against callers that already cd themselves: if the
        command already starts with ``cd ``, the cd step is skipped (the
        export and mkdir still apply — they're prerequisites for outputs).

        Extracted so M1B's compute_job_launched event can record exactly
        what the cluster will run (``command_resolved``) alongside the
        original LLM-issued string (``command_original``).
        """
        run_command = job.command
        job_id = job.id or "default"
        storage_mounts = getattr(job.requirements, "storage", None) or []

        prologue_parts: List[str] = [
            f"mkdir -p /outputs/{shlex.quote(job_id)}",
            f"export OUTPUTS_DIR=/outputs/{shlex.quote(job_id)}",
        ]

        # Conditionally cd into the primary input mount, but only if the
        # caller didn't already cd themselves.
        if not run_command.lstrip().startswith("cd "):
            primary = _pick_primary_input_mount(storage_mounts)
            if primary is not None and primary.path:
                prologue_parts.append(f"cd {shlex.quote(primary.path)}")

        run_command = " && ".join(prologue_parts + [run_command])

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

        # Layered prologue is built inside resolve_command:
        #   1. mkdir/export $OUTPUTS_DIR (always)
        #   2. cd into primary input mount (only when one is declared)
        #   3. timeout-wrap (when ComputeRequirements.timeout_sec > 0)
        # The image's WORKDIR is honored when no input mount is declared —
        # sciagent never invents a CWD.
        run_command = self.resolve_command(job)

        # Pass `workdir=` to sky.Task ONLY when the caller asked us to
        # ship a local directory (job.ship_workdir set). Default (None) →
        # no rsync, image WORKDIR is honored. SkyPilot caps workdir at
        # 250MB and honors .gitignore; bulk payloads belong in an input
        # storage mount.
        task_kwargs: Dict[str, Any] = {
            "name": job.id,
            "run": run_command,
        }
        ship_workdir = getattr(job, "ship_workdir", None)
        if ship_workdir:
            task_kwargs["workdir"] = ship_workdir
        task = sky.Task(**task_kwargs)
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

    # ------------------------------------------------------------------
    # Cluster-mode surface (sky.launch + sky.exec).
    #
    # Sky has two execution models. ``run()`` above uses managed-jobs
    # (sky.jobs.launch) — Sky owns cluster lifecycle, fresh cluster per
    # call. The methods below use cluster-mode (sky.launch / sky.exec) —
    # the agent owns lifecycle, clusters are persistent and reusable. Per
    # Sky's docs: managed-jobs is for batch / scale-out / spot-recovery;
    # cluster-mode is for iterative dev (probe → fix → retry on the same
    # warm cluster, ~10s per follow-up vs. ~3-5min per fresh provision).
    #
    # Both surfaces coexist; compute_run picks via mode= kwarg.
    # ------------------------------------------------------------------

    @staticmethod
    def _coerce_int_job_id(value) -> Optional[int]:
        """Cast a sky-returned job_id to int, tolerating str / None."""
        if value is None:
            return None
        try:
            return int(value)
        except (TypeError, ValueError):
            return None

    def _extract_cluster_job_id(self, request_id) -> Optional[int]:
        """sky.launch / sky.exec return RequestId[(Optional[int], Handle)].
        Pull the int job_id out without blocking longer than necessary —
        only call this AFTER _await_launch_or_fail has confirmed the
        request reached SUCCEEDED inside the fail-fast budget.
        """
        sky = self._get_sky()
        try:
            payload = sky.get(request_id)
        except Exception:
            return None
        if isinstance(payload, tuple) and payload:
            return self._coerce_int_job_id(payload[0])
        return None

    def launch_cluster(
        self,
        cluster_name: str,
        job: Job,
        autostop_minutes: int = 30,
        autostop_hook: Optional[str] = None,
        wait_for: str = "jobs",
    ) -> Tuple[str, Optional[int]]:
        """Provision (or reuse) a persistent cluster and run ``job`` on it.

        ``sky.launch(cluster_name=X)`` is **idempotent** on UP clusters: if
        cluster X is already UP, Sky skips reprovisioning and runs the task
        on the warm cluster. This is the contract sciagent leans on for
        warm-cluster iteration.

        Args:
            cluster_name: Persistent cluster identifier. Same name on
                subsequent calls reuses the cluster.
            job: Compute job to build the task from.
            autostop_minutes: Idle minutes before Sky auto-stops the
                cluster. Reset on every job submission (launch or exec).
                Default 30, matching Sky's interactive-dev guidance scaled
                for agent inter-step latency.
            autostop_hook: Optional shell snippet that runs on the cluster
                before autostop fires (e.g., ``aws s3 sync /scratch s3://...``
                to flush state). Set via a follow-up ``sky.autostop()`` call
                because ``sky.launch`` doesn't accept the hook directly.
            wait_for: Idle definition. ``"jobs"`` (default for sciagent —
                the agent never SSHes), ``"jobs_and_ssh"`` (Sky's default,
                for human dev), or ``"none"`` (hard timeout).

        Returns:
            ``(cluster_name, int_job_id)``. ``int_job_id`` is the
            per-cluster job index Sky assigns (1, 2, 3, ...); use it with
            ``sky.tail_logs(cluster_name, job_id=...)``. Returns
            ``(cluster_name, None)`` when launch is still in-flight after
            the fail-fast budget elapses (rare for warm-cluster launches).

        Raises:
            LaunchError: when Sky reports FAILED/CANCELLED inside the
                fail-fast budget. Same shape as ``run()``.
        """
        from ..cluster_manifest import write_cluster

        sky = self._get_sky()
        task = self._build_task(job)

        with _silence_sky_chatter():
            request_id = sky.launch(
                task,
                cluster_name=cluster_name,
                idle_minutes_to_autostop=autostop_minutes,
            )

        succeeded = self._await_launch_or_fail(
            request_id=request_id,
            cluster_name=cluster_name,
            budget_sec=_LAUNCH_FAIL_FAST_BUDGET_SEC,
        )

        int_job_id: Optional[int] = None
        if succeeded:
            int_job_id = self._extract_cluster_job_id(request_id)

        # Apply autostop hook (and refine wait_for) via a follow-up call
        # because sky.launch doesn't accept hook= directly. Best-effort:
        # a hook-set failure doesn't fail the launch.
        if autostop_hook or wait_for != "jobs_and_ssh":
            self._set_cluster_autostop(
                cluster_name=cluster_name,
                idle_minutes=autostop_minutes,
                wait_for=wait_for,
                hook=autostop_hook,
            )

        # Best-effort manifest write so subsequent compute_cluster(action=
        # "status") can enrich Sky's bare response with sciagent context.
        write_cluster(
            cluster_name=cluster_name,
            autostop_minutes=autostop_minutes,
            autostop_hook=autostop_hook,
            session_id=getattr(job, "session_id", None),
            service=job.service or None,
            image=job.image or None,
            last_job_id=int_job_id,
        )

        # Reuse the launched-event channel — verifier doesn't care whether
        # the launch went via jobs.launch or launch; the bucket / mounts /
        # command_resolved facts are identical. mode= distinguishes the
        # surface so the integer in managed_job_id isn't ambiguous.
        self._emit_launched_event(
            job, cluster_name, int_job_id,
            mode="cluster_launch", cluster_name=cluster_name,
        )

        return cluster_name, int_job_id

    def exec_on_cluster(
        self,
        cluster_name: str,
        job: Job,
    ) -> Tuple[str, Optional[int]]:
        """Run a follow-up job on an existing UP cluster via ``sky.exec``.

        Skips provisioning AND setup — only ships the workdir (if any) and
        runs the command. Returns in seconds, not minutes. The cluster's
        existing storage_mounts apply unchanged; to update mounts use
        :meth:`refresh_cluster_mounts`.

        Args:
            cluster_name: Existing UP cluster. If not UP, Sky raises and
                this method propagates a LaunchError pointing the caller
                at ``compute_cluster(action='status')``.
            job: Job to build the task from. Resources are ignored
                (the cluster's resources apply); workdir + run + envs
                go through.

        Returns:
            ``(cluster_name, int_job_id)``. ``int_job_id`` is the
            per-cluster index for this exec invocation.
        """
        from ..cluster_manifest import write_cluster

        sky = self._get_sky()
        task = self._build_task(job)

        with _silence_sky_chatter():
            request_id = sky.exec(task, cluster_name=cluster_name)

        succeeded = self._await_launch_or_fail(
            request_id=request_id,
            cluster_name=cluster_name,
            budget_sec=_LAUNCH_FAIL_FAST_BUDGET_SEC,
        )

        int_job_id: Optional[int] = None
        if succeeded:
            int_job_id = self._extract_cluster_job_id(request_id)

        write_cluster(
            cluster_name=cluster_name,
            session_id=getattr(job, "session_id", None),
            last_job_id=int_job_id,
        )

        self._emit_launched_event(
            job, cluster_name, int_job_id,
            mode="cluster_exec", cluster_name=cluster_name,
        )

        return cluster_name, int_job_id

    def refresh_cluster_mounts(
        self,
        cluster_name: str,
        job: Job,
    ) -> Tuple[str, Optional[int]]:
        """Re-sync ``file_mounts`` on an existing cluster without re-running
        ``setup``.

        Wraps ``sky.launch(no_setup=True, cluster_name=X)`` — Sky's
        canonical pattern for "iterate on data while reusing a cluster"
        per the syncing-code-artifacts docs. The new task's storage_mounts
        replace the cluster's prior mount set; the run command (if set)
        executes after the mount sync.

        Use case: agent has a warm cluster pointed at workspace_source A;
        wants to point it at workspace_source B without paying full
        reprovisioning. Setup is idempotent and expensive (often hours
        for compiled scientific stacks), so skipping it is the win.
        """
        from ..cluster_manifest import write_cluster

        sky = self._get_sky()
        task = self._build_task(job)

        with _silence_sky_chatter():
            request_id = sky.launch(
                task,
                cluster_name=cluster_name,
                no_setup=True,
            )

        succeeded = self._await_launch_or_fail(
            request_id=request_id,
            cluster_name=cluster_name,
            budget_sec=_LAUNCH_FAIL_FAST_BUDGET_SEC,
        )

        int_job_id: Optional[int] = None
        if succeeded:
            int_job_id = self._extract_cluster_job_id(request_id)

        write_cluster(
            cluster_name=cluster_name,
            session_id=getattr(job, "session_id", None),
            last_job_id=int_job_id,
        )

        self._emit_launched_event(
            job, cluster_name, int_job_id,
            mode="cluster_refresh_mounts", cluster_name=cluster_name,
        )

        return cluster_name, int_job_id

    def cluster_status(self, cluster_name: str) -> Dict[str, Any]:
        """Return a sciagent-shaped status dict for a cluster.

        Combines Sky's ``sky.status(cluster_names=[name])`` response with
        the local manifest (if present) so callers see both the cloud-side
        truth (UP/STOPPED/INIT/AUTOSTOPPING/PENDING) and sciagent context
        (created_at, autostop_minutes, last_used_at, recent job_ids).

        Returns:
            ``{
                "cluster_name": ...,
                "exists": bool,           # is the cluster known to Sky?
                "status": "UP" | "STOPPED" | ... | None,
                "autostop": {"idle_minutes": int, "down": bool} | None,
                "manifest": {...} | None, # local manifest content
            }``
        """
        from ..cluster_manifest import read_cluster

        sky = self._get_sky()
        manifest = read_cluster(cluster_name)

        try:
            request_id = sky.status(cluster_names=[cluster_name])
            payloads = sky.stream_and_get(request_id)
        except Exception as exc:
            return {
                "cluster_name": cluster_name,
                "exists": False,
                "status": None,
                "autostop": None,
                "manifest": manifest,
                "error": f"{type(exc).__name__}: {exc}",
            }

        if not payloads:
            return {
                "cluster_name": cluster_name,
                "exists": False,
                "status": None,
                "autostop": None,
                "manifest": manifest,
            }

        record = payloads[0]
        status_obj = getattr(record, "status", None)
        status_name = (
            getattr(status_obj, "name", None)
            or getattr(status_obj, "value", None)
            or str(status_obj) if status_obj else None
        )
        autostop_minutes = getattr(record, "autostop", None)
        to_down = bool(getattr(record, "to_down", False))
        autostop_block: Optional[Dict[str, Any]] = None
        if autostop_minutes is not None and autostop_minutes >= 0:
            autostop_block = {
                "idle_minutes": int(autostop_minutes),
                "down": to_down,
            }

        return {
            "cluster_name": cluster_name,
            "exists": True,
            "status": status_name,
            "autostop": autostop_block,
            "manifest": manifest,
        }

    def wait_cluster_up(
        self,
        cluster_name: str,
        timeout: float = 300.0,
        poll_interval: float = 5.0,
    ) -> Dict[str, Any]:
        """Block until the cluster reaches UP, hits a terminal-bad state, or
        ``timeout`` elapses. Returns a structured verdict so the caller
        knows whether to proceed (status == UP), bail (FAILED/STOPPED), or
        wait again (still INIT/PENDING after the budget).

        Architectural intent: collapse the agent's status-polling loop
        (which burns one LLM turn per snapshot) into a single tool call
        that internally polls. Each LLM turn that polls instead of
        waiting costs ~5–30s of thinking + tokens; for a 5-min provision
        that's 10+ turns. This wait collapses it to one.

        Returns:
            ``{"ready": bool, "status": str | None, "elapsed_sec": float,
              "timed_out": bool, "manifest": ...}``
            - ``ready=True, status="UP"`` — proceed; cluster is provisioned.
            - ``ready=False, timed_out=True`` — call again with longer
              timeout, or fall back to status snapshots.
            - ``ready=False, status="STOPPED"|"AUTOSTOPPING"`` — terminal-
              bad; agent should not exec on this cluster.

        Honors the BaseTool interrupt event so a user Ctrl+C wakes the
        wait immediately and returns a structured "interrupted" verdict.
        """
        from sciagent.tools.registry import BaseTool

        interrupt_event = BaseTool._shared_interrupt_event
        start = time.monotonic()
        deadline = start + timeout
        last_status: Optional[str] = None

        while time.monotonic() < deadline:
            if interrupt_event is not None and interrupt_event.is_set():
                return {
                    "ready": False,
                    "status": last_status,
                    "elapsed_sec": round(time.monotonic() - start, 1),
                    "timed_out": False,
                    "interrupted": True,
                    "reason": "user-interrupted",
                }

            info = self.cluster_status(cluster_name)
            last_status = info.get("status")

            if last_status == "UP":
                return {
                    "ready": True,
                    "status": "UP",
                    "elapsed_sec": round(time.monotonic() - start, 1),
                    "timed_out": False,
                    "manifest": info.get("manifest"),
                }

            if last_status in ("STOPPED", "AUTOSTOPPING"):
                # Terminal-bad: the cluster is going away. exec'ing on it
                # would fail; surface this so the caller doesn't waste a
                # follow-up call.
                return {
                    "ready": False,
                    "status": last_status,
                    "elapsed_sec": round(time.monotonic() - start, 1),
                    "timed_out": False,
                    "manifest": info.get("manifest"),
                    "reason": (
                        f"cluster reached {last_status} (not UP). Diagnose "
                        f"BEFORE relaunching — the cause typically replays. "
                        f"Run `bash sky api status` to find this launch's "
                        f"request_id (match by cluster_name + recent timestamp), "
                        f"then `bash sky api logs <request_id>` to read the "
                        f"provisioner's output (image pull, instance bring-up, "
                        f"setup script). This is where setup-phase errors live."
                    ),
                }

            # INIT / PENDING / unknown: keep waiting. Use the interrupt-
            # aware wait so a Ctrl+C wakes us right away.
            remaining = max(0.0, deadline - time.monotonic())
            interval = min(poll_interval, remaining)
            if interval <= 0:
                break
            if interrupt_event is not None:
                if interrupt_event.wait(interval):
                    continue
            else:
                time.sleep(interval)

        return {
            "ready": False,
            "status": last_status,
            "elapsed_sec": round(time.monotonic() - start, 1),
            "timed_out": True,
            "reason": (
                f"cluster {cluster_name} still {last_status} after "
                f"{timeout}s. If still progressing, call wait_until_up "
                f"again with a longer timeout. If suspiciously slow (>5 "
                f"min in INIT), inspect the provisioner: "
                f"`bash sky api status` to find this launch's request_id "
                f"(match by cluster_name + recent timestamp), then "
                f"`bash sky api logs <request_id>` to see image pull / "
                f"instance bring-up / setup script progress."
            ),
        }

    def wait_cluster_job(
        self,
        cluster_name: str,
        cluster_job_id: int,
        timeout: float = 1800.0,
        poll_interval: float = 10.0,
    ) -> Dict[str, Any]:
        """Block until a per-cluster job reaches a terminal state.

        Cluster-mode equivalent of ``bg_wait`` for managed-jobs. Polls
        ``sky.queue(cluster_name)`` for the job whose int id matches
        ``cluster_job_id`` and reports its mapped sciagent JobStatus
        when the job is terminal (COMPLETED / FAILED / CANCELLED).

        Returns:
            ``{"terminal": bool, "status": "COMPLETED"|"FAILED"|... | None,
              "elapsed_sec": float, "timed_out": bool, "summary": str | None}``

        Honors the BaseTool interrupt event.
        """
        from sciagent.tools.registry import BaseTool

        sky = self._get_sky()
        interrupt_event = BaseTool._shared_interrupt_event
        start = time.monotonic()
        deadline = start + timeout
        last_status_name: Optional[str] = None

        terminal_names = {
            "SUCCEEDED", "FAILED", "FAILED_SETUP", "FAILED_PRECHECKS",
            "FAILED_DRIVER", "CANCELLED",
        }

        while time.monotonic() < deadline:
            if interrupt_event is not None and interrupt_event.is_set():
                return {
                    "terminal": False,
                    "status": last_status_name,
                    "elapsed_sec": round(time.monotonic() - start, 1),
                    "timed_out": False,
                    "interrupted": True,
                    "reason": "user-interrupted",
                }

            try:
                request_id = sky.queue(cluster_name)
                records = sky.stream_and_get(request_id)
            except Exception:
                records = []

            match = None
            if records:
                for rec in records:
                    rec_id = getattr(rec, "job_id", None)
                    if rec_id is not None and int(rec_id) == int(cluster_job_id):
                        match = rec
                        break

            if match is not None:
                status_obj = getattr(match, "status", None)
                last_status_name = (
                    getattr(status_obj, "name", None)
                    or str(status_obj) if status_obj else None
                )
                if last_status_name in terminal_names:
                    summary = f"Job {cluster_job_id} on {cluster_name}: {last_status_name}"
                    # Cache the tail of the job's log to disk before
                    # returning. The cluster can autostop within minutes of
                    # a FAILED status, after which sky.tail_logs raises
                    # ClusterNotUpError and post-hoc forensics is
                    # impossible. Best-effort: a cache failure must not
                    # break the wait return.
                    try:
                        log_text = self._fetch_cluster_job_log_text(
                            cluster_name=cluster_name,
                            cluster_job_id=int(cluster_job_id),
                            tail_lines=1000,
                        )
                        if log_text:
                            from ..cluster_manifest import cache_job_log
                            cache_job_log(
                                cluster_name=cluster_name,
                                cluster_job_id=int(cluster_job_id),
                                log_text=log_text,
                                max_lines=1000,
                            )
                    except Exception:
                        pass
                    return {
                        "terminal": True,
                        "status": last_status_name,
                        "elapsed_sec": round(time.monotonic() - start, 1),
                        "timed_out": False,
                        "summary": summary,
                    }

            remaining = max(0.0, deadline - time.monotonic())
            interval = min(poll_interval, remaining)
            if interval <= 0:
                break
            if interrupt_event is not None:
                if interrupt_event.wait(interval):
                    continue
            else:
                time.sleep(interval)

        return {
            "terminal": False,
            "status": last_status_name,
            "elapsed_sec": round(time.monotonic() - start, 1),
            "timed_out": True,
            "reason": (
                f"cluster job {cluster_job_id} on {cluster_name} still "
                f"{last_status_name or 'unknown'} after {timeout}s; "
                f"call wait_for_job again with a longer timeout."
            ),
        }

    def _fetch_cluster_job_log_text(
        self,
        cluster_name: str,
        cluster_job_id: int,
        tail_lines: int = 200,
    ) -> str:
        """Live fetch of a cluster-mode job's stdout via sky.tail_logs.

        Wraps ``sky.tail_logs(cluster_name, job_id, follow=False, tail=N,
        output_stream=buf)`` so the buffered tail flows back as a string
        rather than printing to console. ``follow=False`` is mandatory
        (a follow=True call blocks until end-of-job).

        Raises sky.exceptions.* (ClusterNotUpError, ClusterDoesNotExist)
        directly — callers decide whether to fall back to the on-disk
        cache. Other exceptions are swallowed and surface as the partial
        buffer + a bracketed error marker (matching ``get_logs``).
        """
        sky = self._get_sky()
        import io

        buf = io.StringIO()
        try:
            sky.tail_logs(
                cluster_name=cluster_name,
                job_id=int(cluster_job_id),
                follow=False,
                tail=tail_lines,
                output_stream=buf,
            )
        except Exception as exc:
            # Re-raise the cluster-state exceptions so the caller can
            # fall back to cache. Everything else is wrapped so a
            # transient SDK glitch doesn't crash the agent.
            module = type(exc).__module__ or ""
            if module.startswith("sky."):
                raise
            partial = buf.getvalue()
            err_line = f"[tail_logs error: {type(exc).__name__}: {exc}]"
            return f"{partial}\n{err_line}" if partial else err_line
        return buf.getvalue()

    def tail_cluster_job_logs(
        self,
        cluster_name: str,
        cluster_job_id: int,
        tail_lines: int = 200,
    ) -> Dict[str, Any]:
        """Get the tail of a cluster-mode job's stdout, with cache fallback.

        Strategy:
          1. Try ``sky.tail_logs`` (live). If the cluster is UP, this is
             the freshest source and it's also written through to the
             on-disk cache so a subsequent call after autostop still works.
          2. On ``ClusterNotUpError`` / ``ClusterDoesNotExist`` /
             ``RuntimeError``, fall back to the cluster manifest's cached
             log (populated at terminal status by ``wait_cluster_job``).

        Returns ``{cluster_name, cluster_job_id, tail_lines, source,
        log_tail}`` where ``source`` is one of ``"live"``, ``"cached"``,
        or ``"missing"``. ``log_tail`` is empty string when source is
        ``"missing"``.
        """
        from ..cluster_manifest import cache_job_log, read_cached_job_log

        try:
            text = self._fetch_cluster_job_log_text(
                cluster_name=cluster_name,
                cluster_job_id=int(cluster_job_id),
                tail_lines=tail_lines,
            )
            # Refresh cache so a later call after autostop still works.
            cache_job_log(
                cluster_name=cluster_name,
                cluster_job_id=int(cluster_job_id),
                log_text=text,
                max_lines=1000,
            )
            return {
                "cluster_name": cluster_name,
                "cluster_job_id": int(cluster_job_id),
                "tail_lines": tail_lines,
                "source": "live",
                "log_tail": _tail_n_lines(text, tail_lines),
            }
        except Exception as live_exc:
            cached = read_cached_job_log(
                cluster_name=cluster_name,
                cluster_job_id=int(cluster_job_id),
            )
            if cached is None:
                return {
                    "cluster_name": cluster_name,
                    "cluster_job_id": int(cluster_job_id),
                    "tail_lines": tail_lines,
                    "source": "missing",
                    "log_tail": "",
                    "live_error": f"{type(live_exc).__name__}: {live_exc}",
                    "hint": (
                        "Cluster is not UP and no cached log exists. "
                        "Call compute_cluster(action='wait_for_job', ...) "
                        "BEFORE the cluster autostops to populate the "
                        "cache, or launch a `monitor` on `sky logs -f` "
                        "alongside the exec to capture logs in real time."
                    ),
                }
            return {
                "cluster_name": cluster_name,
                "cluster_job_id": int(cluster_job_id),
                "tail_lines": tail_lines,
                "source": "cached",
                "log_tail": _tail_n_lines(cached, tail_lines),
                "live_error": f"{type(live_exc).__name__}: {live_exc}",
            }

    def cluster_down(self, cluster_name: str, graceful: bool = True) -> bool:
        """Tear down a cluster. Returns True on success, False on error.

        ``graceful=True`` (default) gives in-flight jobs a chance to flush
        state before teardown. Best-effort: a torn-down cluster's manifest
        is removed so a stale entry doesn't surface in subsequent status
        listings.

        Emits a ``compute_cluster_down`` event into the session's
        provenance log so audits can see *when* and *why* a cluster died,
        not just that it's gone. The session_id is recovered from the
        cluster manifest (written at launch); without it the event is
        skipped silently — there's no log to write to.
        """
        from ..cluster_manifest import delete_cluster, read_cluster

        # Read the manifest BEFORE deleting it — we need session_id to
        # emit the down event into the right log.
        manifest = read_cluster(cluster_name)
        session_id = (manifest or {}).get("session_id")

        sky = self._get_sky()
        success = True
        reason: Optional[str] = None
        try:
            request_id = sky.down(cluster_name, graceful=graceful)
            sky.stream_and_get(request_id)
        except Exception as exc:
            success = False
            reason = f"{type(exc).__name__}: {exc}"

        # Best-effort provenance: emit even on failure so an audit can see
        # the attempted teardown. Skipped silently when no session_id is
        # known (orphan cluster, or manifest missing).
        if session_id:
            try:
                log = get_provenance_log(session_id)
                log.emit_compute_cluster_down(
                    cluster_name=cluster_name,
                    graceful=graceful,
                    success=success,
                    reason=reason,
                )
            except Exception:
                pass  # Best-effort.

        if success:
            delete_cluster(cluster_name)
        return success

    def _set_cluster_autostop(
        self,
        cluster_name: str,
        idle_minutes: int,
        wait_for: str = "jobs",
        hook: Optional[str] = None,
    ) -> bool:
        """Apply autostop config (idle threshold, wait_for, hook) to an
        existing cluster. Best-effort.

        Wraps ``sky.autostop()``. Used by ``launch_cluster`` to apply
        the hook (since ``sky.launch`` doesn't take it directly) and
        callable directly via the ``compute_cluster`` tool.
        """
        sky = self._get_sky()
        try:
            from sky.skylet.autostop_lib import AutostopWaitFor
        except Exception:
            AutostopWaitFor = None  # type: ignore

        wait_enum = None
        if AutostopWaitFor is not None:
            try:
                wait_enum = AutostopWaitFor(wait_for)
            except Exception:
                wait_enum = None

        try:
            request_id = sky.autostop(
                cluster_name,
                idle_minutes=idle_minutes,
                wait_for=wait_enum,
                hook=hook,
            )
            sky.stream_and_get(request_id)
            return True
        except Exception:
            return False

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
            # second round-trip. Same shape as M0's failure path. Pass the
            # int we already extracted from the queue record — Sky's name
            # lookup only resolves non-terminal jobs, so a FAILED job needs
            # the int form to retrieve any logs at all.
            error_logs = self.get_logs(
                job_id, tail=200, managed_job_id=managed_job_id
            )
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

    # Sky's dump_managed_job_logs returns this literal string when a name
    # lookup hits no non-terminal job (sky/jobs/utils.py:1587). Terminal jobs
    # are invisible to tail_logs(name=...) — they're only reachable by integer
    # job_id. Treating the sentinel as logs would let _extract_error_line
    # surface "not found" as the user's error preview, which is exactly the
    # bg_wait-lying-about-terminal-jobs bug.
    _SKY_NONTERMINAL_NAME_SENTINEL = "No running managed job found with name"

    def get_logs(
        self,
        job_id: str,
        tail: int = 100,
        managed_job_id: Optional[int] = None,
    ) -> str:
        """Get tail of a managed job's logs.

        Uses ``sky.jobs.tail_logs`` — the managed-jobs equivalent of M0's
        ``sky.tail_logs``. ``follow=False`` is mandatory: a follow=True call
        blocks until the job ends, which would freeze the agent loop on a
        long-running case. ``output_stream=`` captures into an in-memory
        buffer so the function returns once the tail is buffered, not at
        end-of-time.

        Resolution strategy:
          1. If ``managed_job_id`` is provided (or recoverable from the
             managed-job queue), call ``tail_logs(job_id=<int>)``. This is
             the *only* form that works for terminal jobs — Sky's name
             lookup goes through ``get_nonterminal_job_ids_by_name`` and
             returns a "not found" sentinel for any FAILED/COMPLETED job.
          2. Fall back to ``tail_logs(name=<str>)`` only when no int is
             recoverable (orphaned manifest, queue lookup transiently
             failed). Useful for still-running jobs.

        Args:
            job_id: Managed-job name (the value passed to ``sky.jobs.launch(name=...)``).
            tail: Number of trailing log lines to return.
            managed_job_id: Integer Sky-side job id. Pass it through when
                the caller already has it in hand (typically ``get_status``,
                which holds the queue record). Saves a redundant queue_v2
                round-trip and is the only path that works post-terminal.

        Returns:
            Log content as a string; never None — callers (notably
            ``get_status`` on FAILED) write the return value straight to
            the saved log file used by ``bg_status``.
        """
        sky = self._get_sky()
        import io

        if managed_job_id is None:
            managed_job_id = self.get_managed_job_id(job_id)

        buf = io.StringIO()
        try:
            if managed_job_id is not None:
                sky.jobs.tail_logs(
                    name=None,
                    job_id=managed_job_id,
                    follow=False,
                    tail=tail,
                    output_stream=buf,
                )
            else:
                sky.jobs.tail_logs(
                    name=job_id,
                    job_id=None,
                    follow=False,
                    tail=tail,
                    output_stream=buf,
                )
            text = buf.getvalue()
        except Exception as e:
            partial = buf.getvalue()
            err_line = f"[get_logs error: {type(e).__name__}: {e}]"
            return f"{partial}\n{err_line}" if partial else err_line

        # Defensive: if Sky still emitted the non-terminal name sentinel
        # (e.g. a future code path or the int lookup also returned it),
        # treat it as "no logs" rather than feeding it to _extract_error_line.
        if self._SKY_NONTERMINAL_NAME_SENTINEL in text:
            return ""
        return text

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
