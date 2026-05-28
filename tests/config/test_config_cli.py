"""End-to-end tests for `sciagent config show` and `sciagent config keys`.

These drive the CLI via subprocess so they exercise argparse, the dispatcher,
and the loader together (the integration check from DESIGN_HARNESS.md §3.6).
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
import yaml


REPO_ROOT = Path(__file__).resolve().parents[2]
SRC_ROOT = REPO_ROOT / "src"


def _run_cli(*args, env_overrides=None, cwd=None) -> subprocess.CompletedProcess:
    env = os.environ.copy()
    # Make sure the test sees the in-tree package, not anything pip-installed.
    env["PYTHONPATH"] = str(SRC_ROOT) + os.pathsep + env.get("PYTHONPATH", "")
    if env_overrides:
        env.update(env_overrides)
    return subprocess.run(
        [sys.executable, "-m", "sciagent.cli", *args],
        capture_output=True,
        text=True,
        env=env,
        cwd=cwd,
    )


def test_config_keys_text_lists_h1_fields(tmp_path):
    result = _run_cli("config", "keys", cwd=tmp_path)
    assert result.returncode == 0, result.stderr
    out = result.stdout
    assert "[orchestrator]" in out
    assert "verifier_model" in out
    assert "max_wall_seconds" in out
    assert "max_cost_usd" in out
    assert "[agent]" in out
    assert "session_soft_budget" in out


def test_config_keys_json(tmp_path):
    result = _run_cli("config", "keys", "--format", "json", cwd=tmp_path)
    assert result.returncode == 0, result.stderr
    data = json.loads(result.stdout)
    orch_names = {entry["name"] for entry in data["orchestrator"]}
    assert {"verifier_model", "max_wall_seconds", "max_cost_usd"}.issubset(orch_names)


def test_config_show_round_trip_through_explicit_file(tmp_path):
    """Real round-trip: write a YAML, point --config at it, read back via show."""
    explicit = tmp_path / "h1.yaml"
    explicit.write_text(yaml.safe_dump({
        "orchestrator": {
            "verifier_model": "anthropic/claude-haiku-4-5-20251001",
            "max_cost_usd": 0.5,
            "enable_data_gate": False,
        },
        "agent": {"reasoning_effort": "high"},
    }))

    result = _run_cli("config", "show", "--config", str(explicit), cwd=tmp_path)
    assert result.returncode == 0, result.stderr

    parsed = yaml.safe_load(result.stdout)
    assert parsed["orchestrator"]["verifier_model"] == "anthropic/claude-haiku-4-5-20251001"
    assert parsed["orchestrator"]["max_cost_usd"] == 0.5
    assert parsed["orchestrator"]["enable_data_gate"] is False
    # Untouched fields keep defaults
    assert parsed["orchestrator"]["enable_exec_gate"] is True
    assert parsed["agent"]["reasoning_effort"] == "high"


def test_config_show_user_home_round_trip(tmp_path):
    """Per §3.6 integration test: a ~/.sciagent/config.yaml is picked up."""
    fake_home = tmp_path / "home"
    fake_sciagent = fake_home / ".sciagent"
    fake_sciagent.mkdir(parents=True)
    (fake_sciagent / "config.yaml").write_text(yaml.safe_dump({
        "orchestrator": {"verifier_model": "anthropic/claude-haiku-4-5-20251001"}
    }))

    project = tmp_path / "proj"
    project.mkdir()

    result = _run_cli(
        "config", "show",
        env_overrides={"HOME": str(fake_home)},
        cwd=project,
    )
    assert result.returncode == 0, result.stderr
    parsed = yaml.safe_load(result.stdout)
    assert parsed["orchestrator"]["verifier_model"] == "anthropic/claude-haiku-4-5-20251001"


def test_config_show_set_overrides_explicit(tmp_path):
    explicit = tmp_path / "h1.yaml"
    explicit.write_text(yaml.safe_dump({
        "orchestrator": {"verifier_model": "anthropic/claude-opus-4-7"}
    }))

    result = _run_cli(
        "config", "show",
        "--config", str(explicit),
        "--set", "orchestrator.verifier_model=openai/gpt-5-mini",
        cwd=tmp_path,
    )
    assert result.returncode == 0, result.stderr
    parsed = yaml.safe_load(result.stdout)
    assert parsed["orchestrator"]["verifier_model"] == "openai/gpt-5-mini"


def test_config_show_unknown_key_exits_nonzero(tmp_path):
    bad = tmp_path / "bad.yaml"
    bad.write_text(yaml.safe_dump({"orchestrator": {"does_not_exist": 1}}))

    result = _run_cli("config", "show", "--config", str(bad), cwd=tmp_path)
    assert result.returncode != 0
    assert "Unknown orchestrator key" in result.stderr


def test_config_show_json_format(tmp_path):
    result = _run_cli("config", "show", "--format", "json", cwd=tmp_path)
    assert result.returncode == 0, result.stderr
    data = json.loads(result.stdout)
    assert "orchestrator" in data and "agent" in data
