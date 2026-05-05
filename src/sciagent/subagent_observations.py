"""Lite-tier subagent Observation schema + parser.

Sub-agents emit candidate lessons (image quirks, backend quirks, workflow
patterns, service idioms) in their terminal reply between
``<observations>...</observations>`` tags as a JSON list. The orchestrator
parses the block off the reply, attaches the structured Observations to
``SubAgentResult.observations``, and bubbles them to the parent's tool
result under an "Observations" header. Observations are never auto-applied
— the user (or, in Full, a router) decides where they get codified.

Source design: ``designdocs_memory/subagent_self_learning.md``.
Plan: ``designdocs_nextsteps/PLAN_OBSERVATIONS_LITE.md``.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple


# Allowed enums. Kept loose-ish — the Lite tier is the platform shake-out
# that validates whether the schema choices hold against real LLM output;
# tightening to strict Literal-checked validation is a Full concern.
KINDS = ("image_quirk", "backend_quirk", "workflow_pattern", "service_idiom")
CONFIDENCES = ("high", "medium", "low")
DESTINATIONS = (
    "dockerfile_env", "dockerfile_run",
    "registry_quirks", "registry_parallel",
    "prompt_compute", "prompt_analyse",
    "smoke_test", "none",
)


@dataclass
class Observation:
    """A candidate lesson surfaced by a sub-agent.

    Field meanings:
      - ``kind``: which lesson family (see ``KINDS``).
      - ``scope``: tags like ``service:openfoam`` / ``backend:skypilot`` /
        ``workflow:parallel-solver``. Without scope, lessons leak to wrong
        contexts — but Lite leaves enforcement to the user reviewing the
        observation.
      - ``trigger`` / ``symptom``: the command/situation and what went
        wrong (or what was non-obvious).
      - ``fix_shape``: ``{"destination": <one of DESTINATIONS>, "patch": <str>}``.
      - ``confidence``: ``high`` (saw symptom AND saw fix work), ``medium``
        (strongly implied, didn't verify), ``low`` (guessing).
      - ``session_id``: the producing subagent's session — for cross-session
        aggregation in Full.
    """

    kind: str
    scope: List[str]
    trigger: str
    symptom: str
    fix_shape: Dict[str, Any]
    confidence: str
    session_id: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "kind": self.kind,
            "scope": list(self.scope),
            "trigger": self.trigger,
            "symptom": self.symptom,
            "fix_shape": dict(self.fix_shape) if self.fix_shape else {},
            "confidence": self.confidence,
            "session_id": self.session_id,
        }

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "Observation":
        return cls(
            kind=str(d.get("kind") or ""),
            scope=[str(s) for s in (d.get("scope") or [])],
            trigger=str(d.get("trigger") or ""),
            symptom=str(d.get("symptom") or ""),
            fix_shape=dict(d.get("fix_shape") or {}),
            confidence=str(d.get("confidence") or ""),
            session_id=d.get("session_id"),
        )


# Match `<observations>...</observations>` (case-insensitive, dot-matches-newline).
# Greedy on inner content, tolerant of leading whitespace inside the tag.
_OBS_BLOCK_RE = re.compile(
    r"<observations>\s*(?P<body>.*?)\s*</observations>",
    re.IGNORECASE | re.DOTALL,
)


def parse_observations_block(
    output: str,
    *,
    session_id: Optional[str] = None,
) -> Tuple[List[Observation], str]:
    """Pull an ``<observations>...</observations>`` block off ``output``.

    Returns ``(observations, stripped_output)`` — the block is removed from
    the returned text so it doesn't double-print under the Observations
    header. Best-effort: malformed JSON, missing tag, or non-list payload
    yields ``([], output)`` unchanged.

    The block body must be a JSON list of observation dicts. A scalar /
    object payload is rejected so a stray ``<observations>note</observations>``
    in narrative text doesn't accidentally parse.
    """
    if not output or "<observations" not in output.lower():
        return [], output

    m = _OBS_BLOCK_RE.search(output)
    if not m:
        return [], output

    body = m.group("body").strip()
    if not body:
        # Empty block is a valid "I considered this and had nothing" signal —
        # strip it from the output but return no observations.
        stripped = (output[: m.start()] + output[m.end():]).rstrip()
        return [], stripped

    # Tolerate fenced JSON (```json ... ```) inside the block.
    body = _strip_code_fence(body)

    try:
        payload = json.loads(body)
    except json.JSONDecodeError:
        return [], output

    if not isinstance(payload, list):
        return [], output

    obs: List[Observation] = []
    for entry in payload:
        if not isinstance(entry, dict):
            continue
        candidate = Observation.from_dict(entry)
        if session_id and not candidate.session_id:
            candidate.session_id = session_id
        # Drop wholly empty entries — kind+symptom is the minimum signal.
        if not candidate.kind and not candidate.symptom:
            continue
        obs.append(candidate)

    stripped = (output[: m.start()] + output[m.end():]).rstrip()
    return obs, stripped


def _strip_code_fence(body: str) -> str:
    """Remove a leading/trailing ```...``` fence if present."""
    s = body.strip()
    if s.startswith("```"):
        first_nl = s.find("\n")
        if first_nl != -1:
            s = s[first_nl + 1:]
        if s.endswith("```"):
            s = s[:-3]
    return s.strip()


def format_observations_for_parent(observations: List[Observation]) -> str:
    """Render observations under an "Observations" header for TaskTool's
    return string.

    Empty list renders nothing (caller should not append the header at all).
    Output is plain markdown — the parent LLM sees this as part of the
    tool result and can mention it in its end-of-task summary to the user.
    """
    if not observations:
        return ""
    lines = ["## Observations", ""]
    for i, obs in enumerate(observations, 1):
        scope = ", ".join(obs.scope) if obs.scope else "(unscoped)"
        dest = (obs.fix_shape or {}).get("destination") or "(no destination)"
        patch = (obs.fix_shape or {}).get("patch") or ""
        lines.append(
            f"{i}. [{obs.kind} · {obs.confidence} · {scope}] {obs.symptom}"
        )
        if obs.trigger:
            lines.append(f"   trigger: {obs.trigger}")
        lines.append(f"   fix → {dest}: {patch}" if patch else f"   fix → {dest}")
    lines.append("")
    lines.append(
        "Review and codify into Dockerfile / registry / prompt if recurring. "
        "Observations are candidate findings — never auto-applied."
    )
    return "\n".join(lines)


# Prompt block injected into compute / analyze / verifier system prompts.
# Idiom-led, leans passive on emission ("empty list is fine"). The schema
# itself lives in this module — keeping the prompt and the parser one
# `from .subagent_observations import OBSERVATION_PROMPT_BLOCK` away from
# drifting apart.
OBSERVATION_PROMPT_BLOCK = """\
## Lessons learned (observations)

When you finish, scan your trajectory for a moment where you discovered
something non-obvious about the image, backend, or workflow shape — a
quirk a future session would otherwise re-discover (image runs as root
and OpenMPI refuses; /tmp is noexec on this backend; this solver needs
decompose-before-run; this image's shell sources a setup script that
mangles PATH). If anything fits, emit it in your final reply between
`<observations>` tags as a JSON list:

```
<observations>
[{
  "kind": "image_quirk",            // image_quirk | backend_quirk | workflow_pattern | service_idiom
  "scope": ["service:<name>"],      // service:<name> | backend:<name> | workflow:<shape>
  "trigger": "<command or situation that surfaced it>",
  "symptom": "<what went wrong, or what was non-obvious>",
  "fix_shape": {"destination": "dockerfile_env",  // dockerfile_env | dockerfile_run | registry_quirks | registry_parallel | prompt_compute | prompt_analyse | smoke_test | none
                "patch": "<one-line patch sketch, e.g. 'ENV OMPI_ALLOW_RUN_AS_ROOT=1'>"},
  "confidence": "high"              // high (saw symptom AND saw fix work) | medium (implied, not verified) | low (guess)
}]
</observations>
```

Empty list (or omit the block entirely) is fine — most sessions don't
surface novel lessons, and fabricating observations to look thorough is
worse than silence. Observations surface to the user as candidate
findings; they are never auto-applied to Dockerfiles, the registry, or
prompts.
"""
