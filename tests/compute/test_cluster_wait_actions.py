"""compute_cluster(action='wait_until_up' / 'wait_for_job') — long-poll
inside one LLM turn.

The architectural fix for the trace where the agent burned dozens of
LLM turns polling cluster status. These tests pin:

  - wait_until_up returns ready=True when sky.status reports UP
  - wait_until_up returns ready=False, timed_out=True when budget elapses
  - wait_until_up bails on terminal-bad states (STOPPED/AUTOSTOPPING)
    so the agent doesn't exec on a dying cluster
  - wait_until_up honors the BaseTool interrupt event (Ctrl+C wakes wait)
  - wait_for_job tracks per-cluster int job_ids via sky.queue
  - wait_for_job returns terminal=True on COMPLETED/FAILED/CANCELLED
  - wait_for_job times out cleanly with a "call again with longer
    timeout" hint
"""

from __future__ import annotations

import threading
import time
from unittest.mock import MagicMock

import pytest

from sciagent.compute.backends.skypilot import SkyPilotBackend
from sciagent.tools.registry import BaseTool


@pytest.fixture(autouse=True)
def _clear_event():
    BaseTool._shared_interrupt_event = None
    yield
    BaseTool._shared_interrupt_event = None


def _backend(mock_sky):
    b = SkyPilotBackend()
    b._sky = mock_sky
    return b


def _status_payload(name: str, autostop=None):
    p = MagicMock()
    p.status.name = name
    p.autostop = autostop
    p.to_down = False
    return p


# ---- wait_until_up --------------------------------------------------


def test_wait_until_up_returns_ready_when_cluster_is_up(tmp_path, monkeypatch):
    """The happy path: sky.status reports UP on the first poll. The wait
    must return immediately with ready=True so the caller proceeds."""
    monkeypatch.setenv("HOME", str(tmp_path))
    mock_sky = MagicMock()
    mock_sky.status.return_value = "rid"
    mock_sky.stream_and_get.return_value = [_status_payload("UP")]

    b = _backend(mock_sky)
    info = b.wait_cluster_up("c1", timeout=5.0, poll_interval=0.01)
    assert info["ready"] is True
    assert info["status"] == "UP"
    assert info["timed_out"] is False


def test_wait_until_up_times_out_on_persistent_init(tmp_path, monkeypatch):
    """When sky.status keeps reporting INIT past the timeout, return
    timed_out=True — NOT raise. The agent's job is to decide whether to
    wait again or fall back to status snapshots."""
    monkeypatch.setenv("HOME", str(tmp_path))
    mock_sky = MagicMock()
    mock_sky.status.return_value = "rid"
    mock_sky.stream_and_get.return_value = [_status_payload("INIT")]

    b = _backend(mock_sky)
    info = b.wait_cluster_up("c1", timeout=0.1, poll_interval=0.02)
    assert info["ready"] is False
    assert info["timed_out"] is True
    assert info["status"] == "INIT"
    assert "longer timeout" in info["reason"].lower()


def test_wait_until_up_bails_on_terminal_bad_status(tmp_path, monkeypatch):
    """STOPPED / AUTOSTOPPING are terminal-bad — no point waiting. Return
    ready=False, timed_out=False, with a reason naming the state so the
    agent doesn't try to exec on a dying cluster."""
    monkeypatch.setenv("HOME", str(tmp_path))
    mock_sky = MagicMock()
    mock_sky.status.return_value = "rid"
    mock_sky.stream_and_get.return_value = [_status_payload("STOPPED")]

    b = _backend(mock_sky)
    info = b.wait_cluster_up("c1", timeout=10.0, poll_interval=0.01)
    assert info["ready"] is False
    assert info["timed_out"] is False
    assert info["status"] == "STOPPED"
    assert "STOPPED" in info["reason"]


def test_wait_until_up_honors_interrupt_event(tmp_path, monkeypatch):
    """Ctrl+C during the wait must wake quickly and return interrupted=True.
    Same contract as bg_wait/task_wait."""
    monkeypatch.setenv("HOME", str(tmp_path))
    event = threading.Event()
    BaseTool.set_shared_interrupt_event(event)

    mock_sky = MagicMock()
    mock_sky.status.return_value = "rid"
    # Always INIT; interrupt is the only exit condition.
    mock_sky.stream_and_get.return_value = [_status_payload("INIT")]

    def _trip_after_short_delay():
        time.sleep(0.05)
        event.set()
    threading.Thread(target=_trip_after_short_delay, daemon=True).start()

    b = _backend(mock_sky)
    start = time.monotonic()
    info = b.wait_cluster_up("c1", timeout=10.0, poll_interval=0.05)
    elapsed = time.monotonic() - start

    assert info.get("interrupted") is True
    assert info["ready"] is False
    assert elapsed < 2.0


