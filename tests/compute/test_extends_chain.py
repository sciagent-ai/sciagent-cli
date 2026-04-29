"""Tests for ``_get_service_resources`` extends:-chain merge (M0 follow-up #2).

Surfaced by B8: ``openfoam-swak4foam-2012`` extends ``openfoam-swak4foam``
extends ``openfoam``, but only the root ``openfoam`` declares
``resources: {min_memory_gb: 8, recommended_memory_gb: 32, min_cpus: 4}``.
A bare ``compute_run(service="openfoam-swak4foam-2012", ...)`` was landing
on a c6i.large (2 vCPU / 8 GB) — a node 8 MPI ranks would thrash on.

The fix walks the extends chain so leaves inherit hints from the nearest
ancestor that declares them; explicit leaf hints still win.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from sciagent.tools.atomic import compute as compute_mod
from sciagent.tools.atomic.compute import _get_service_resources


@pytest.fixture(autouse=True)
def _clear_registry_cache():
    """Each test starts with a fresh registry cache so patching takes effect."""
    compute_mod._registry_cache.clear()
    yield
    compute_mod._registry_cache.clear()


def _patched_registry(registry: dict):
    return patch.object(
        compute_mod, "_load_service_registry", return_value=registry
    )


def test_leaf_inherits_root_resources_through_chain():
    """openfoam-swak4foam-2012 → openfoam-swak4foam → openfoam: leaf gets root's hints."""
    registry = {
        "defaults": {"resources": {"min_memory_gb": 4, "min_cpus": 2}},
        "services": {
            "openfoam": {
                "extends": None,
                "resources": {
                    "min_memory_gb": 8,
                    "recommended_memory_gb": 32,
                    "min_cpus": 4,
                },
            },
            "openfoam-swak4foam": {"extends": "openfoam"},
            "openfoam-swak4foam-2012": {"extends": "openfoam-swak4foam"},
        },
    }
    with _patched_registry(registry):
        resources = _get_service_resources("openfoam-swak4foam-2012")
    assert resources["min_memory_gb"] == 8
    assert resources["recommended_memory_gb"] == 32
    assert resources["min_cpus"] == 4


def test_leaf_resources_override_parent():
    """Explicit leaf keys win over inherited ones."""
    registry = {
        "defaults": {"resources": {"min_cpus": 2}},
        "services": {
            "parent": {
                "extends": None,
                "resources": {"min_memory_gb": 8, "min_cpus": 4},
            },
            "leaf": {
                "extends": "parent",
                "resources": {"min_cpus": 16},  # overrides parent's 4
            },
        },
    }
    with _patched_registry(registry):
        resources = _get_service_resources("leaf")
    assert resources["min_cpus"] == 16
    assert resources["min_memory_gb"] == 8  # inherited from parent


def test_no_extends_falls_back_to_defaults():
    """Service with no extends and no resources gets defaults verbatim."""
    registry = {
        "defaults": {
            "resources": {
                "min_memory_gb": 4,
                "recommended_memory_gb": 8,
                "min_cpus": 2,
                "gpu_beneficial": False,
                "gpu_required": False,
            }
        },
        "services": {"plain": {"extends": None}},
    }
    with _patched_registry(registry):
        resources = _get_service_resources("plain")
    assert resources["min_memory_gb"] == 4
    assert resources["recommended_memory_gb"] == 8
    assert resources["min_cpus"] == 2
    assert resources["gpu_beneficial"] is False


def test_unknown_service_returns_defaults():
    """A service not in the registry returns the defaults block unchanged."""
    registry = {
        "defaults": {"resources": {"min_memory_gb": 4, "min_cpus": 2}},
        "services": {"known": {"extends": None}},
    }
    with _patched_registry(registry):
        resources = _get_service_resources("not-a-real-service")
    assert resources == {"min_memory_gb": 4, "min_cpus": 2}


def test_cycle_does_not_loop_forever():
    """Hand-edited registry could introduce a cycle; we must terminate."""
    registry = {
        "defaults": {"resources": {"min_cpus": 2}},
        "services": {
            "a": {"extends": "b", "resources": {"min_memory_gb": 1}},
            "b": {"extends": "a", "resources": {"min_memory_gb": 2}},
        },
    }
    with _patched_registry(registry):
        # Should not hang. Leaf wins, so the result starts at 'a'.
        resources = _get_service_resources("a")
    assert resources["min_memory_gb"] == 1


def test_missing_parent_terminates_chain():
    """If a parent named in extends: is absent, walk stops there cleanly."""
    registry = {
        "defaults": {"resources": {"min_cpus": 2}},
        "services": {
            "child": {
                "extends": "ghost-parent",
                "resources": {"min_memory_gb": 16},
            }
        },
    }
    with _patched_registry(registry):
        resources = _get_service_resources("child")
    assert resources["min_memory_gb"] == 16
    assert resources["min_cpus"] == 2  # default survives


def test_real_registry_openfoam_chain():
    """Integration check against the actual services/registry.yaml.

    Guards against the real B8 regression: a bare
    ``compute_run(service="openfoam-swak4foam-2012")`` landing on a node
    too small to hold its declared MPI rank count.
    """
    # No patch — uses the on-disk registry.
    resources = _get_service_resources("openfoam-swak4foam-2012")
    # Inherited from root ``openfoam`` two hops up.
    assert resources["min_memory_gb"] == 8
    assert resources["recommended_memory_gb"] == 32
    assert resources["min_cpus"] == 4
