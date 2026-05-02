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
                    raise LaunchError(msg, cluster_name=cluster_name)

                if status_name == "SUCCEEDED":
                    return True

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