# ---- wait_for_job ---------------------------------------------------


def _queue_record(job_id: int, status_name: str):
    rec = MagicMock()
    rec.job_id = job_id
    rec.status.name = status_name
    return rec


def test_wait_for_job_returns_terminal_on_succeeded(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    mock_sky = MagicMock()
    mock_sky.queue.return_value = "rid"
    mock_sky.stream_and_get.return_value = [_queue_record(7, "SUCCEEDED")]

    b = _backend(mock_sky)
    info = b.wait_cluster_job("c1", cluster_job_id=7, timeout=5.0, poll_interval=0.01)
    assert info["terminal"] is True
    assert info["status"] == "SUCCEEDED"
    assert info["timed_out"] is False


def test_wait_for_job_returns_terminal_on_failed(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    mock_sky = MagicMock()
    mock_sky.queue.return_value = "rid"
    mock_sky.stream_and_get.return_value = [_queue_record(3, "FAILED")]

    b = _backend(mock_sky)
    info = b.wait_cluster_job("c1", cluster_job_id=3, timeout=5.0, poll_interval=0.01)
    assert info["terminal"] is True
    assert info["status"] == "FAILED"


def test_wait_for_job_times_out_with_running_status(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    mock_sky = MagicMock()
    mock_sky.queue.return_value = "rid"
    mock_sky.stream_and_get.return_value = [_queue_record(1, "RUNNING")]

    b = _backend(mock_sky)
    info = b.wait_cluster_job("c1", cluster_job_id=1, timeout=0.1, poll_interval=0.02)
    assert info["terminal"] is False
    assert info["timed_out"] is True
    assert info["status"] == "RUNNING"
    assert "longer timeout" in info["reason"].lower()


def test_wait_for_job_picks_right_record_among_many(tmp_path, monkeypatch):
    """sky.queue returns ALL jobs on the cluster. wait_for_job must match
    by int job_id, not by position."""
    monkeypatch.setenv("HOME", str(tmp_path))
    mock_sky = MagicMock()
    mock_sky.queue.return_value = "rid"
    mock_sky.stream_and_get.return_value = [
        _queue_record(1, "SUCCEEDED"),
        _queue_record(2, "RUNNING"),
        _queue_record(3, "FAILED"),
    ]
    b = _backend(mock_sky)

    # Asking for job 3 should return its FAILED status, not job 1's
    # SUCCEEDED.
    info = b.wait_cluster_job("c1", cluster_job_id=3, timeout=5.0, poll_interval=0.01)
    assert info["terminal"] is True
    assert info["status"] == "FAILED"


# ---- compute_cluster tool surface -----------------------------------


def test_compute_cluster_wait_until_up_action_dispatches(tmp_path, monkeypatch):
    """End-to-end through the compute_cluster atomic tool — verifies the
    new action plumbs to router.wait_cluster_up."""
    from sciagent.tools.atomic.compute_cluster import ComputeClusterTool

    tool = ComputeClusterTool()
    fake_router = MagicMock()
    fake_router.wait_cluster_up.return_value = {
        "ready": True, "status": "UP", "elapsed_sec": 4.2,
        "timed_out": False, "manifest": None,
    }
    tool._router = fake_router

    out = tool.execute(action="wait_until_up", cluster_name="c1", timeout=10)
    assert out.success is True
    assert out.output["ready"] is True
    assert out.output["status"] == "UP"
    fake_router.wait_cluster_up.assert_called_once()


def test_compute_cluster_wait_for_job_requires_cluster_job_id():
    from sciagent.tools.atomic.compute_cluster import ComputeClusterTool
    tool = ComputeClusterTool()
    tool._router = MagicMock()
    out = tool.execute(action="wait_for_job", cluster_name="c1")
    assert out.success is False
    assert "cluster_job_id" in (out.error or "")


def test_compute_cluster_wait_for_job_action_dispatches():
    from sciagent.tools.atomic.compute_cluster import ComputeClusterTool

    tool = ComputeClusterTool()
    fake_router = MagicMock()
    fake_router.wait_cluster_job.return_value = {
        "terminal": True, "status": "SUCCEEDED", "elapsed_sec": 12.0,
        "timed_out": False, "summary": "ok",
    }
    tool._router = fake_router

    out = tool.execute(
        action="wait_for_job",
        cluster_name="c1",
        cluster_job_id=7,
        timeout=120,
    )
    assert out.success is True
    assert out.output["terminal"] is True
    assert out.output["status"] == "SUCCEEDED"
    fake_router.wait_cluster_job.assert_called_once()
