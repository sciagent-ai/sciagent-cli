"""
Layered config loader for sciagent.

Resolution order, last writer wins:
  1. dataclass defaults (OrchestratorConfig, AgentConfig)
  2. ~/.sciagent/config.yaml
  3. <project_dir>/.sciagent.yaml (walks up from cwd until found or root)
  4. --config <path>
  5. --set KEY=VAL overrides (dotted paths, YAML-typed values)

Public schema mirrors DESIGN_HARNESS.md §10.2:

    orchestrator:
      enable_data_gate: bool
      enable_exec_gate: bool
      enable_verification: bool
      verification_threshold: float
      verifier_model: str | null
      max_wall_seconds: int | null
      max_cost_usd: float | null
    agent:
      max_iterations: int
      session_soft_budget: int | null
      reasoning_effort: "low" | "medium" | "high"

Bench cells and end users reach every OrchestratorConfig / AgentConfig field via
`--set` (e.g. `--set orchestrator.data_gate_strict=false`); the schema above
lists the keys the harness contract pins, not the only ones accepted.

Named presets (`--profile`) are intentionally not part of H1 — bench owns recipe
naming. If end users later ask for shipped presets, add a thin `--profile NAME`
shim that resolves to a YAML inside the package.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field, fields, is_dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, Optional

import yaml

from .agent import AgentConfig
from .orchestrator import OrchestratorConfig


USER_CONFIG_PATH = Path.home() / ".sciagent" / "config.yaml"
PROJECT_CONFIG_NAME = ".sciagent.yaml"


class ConfigError(ValueError):
    """Raised on malformed config files or unknown keys."""


@dataclass
class SciagentConfig:
    """Effective config produced by ``load_config``."""

    orchestrator: OrchestratorConfig = field(default_factory=OrchestratorConfig)
    agent: AgentConfig = field(default_factory=AgentConfig)
    sources: list = field(default_factory=list)  # paths/labels merged in order

    def to_dict(self) -> Dict[str, Any]:
        return {
            "orchestrator": _dataclass_to_dict(self.orchestrator),
            "agent": _dataclass_to_dict(self.agent),
        }

    def to_yaml(self) -> str:
        return yaml.safe_dump(self.to_dict(), sort_keys=False, default_flow_style=False)


def load_config(
    explicit_path: Optional[Path] = None,
    project_dir: Optional[Path] = None,
    overrides: Optional[Iterable[str]] = None,
) -> SciagentConfig:
    """Build a ``SciagentConfig`` by merging the documented layers.

    ``overrides`` is an iterable of ``KEY=VALUE`` strings from ``--set``; keys
    use dotted paths (``orchestrator.enable_data_gate``) and values parse as
    YAML scalars so ``true``/``false``/``123``/``1.5`` typecheck.
    """
    merged: Dict[str, Any] = {}
    sources: list = ["defaults"]

    # Layer 2: user-level
    if USER_CONFIG_PATH.exists():
        merged = _deep_merge(merged, _read_yaml(USER_CONFIG_PATH))
        sources.append(str(USER_CONFIG_PATH))

    # Layer 3: project-level (walk up from cwd, then project_dir if given)
    project_file = _find_project_config(project_dir)
    if project_file is not None:
        merged = _deep_merge(merged, _read_yaml(project_file))
        sources.append(str(project_file))

    # Layer 4: explicit --config path
    if explicit_path is not None:
        path = Path(explicit_path)
        if not path.exists():
            raise ConfigError(f"--config path does not exist: {path}")
        merged = _deep_merge(merged, _read_yaml(path))
        sources.append(str(path))

    # Layer 5: --set KEY=VAL overrides
    if overrides:
        for raw in overrides:
            _apply_override(merged, raw)
            sources.append(f"--set {raw}")

    return SciagentConfig(
        orchestrator=_build_orchestrator(merged.get("orchestrator", {})),
        agent=_build_agent(merged.get("agent", {})),
        sources=sources,
    )


def list_config_keys() -> Dict[str, list]:
    """Return the keys exposed by each top-level section, with type hints.

    Drives ``sciagent config keys`` so users discover what they can ``--set``
    without reading the source.
    """

    def describe(cls) -> list:
        out = []
        for f in fields(cls):
            out.append({"name": f.name, "type": _type_label(f.type), "default": _default_label(f)})
        return out

    return {
        "orchestrator": describe(OrchestratorConfig),
        "agent": describe(AgentConfig),
    }


# ---------- internals ----------


def _find_project_config(project_dir: Optional[Path]) -> Optional[Path]:
    """Walk up from cwd (and then project_dir if distinct) to find .sciagent.yaml."""
    seen: set = set()
    starts: list = [Path.cwd()]
    if project_dir is not None:
        p = Path(project_dir).resolve()
        if p not in starts:
            starts.append(p)

    for start in starts:
        current = start.resolve()
        while True:
            if current in seen:
                break
            seen.add(current)
            candidate = current / PROJECT_CONFIG_NAME
            if candidate.exists():
                return candidate
            if current.parent == current:
                break
            current = current.parent
    return None


def _read_yaml(path: Path) -> Dict[str, Any]:
    try:
        with open(path) as handle:
            data = yaml.safe_load(handle) or {}
    except yaml.YAMLError as exc:
        raise ConfigError(f"Malformed YAML at {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise ConfigError(f"Top-level YAML at {path} must be a mapping, got {type(data).__name__}")
    return data


def _apply_override(data: Dict[str, Any], raw: str) -> None:
    if "=" not in raw:
        raise ConfigError(f"--set expects KEY=VALUE, got: {raw!r}")
    key, _, value_str = raw.partition("=")
    key = key.strip()
    if not key:
        raise ConfigError(f"--set key is empty in: {raw!r}")
    try:
        # YAML scalar parsing covers bool / int / float / null / string in one shot.
        value = yaml.safe_load(value_str)
    except yaml.YAMLError as exc:
        raise ConfigError(f"--set value is not a valid YAML scalar: {raw!r}: {exc}") from exc

    parts = key.split(".")
    cursor = data
    for part in parts[:-1]:
        nxt = cursor.get(part)
        if not isinstance(nxt, dict):
            nxt = {}
            cursor[part] = nxt
        cursor = nxt
    cursor[parts[-1]] = value


def _deep_merge(base: Dict[str, Any], overlay: Dict[str, Any]) -> Dict[str, Any]:
    out = dict(base)
    for key, value in overlay.items():
        if key in out and isinstance(out[key], dict) and isinstance(value, dict):
            out[key] = _deep_merge(out[key], value)
        else:
            out[key] = value
    return out


def _build_orchestrator(data: Dict[str, Any]) -> OrchestratorConfig:
    return _build_dataclass(OrchestratorConfig, data, label="orchestrator")


def _build_agent(data: Dict[str, Any]) -> AgentConfig:
    return _build_dataclass(AgentConfig, data, label="agent")


def _build_dataclass(cls, data: Dict[str, Any], *, label: str):
    valid = {f.name for f in fields(cls)}
    unknown = [k for k in data if k not in valid]
    if unknown:
        raise ConfigError(
            f"Unknown {label} key(s): {unknown}. "
            f"Run `sciagent config keys` to see the supported set."
        )
    # Filter to fields the dataclass actually carries; leave defaults for the rest.
    kwargs = {k: v for k, v in data.items() if k in valid}
    return cls(**kwargs)


def _dataclass_to_dict(obj: Any) -> Dict[str, Any]:
    if not is_dataclass(obj):
        return obj
    out: Dict[str, Any] = {}
    for f in fields(obj):
        value = getattr(obj, f.name)
        # tuple fields (e.g., data_acquisition_types) serialize as lists for YAML.
        if isinstance(value, tuple):
            value = list(value)
        out[f.name] = value
    return out


def _type_label(t: Any) -> str:
    # Field type annotations come through as strings under `from __future__
    # import annotations`; otherwise they're real type objects. Real typing
    # constructs like Optional[float] need str() rather than __name__ (which
    # would drop the parameter and just say "Optional").
    if isinstance(t, str):
        return t
    name = getattr(t, "__name__", None)
    if name and not hasattr(t, "__args__"):
        return name
    return str(t).replace("typing.", "")


def _default_label(f) -> Any:
    if f.default is not field.__class__:  # has a default
        try:
            return f.default
        except Exception:
            return None
    return None
