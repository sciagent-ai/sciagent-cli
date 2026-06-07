"""
Output sync helper for SkyPilot jobs.

Why this exists: sciagent's compute_run wrapper uses SkyPilot managed jobs,
which run on worker nodes the user can't ssh/rsync into. Outputs land in a
per-session workspace bucket on whichever cloud the cluster ran on. Without
an automatic fetch, the agent thrashes — observed in real transcripts to
guess non-existent `sky storage download` commands and launch extra cloud
jobs to `cat` files.

This module is NOT a standalone tool. It's a helper called by `bg_wait`
when a cloud job hits COMPLETED, so the agent gets local file paths back
in the same call that observed completion.

Cloud-agnostic dispatch: SkyPilot supports S3/GCS/Azure/R2/OCI uniformly.
The manifest's ``outputs_uri`` field carries the cloud identity from launch
to fetch (scheme = the cloud); the dispatch table below maps each scheme
to its cloud-native CLI. Legacy manifests written before the multi-cloud
upgrade had no ``outputs_uri`` and were S3-only — the legacy reconstruction
falls back to ``s3://sciagent-workspace-<session>/_outputs/<job_id>/`` so
in-flight jobs from older sciagent versions keep working.
"""

from __future__ import annotations

import shlex
import shutil
import subprocess
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple
from urllib.parse import urlparse


def _split_uri(uri: str) -> Tuple[str, str, str]:
    """Return (scheme, bucket, prefix) for a sciagent-internal cloud URI.

    Examples:
        s3://bucket/prefix/      -> ("s3", "bucket", "prefix/")
        gs://bucket/             -> ("gs", "bucket", "")
        az://container/jobs/abc/ -> ("az", "container", "jobs/abc/")
    """
    parsed = urlparse(uri)
    scheme = parsed.scheme
    bucket = parsed.netloc
    prefix = parsed.path.lstrip("/")
    return scheme, bucket, prefix


# --- Cloud-CLI dispatch table ------------------------------------------------
#
# Each entry takes (uri, local_target) and returns an argv list. The cloud
# CLI must be installed locally — SkyPilot already requires it for that
# cloud to be usable, so we don't add new deps. Adding a cloud = adding
# one entry below.


def _cmd_s3(uri: str, local_target: str) -> List[str]:
    return ["aws", "s3", "sync", uri, local_target]


def _cmd_gs(uri: str, local_target: str) -> List[str]:
    # gsutil rsync recursively syncs a prefix to a local dir. Fallback to
    # `gcloud storage rsync` happens at exec time if `gsutil` isn't on PATH.
    return ["gsutil", "-m", "rsync", "-r", uri, local_target]


def _cmd_az(uri: str, local_target: str) -> List[str]:
    # Azure URIs in our internal manifest are az://<container>/<prefix>.
    # The Azure CLI needs --source <container> and --destination <local>;
    # the storage account name is resolved from `az login` / SkyPilot config.
    _, container, prefix = _split_uri(uri)
    cmd = [
        "az", "storage", "blob", "download-batch",
        "--source", container,
        "--destination", local_target,
    ]
    if prefix:
        cmd.extend(["--pattern", f"{prefix.rstrip('/')}/*"])
    return cmd


def _cmd_r2(uri: str, local_target: str) -> List[str]:
    # R2 speaks the S3 API but needs a custom endpoint. We rewrite the URI
    # scheme to s3:// so `aws s3 sync` accepts it; the user's AWS CLI must
    # be configured with the R2 endpoint URL (typically via env or profile).
    _, bucket, prefix = _split_uri(uri)
    s3_uri = f"s3://{bucket}/{prefix}"
    return ["aws", "s3", "sync", s3_uri, local_target]


def _cmd_oci(uri: str, local_target: str) -> List[str]:
    _, bucket, prefix = _split_uri(uri)
    cmd = [
        "oci", "os", "object", "bulk-download",
        "--bucket-name", bucket,
        "--download-dir", local_target,
    ]
    if prefix:
        cmd.extend(["--prefix", prefix])
    return cmd


_FETCH_DISPATCH: Dict[str, Callable[[str, str], List[str]]] = {
    "s3": _cmd_s3,
    "gs": _cmd_gs,
    "az": _cmd_az,
    "r2": _cmd_r2,
    "oci": _cmd_oci,
}

# CLI program each scheme depends on. Used to surface a clear "install
# CLI X" error when the binary isn't on PATH.
_REQUIRED_CLI: Dict[str, str] = {
    "s3": "aws",
    "gs": "gsutil",
    "az": "az",
    "r2": "aws",
    "oci": "oci",
}


def _supported_schemes_message() -> str:
    return (
        "supported schemes: " + ", ".join(sorted(_FETCH_DISPATCH.keys()))
    )


