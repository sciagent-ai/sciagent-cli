"""Tests for ``_map_status(ManagedJobStatus) -> JobStatus`` (M1A, v4.1 §1).

Two contracts:

  1. Every ``sky.jobs.ManagedJobStatus`` member must map to exactly one
     ``JobStatus``. A future Sky release that adds a new state must fail
     loudly here, not silently default to RUNNING somewhere downstream.
  2. The mapping is keyed on Sky's wire *value* (e.g. ``"SUBMITTED"``), not
     the Python member name (``DEPRECATED_SUBMITTED`` as of sky 0.12.x), so
     it survives Sky renaming members while keeping the values stable.

Together these guard the agent's reaction-to-status logic: the agent treats
RECOVERING differently from RUNNING (output paused, don't conclude the job
hung), and CANCELLED differently from FAILED (terminal, but not retryable
as a failure).
"""

from __future__ import annotations

import pytest

from sciagent.compute.backends.skypilot import (
    _SKY_STATUS_TO_JOB_STATUS,
    _map_status,
)
from sciagent.compute.job import JobStatus


def _all_sky_statuses():
    """Yield every ManagedJobStatus member from the installed Sky."""
    try:
        from sky.jobs import ManagedJobStatus
    except ImportError:
        pytest.skip("skypilot not installed")
    return list(ManagedJobStatus)


def test_every_sky_status_has_a_mapping():
    """Future Sky additions must be addressed deliberately, not collapsed silently."""
    for sky_status in _all_sky_statuses():
        assert sky_status.value in _SKY_STATUS_TO_JOB_STATUS, (
            f"sky.jobs.ManagedJobStatus.{sky_status.name} (value={sky_status.value!r}) "
            "has no mapping in _SKY_STATUS_TO_JOB_STATUS — add it explicitly."
        )


def test_no_sky_status_maps_to_arbitrary_default():
    """Every entry in the map must use a real JobStatus member."""
    for sky_value, mapped in _SKY_STATUS_TO_JOB_STATUS.items():
        assert isinstance(mapped, JobStatus), (
            f"_SKY_STATUS_TO_JOB_STATUS[{sky_value!r}] is not a JobStatus"
        )


# Ground truth from v4.1 §1 — values, not member names, so these are
# rename-resilient. A change to any cell is a deliberate semantic change
# the reviewer must approve.
@pytest.mark.parametrize(
    "sky_value,expected",
    [
        ("PENDING",            JobStatus.PENDING),
        ("SUBMITTED",          JobStatus.PENDING),
        ("STARTING",           JobStatus.PENDING),
        ("RUNNING",            JobStatus.RUNNING),
        ("RECOVERING",         JobStatus.RECOVERING),
        ("CANCELLING",         JobStatus.RUNNING),
        ("SUCCEEDED",          JobStatus.COMPLETED),
        ("CANCELLED",          JobStatus.CANCELLED),
        ("FAILED",             JobStatus.FAILED),
        ("FAILED_SETUP",       JobStatus.FAILED),
        ("FAILED_PRECHECKS",   JobStatus.FAILED),
        ("FAILED_NO_RESOURCE", JobStatus.FAILED),
        ("FAILED_CONTROLLER",  JobStatus.FAILED),
    ],
)
def test_each_sky_value_maps_to_expected_job_status(sky_value, expected):
    """Hard-coded table from v4.1 §1, asserted independently of the live enum.

    Catches drift even if Sky retires a value: the test is the source of
    truth for what sciagent expects; the live enum probe (above) is the
    cross-check.
    """
    assert _SKY_STATUS_TO_JOB_STATUS[sky_value] == expected


def test_map_status_accepts_enum_member():
    """The helper accepts a real ManagedJobStatus member (the runtime path)."""
    try:
        from sky.jobs import ManagedJobStatus
    except ImportError:
        pytest.skip("skypilot not installed")
    assert _map_status(ManagedJobStatus.RUNNING) == JobStatus.RUNNING
    assert _map_status(ManagedJobStatus.SUCCEEDED) == JobStatus.COMPLETED
    assert _map_status(ManagedJobStatus.FAILED_NO_RESOURCE) == JobStatus.FAILED


def test_map_status_accepts_string_value():
    """Debug paths or JSON-deserialized status strings still map correctly."""
    assert _map_status("RUNNING") == JobStatus.RUNNING
    assert _map_status("CANCELLED") == JobStatus.CANCELLED
    assert _map_status("FAILED_SETUP") == JobStatus.FAILED


def test_map_status_unknown_value_returns_failed_loudly():
    """Unknown state defaults to FAILED — never silently to RUNNING."""
    assert _map_status("WHO_KNOWS") == JobStatus.FAILED
    assert _map_status(None) == JobStatus.FAILED


def test_recovering_and_cancelled_are_distinct_terminals():
    """The two new sciagent states must round-trip — agent reacts on them."""
    assert JobStatus.RECOVERING != JobStatus.RUNNING
    assert JobStatus.RECOVERING != JobStatus.FAILED
    assert JobStatus.CANCELLED != JobStatus.FAILED
    assert JobStatus.CANCELLED != JobStatus.COMPLETED
    # Wire values stable for manifest/JSON consumers.
    assert JobStatus.RECOVERING.value == "recovering"
    assert JobStatus.CANCELLED.value == "cancelled"
