"""Unit tests for sciagent.config.load_config — resolution order, --set, errors."""

from __future__ import annotations

from pathlib import Path
from unittest import mock

import pytest
import yaml

from sciagent.config import ConfigError, list_config_keys, load_config


@pytest.fixture
def isolated_home(tmp_path, monkeypatch):
    """Point ~/.sciagent at a temp dir so layer-2 lookups don't read the real one."""
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: fake_home))
    # config.USER_CONFIG_PATH was bound at import time against the real Home;
    # rebind it for the test so the loader sees the temp file.
    import sciagent.config as cfg_mod
    monkeypatch.setattr(cfg_mod, "USER_CONFIG_PATH", fake_home / ".sciagent" / "config.yaml")
    return fake_home


def test_defaults_only(isolated_home, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    cfg = load_config()
    assert cfg.orchestrator.enable_data_gate is True
    assert cfg.orchestrator.verifier_model is None
    assert cfg.orchestrator.max_wall_seconds is None
    # L5: session_soft_budget on AgentConfig now defaults to None — the
    # AgentLoop resolves the concrete value from the per-provider overlay
    # in llm_profiles._OVERLAY at construction time (4M for Anthropic,
    # 1.5M for OpenAI, 2M for Gemini/xAI). Explicit values still win.
    assert cfg.agent.session_soft_budget is None
    assert cfg.sources == ["defaults"]


def test_user_config_merges(isolated_home, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    user_cfg = isolated_home / ".sciagent" / "config.yaml"
    user_cfg.parent.mkdir(parents=True)
    user_cfg.write_text(yaml.safe_dump({
        "orchestrator": {
            "verifier_model": "anthropic/claude-haiku-4-5-20251001",
            "verification_threshold": 0.6,
        }
    }))

    cfg = load_config()
    assert cfg.orchestrator.verifier_model == "anthropic/claude-haiku-4-5-20251001"
    assert cfg.orchestrator.verification_threshold == 0.6
    # Fields not mentioned keep defaults
    assert cfg.orchestrator.enable_data_gate is True
    assert str(user_cfg) in cfg.sources


def test_project_config_overrides_user(isolated_home, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    user_cfg = isolated_home / ".sciagent" / "config.yaml"
    user_cfg.parent.mkdir(parents=True)
    user_cfg.write_text(yaml.safe_dump({
        "orchestrator": {"verification_threshold": 0.6}
    }))

    project_cfg = tmp_path / ".sciagent.yaml"
    project_cfg.write_text(yaml.safe_dump({
        "orchestrator": {"verification_threshold": 0.9}
    }))

    cfg = load_config(project_dir=tmp_path)
    assert cfg.orchestrator.verification_threshold == 0.9


def test_explicit_config_overrides_project(isolated_home, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    project_cfg = tmp_path / ".sciagent.yaml"
    project_cfg.write_text(yaml.safe_dump({
        "orchestrator": {"verification_threshold": 0.5}
    }))
    explicit = tmp_path / "explicit.yaml"
    explicit.write_text(yaml.safe_dump({
        "orchestrator": {"verification_threshold": 0.85}
    }))

    cfg = load_config(explicit_path=explicit, project_dir=tmp_path)
    assert cfg.orchestrator.verification_threshold == 0.85


def test_set_overrides_lands_on_dataclass(isolated_home, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    cfg = load_config(overrides=[
        "orchestrator.enable_data_gate=false",
        "orchestrator.max_cost_usd=1.5",
        "orchestrator.max_wall_seconds=300",
        "orchestrator.verifier_model=openai/gpt-5-mini",
        "agent.reasoning_effort=high",
    ])
    assert cfg.orchestrator.enable_data_gate is False
    assert cfg.orchestrator.max_cost_usd == 1.5
    assert cfg.orchestrator.max_wall_seconds == 300
    assert cfg.orchestrator.verifier_model == "openai/gpt-5-mini"
    assert cfg.agent.reasoning_effort == "high"


def test_set_beats_explicit_config(isolated_home, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    explicit = tmp_path / "explicit.yaml"
    explicit.write_text(yaml.safe_dump({
        "orchestrator": {"verifier_model": "anthropic/claude-opus-4-7"}
    }))

    cfg = load_config(
        explicit_path=explicit,
        overrides=["orchestrator.verifier_model=openai/gpt-5-mini"],
    )
    assert cfg.orchestrator.verifier_model == "openai/gpt-5-mini"


def test_unknown_key_in_yaml_raises(isolated_home, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    bad = tmp_path / "bad.yaml"
    bad.write_text(yaml.safe_dump({"orchestrator": {"does_not_exist": 1}}))

    with pytest.raises(ConfigError, match="Unknown orchestrator key"):
        load_config(explicit_path=bad)


def test_missing_config_path_raises(isolated_home, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    with pytest.raises(ConfigError, match="does not exist"):
        load_config(explicit_path=tmp_path / "missing.yaml")


def test_malformed_yaml_raises(isolated_home, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    bad = tmp_path / "bad.yaml"
    bad.write_text("orchestrator: [this is not a mapping")

    with pytest.raises(ConfigError, match="Malformed YAML"):
        load_config(explicit_path=bad)


def test_top_level_yaml_must_be_mapping(isolated_home, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    bad = tmp_path / "bad.yaml"
    bad.write_text("- just\n- a\n- list\n")

    with pytest.raises(ConfigError, match="must be a mapping"):
        load_config(explicit_path=bad)


def test_set_bad_format_raises(isolated_home, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    with pytest.raises(ConfigError, match="--set expects KEY=VALUE"):
        load_config(overrides=["bareword"])


def test_project_walk_up_from_cwd(isolated_home, tmp_path, monkeypatch):
    """If .sciagent.yaml lives in a parent dir, cwd-walk-up finds it."""
    sub = tmp_path / "a" / "b" / "c"
    sub.mkdir(parents=True)
    (tmp_path / ".sciagent.yaml").write_text(yaml.safe_dump({
        "orchestrator": {"verification_threshold": 0.42}
    }))
    monkeypatch.chdir(sub)

    cfg = load_config()
    assert cfg.orchestrator.verification_threshold == 0.42


def test_to_yaml_round_trips(isolated_home, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    cfg = load_config(overrides=["orchestrator.verifier_model=openai/gpt-5-mini"])
    rendered = cfg.to_yaml()
    data = yaml.safe_load(rendered)
    assert data["orchestrator"]["verifier_model"] == "openai/gpt-5-mini"
    assert "agent" in data and "orchestrator" in data


def test_list_config_keys_covers_h1_additions():
    catalog = list_config_keys()
    orch_names = {entry["name"] for entry in catalog["orchestrator"]}
    assert {"verifier_model", "max_wall_seconds", "max_cost_usd"}.issubset(orch_names)
    agent_names = {entry["name"] for entry in catalog["agent"]}
    assert "session_soft_budget" in agent_names
    assert "reasoning_effort" in agent_names