def fetch_workspace_outputs(
    job_id: str,
    working_dir: str = ".",
    dest: Optional[str] = None,
    prefix: Optional[str] = None,
    timeout: int = 300,
) -> Dict[str, Any]:
    """Sync a SkyPilot job's outputs from the cloud to a local directory.

    Resolution order (cloud-agnostic):
      1. ``manifest["outputs_uri"]`` — full URI with scheme. Carries the
         cloud identity through from launch.
      2. Legacy fallback: reconstruct ``s3://sciagent-workspace-<session>/<prefix>/``
         from session_id + outputs_prefix (or the legacy ``_outputs/<job_id>/``
         default). Pre-multi-cloud manifests are S3-only; that's the only
         cloud the legacy code wrote to.

    ``prefix`` (optional caller override) replaces the bucket-side prefix
    while keeping the bucket and scheme — useful for cross-tool sharing
    (Job 2 reads Job 1's prefix explicitly).

    Returns a dict describing what was fetched (or why it couldn't be).
    Always returns a dict — never raises — so callers (bg_wait) can fold
    the result into their own ToolResult without try/except plumbing.
    """
    from sciagent.compute.task_index import read_task

    manifest = read_task(job_id)
    if manifest is None:
        return {"ok": False, "reason": f"no manifest for job_id={job_id!r}"}

    # Resolve URI: prefer manifest["outputs_uri"]; else legacy reconstruction.
    uri = manifest.get("outputs_uri")
    if uri is None:
        session_id = manifest.get("session_id")
        if not session_id:
            return {
                "ok": False,
                "reason": (
                    "job has no session_id and no outputs_uri — likely "
                    "launched without a workspace mount (workspace=False "
                    "or non-skypilot backend)"
                ),
            }
        legacy_prefix = (
            manifest.get("outputs_prefix") or f"_outputs/{job_id}/"
        )
        bucket = f"sciagent-workspace-{session_id}"
        uri = f"s3://{bucket}/{legacy_prefix}"

    if prefix is not None:
        # Caller override: replace the bucket-side prefix while keeping the
        # scheme + bucket. Strip any existing path off the URI.
        scheme, bucket, _old_prefix = _split_uri(uri)
        uri = f"{scheme}://{bucket}/{prefix.lstrip('/')}"

    scheme, bucket, resolved_prefix = _split_uri(uri)

    if scheme not in _FETCH_DISPATCH:
        return {
            "ok": False,
            "bucket": bucket,
            "reason": (
                f"unsupported cloud scheme {scheme!r} in outputs_uri={uri!r}; "
                + _supported_schemes_message()
            ),
        }

    required_cli = _REQUIRED_CLI[scheme]
    if shutil.which(required_cli) is None:
        return {
            "ok": False,
            "bucket": bucket,
            "reason": (
                f"{required_cli} CLI not found on PATH — install it or pull "
                f"manually: {required_cli} <download> {uri}"
            ),
        }

    # Local destination layout: <dest_root>/_outputs/<bucket-prefix>/.
    # The _outputs/ parent keeps generated files out of the project root
    # (matters for cases like OpenFOAM that produce many subdirs). The
    # bucket-side prefix stays clean (no _outputs/ segment) — this is a
    # purely local organizational convention.
    #
    # Legacy manifests have bucket prefix "_outputs/<job_id>/" baked in
    # (the pre-migration layout). For those, we DON'T add a second
    # _outputs/ parent — that would produce _outputs/_outputs/<job_id>/.
    dest_root = Path(dest) if dest else Path(working_dir)
    if not dest_root.is_absolute():
        dest_root = Path(working_dir) / dest_root

    prefix_clean = resolved_prefix.strip("/")
    if prefix_clean.startswith("_outputs/") or prefix_clean == "_outputs":
        local_target = dest_root / prefix_clean
    elif prefix_clean:
        local_target = dest_root / "_outputs" / prefix_clean
    else:
        local_target = dest_root / "_outputs"
    local_target.mkdir(parents=True, exist_ok=True)

    cmd_builder = _FETCH_DISPATCH[scheme]
    cmd = cmd_builder(uri, str(local_target))

    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return {
            "ok": False,
            "bucket": bucket,
            "reason": f"{required_cli} sync timed out after {timeout}s",
        }
    except OSError as e:
        return {
            "ok": False,
            "bucket": bucket,
            "reason": f"failed to invoke {required_cli} CLI: {e}",
        }

    if result.returncode != 0:
        stderr = (result.stderr or "").strip()
        # Cloud-specific "bucket missing" hints — most users see these as
        # opaque CLI failures otherwise.
        bucket_missing_markers = (
            "NoSuchBucket",          # S3
            "BucketNotFoundException",  # GCS
            "ContainerNotFound",     # Azure (loose match)
            "BucketNotFound",        # OCI
        )
        if any(m in stderr for m in bucket_missing_markers):
            return {
                "ok": False,
                "bucket": bucket,
                "reason": (
                    "bucket does not exist — job likely didn't mount a "
                    "workspace, or finished before the bucket was created"
                ),
            }
        return {
            "ok": False,
            "bucket": bucket,
            "reason": (
                f"{required_cli} sync exit {result.returncode}: "
                f"{stderr[:300]}"
            ),
        }

    files = []
    bytes_total = 0
    if local_target.exists():
        for p in sorted(local_target.rglob("*")):
            if p.is_file():
                size = p.stat().st_size
                files.append({"path": str(p.relative_to(dest_root)), "bytes": size})
                bytes_total += size

    return {
        "ok": True,
        "bucket": bucket,
        "prefix": resolved_prefix,
        "uri": uri,
        "scheme": scheme,
        "dest": str(dest_root),
        "files": files,
        "file_count": len(files),
        "bytes_total": bytes_total,
    }


