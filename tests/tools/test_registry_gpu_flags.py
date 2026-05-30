"""Pin tests for registry GPU flags after H5 (`pytorch` service) and the
companion cheap-fix that adds the "GPU acceleration available (CUDA)"
capability line to the five GPU-headline-speedup services.

These tests guard against:
  - silent reversion of `pytorch` resources (it's the only GPU-required
    service in the registry)
  - silent reversion of `meep` back to gpu_beneficial=false
  - silent removal of the CUDA capability line on the five cheap-fix
    services (gromacs, lammps, qiskit, meep, paraview)
  - sweep-too-wide: 17 other services must remain at gpu_beneficial=false
    (false by default OR explicitly)
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml


@pytest.fixture(scope="module")
def registry() -> dict:
    p = Path(__file__).resolve().parents[2] / "src" / "sciagent" / "services" / "registry.yaml"
    with p.open() as f:
        return yaml.safe_load(f)


def _gpu_beneficial(registry: dict, service: str) -> bool:
    """Read effective gpu_beneficial honoring the registry default (false)."""
    entry = registry["services"][service]
    resources = entry.get("resources") or {}
    if "gpu_beneficial" in resources:
        return resources["gpu_beneficial"]
    return registry["defaults"]["resources"]["gpu_beneficial"]


# ---- H5: pytorch service shape ------------------------------------------


def test_pytorch_service_exists(registry: dict) -> None:
    assert "pytorch" in registry["services"]


def test_pytorch_image_and_dockerfile(registry: dict) -> None:
    entry = registry["services"]["pytorch"]
    assert entry["image"] == "ghcr.io/sciagent-ai/pytorch"
    assert entry["dockerfile"] == "services/pytorch/Dockerfile"
    # Dockerfile actually exists on disk
    repo_root = Path(__file__).resolve().parents[2]
    assert (repo_root / "src" / "sciagent" / "services" / "pytorch" / "Dockerfile").exists()


def test_pytorch_resources_gpu_required(registry: dict) -> None:
    res = registry["services"]["pytorch"]["resources"]
    assert res["gpu_beneficial"] is True
    assert res["gpu_required"] is True
    assert res["min_memory_gb"] == 16
    assert res["recommended_memory_gb"] == 24
    assert res["min_cpus"] == 4


def test_pytorch_packages_user_facing_no_internal_base(registry: dict) -> None:
    """Per the registry's user-facing convention, the `packages:` list shows
    recognizable names — `torch`, `transformers`, etc. — not internal sciagent
    base images."""
    pkgs = registry["services"]["pytorch"]["packages"]
    assert "torch" in pkgs
    assert "transformers" in pkgs
    assert "accelerate" in pkgs
    assert "peft" in pkgs
    # Internal bases like `sci-core` / `scipy-base` must not appear.
    assert "sci-core" not in pkgs
    assert "scipy-base" not in pkgs


def test_pytorch_extends_null_so_extension_pattern_is_explicit(registry: dict) -> None:
    """`extends: null` because the build pipeline doesn't honor `extends:` at
    image-build time — pytorch-bio etc. must FROM the base explicitly in
    their own Dockerfile."""
    assert registry["services"]["pytorch"]["extends"] is None


def test_pytorch_amd64_only(registry: dict) -> None:
    """pytorch/pytorch base image is amd64-only."""
    assert registry["services"]["pytorch"]["architectures"] == ["linux/amd64"]


# ---- Cheap fix: gpu_beneficial state on the five headline services -----


GPU_HEADLINE_SERVICES = ["gromacs", "lammps", "qiskit", "meep", "paraview"]


@pytest.mark.parametrize("service", GPU_HEADLINE_SERVICES)
def test_gpu_headline_services_are_gpu_beneficial(registry: dict, service: str) -> None:
    assert _gpu_beneficial(registry, service) is True, (
        f"{service}: GPU is the headline algorithmic speedup; flag must be true"
    )


@pytest.mark.parametrize("service", GPU_HEADLINE_SERVICES)
def test_gpu_headline_services_advertise_cuda_capability(
    registry: dict, service: str
) -> None:
    caps = registry["services"][service]["capabilities"]
    assert "GPU acceleration available (CUDA)" in caps, (
        f"{service}: missing user-facing CUDA capability line"
    )


@pytest.mark.parametrize("service", GPU_HEADLINE_SERVICES)
def test_gpu_headline_services_do_not_require_gpu(
    registry: dict, service: str
) -> None:
    """These algorithms work on CPU, just slower. gpu_required stays false."""
    assert registry["services"][service]["resources"]["gpu_required"] is False


# ---- Regression: don't sweep too wide ----------------------------------


# Every other registered service must stay at gpu_beneficial=false. `pytorch`
# is excluded because it is genuinely GPU-required (asserted above); the five
# headline services are excluded because the cheap fix targets them.
NON_GPU_SERVICES = [
    "scipy-base", "sci-core", "rdkit", "sympy", "cvxpy", "rcwa",
    "openfoam", "openfoam-swak4foam", "openfoam-swak4foam-2012",
    "ngspice", "ase", "gmsh", "elmer", "openroad", "biopython",
    "blast", "pyoptools", "optuna", "dwsim", "iic-osic-tools",
    "sciml-julia",
]


@pytest.mark.parametrize("service", NON_GPU_SERVICES)
def test_non_gpu_services_remain_cpu_only(registry: dict, service: str) -> None:
    assert _gpu_beneficial(registry, service) is False, (
        f"{service}: cheap fix swept too wide — flag should still be false"
    )