def fetch_session_workspace(
    session_id: str,
    working_dir: str = ".",
    timeout: int = 300,
) -> Dict[str, Any]:
    """Sync the durable session workspace bucket to ``<working_dir>/_outputs/workspace/``.

    Sibling of :func:`fetch_workspace_outputs`: the latter pulls a single
    job's ``/outputs/<job_id>/`` slice; this one pulls the cross-step
    ``/workspace/`` mount that survives job boundaries. Both target the
    same local layout the agent (and ``materialize_workspace``) already
    expect, so a sciagent run that mixes auto-fetch with explicit
    materialize calls sees one consistent local mirror.

    Cloud-agnostic via the same dispatch table. Returns a dict in the same
    shape as :func:`fetch_workspace_outputs` so ``bg_wait`` can fold both
    fetches through identical output formatting.

    Best-effort: a missing bucket (session never ran a workspace-mounted
    job) returns ``ok=False`` with a reason; the caller treats that as a
    no-op, not a failure.
    """
    if not session_id:
        return {"ok": False, "reason": "session_id is required"}

    try:
        from sciagent.compute.backends.skypilot import (
            SkyPilotBackend,
            _build_workspace_uri,
        )
        store = SkyPilotBackend().resolve_workspace_store()
        uri = _build_workspace_uri(store, session_id)
    except Exception as exc:
        return {
            "ok": False,
            "reason": f"could not resolve workspace URI for session {session_id!r}: {exc}",
        }

    scheme, bucket, resolved_prefix = _split_uri(uri)

    if scheme not in _FETCH_DISPATCH:
        return {
            "ok": False,
            "bucket": bucket,
            "reason": (
                f"unsupported cloud scheme {scheme!r} for workspace fetch; "
                + _supported_schemes_message()
            ),
        }

    required_cli = _REQUIRED_CLI[scheme]
    if shutil.which(required_cli) is None:
        return {
            "ok": False,
            "bucket": bucket,
            "reason": (
                f"{required_cli} CLI not found on PATH — install it or pull "
                f"the workspace manually with materialize_workspace()."
            ),
        }

    # Mirror materialize_workspace's default layout so explicit and
    # auto-fetched workspaces land at the same local path.
    dest_root = Path(working_dir)
    if not dest_root.is_absolute():
        dest_root = Path(working_dir)
    local_target = dest_root / "_outputs" / "workspace"
    local_target.mkdir(parents=True, exist_ok=True)

    cmd_builder = _FETCH_DISPATCH[scheme]
    cmd = cmd_builder(uri, str(local_target))

    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return {
            "ok": False,
            "bucket": bucket,
            "reason": f"{required_cli} sync timed out after {timeout}s",
        }
    except OSError as e:
        return {
            "ok": False,
            "bucket": bucket,
            "reason": f"failed to invoke {required_cli} CLI: {e}",
        }

    if result.returncode != 0:
        stderr = (result.stderr or "").strip()
        bucket_missing_markers = (
            "NoSuchBucket",
            "BucketNotFoundException",
            "ContainerNotFound",
            "BucketNotFound",
        )
        if any(m in stderr for m in bucket_missing_markers):
            return {
                "ok": False,
                "bucket": bucket,
                "reason": (
                    "workspace bucket does not exist — session has no "
                    "workspace-mounted jobs yet"
                ),
            }
        return {
            "ok": False,
            "bucket": bucket,
            "reason": (
                f"{required_cli} workspace sync exit {result.returncode}: "
                f"{stderr[:300]}"
            ),
        }

    files = []
    bytes_total = 0
    if local_target.exists():
        for p in sorted(local_target.rglob("*")):
            if p.is_file():
                size = p.stat().st_size
                files.append(
                    {"path": str(p.relative_to(dest_root)), "bytes": size}
                )
                bytes_total += size

    return {
        "ok": True,
        "bucket": bucket,
        "prefix": resolved_prefix,
        "uri": uri,
        "scheme": scheme,
        "dest": str(dest_root),
        "files": files,
        "file_count": len(files),
        "bytes_total": bytes_total,
    }
