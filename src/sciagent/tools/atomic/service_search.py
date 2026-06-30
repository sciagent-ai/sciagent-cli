"""Service registry search — case-insensitive, no YAML reading required.

Discovery shortcut: instead of reading the full registry.yaml (1000+ lines,
truncated by file_ops at 200 lines, missed by case-sensitive grep), the
agent calls ``service_search(keyword)`` and gets back the matching service
names with their description, package list, and capabilities. Eliminates
three failure modes at once:
  - file_ops truncating the YAML mid-file and hiding services past the cut
  - grep on the wrong case (registry keys are lowercase, prose is mixed)
  - skipping the use-service skill entirely and giving up after one read

Returns metadata sufficient for the agent to pick the right service and
move on to writing the run script. Does NOT replace reading the full entry
when the agent needs the example/dockerfile/resources — it's the discovery
step that comes before that.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml

from ..registry import BaseTool, ToolResult


# Token boundary for the multi-word fallback path. We split on runs of
# non-alphanumeric so service names like ``openfoam-swak4foam-2012`` and
# capability prose ("Molecular dynamics, MD simulation") both yield clean
# tokens. Lowercased upstream — the regex stays ASCII.
_TOKEN_RE = re.compile(r"[a-z0-9]+")


def _tokenize(text: str) -> List[str]:
    """Lowercase + split on non-alphanumeric. Empty input → empty list."""
    return _TOKEN_RE.findall(text.lower())


# Suffixes we DO NOT strip a trailing 's' from. These are common
# false-friends where the word ends in 's' but is not a plural:
#   - 'ss': process, class, glass, boss
#   - 'us': focus, status, abacus
#   - 'is': analysis, basis, axis, thesis
#   - 'os': bios, chaos
#   - 'as': atlas, gas, canvas
# Conservative on purpose — the token-fallback path is forgiving enough that
# missing a plural strip is fine, but over-stripping ("analysis" → "analysi")
# introduces silent false positives that are hard to debug.
_NON_PLURAL_S_TAIL = ("ss", "us", "is", "os", "as")


def _singularize(token: str) -> str:
    """Guarded plural→singular. Handles the common English patterns:

      - ``frequencies`` → ``frequency`` (ies → y), len > 4 guard.
      - ``classes`` / ``processes`` → ``class`` / ``process`` (sses → ss).
      - ``boxes`` / ``matches`` / ``brushes`` / ``buzzes`` → strip ``es``.
      - Generic trailing ``s`` strip with the ss/us/is/os/as guard above.

    Returns the input untouched when no rule applies. We deliberately do not
    try to handle irregulars (man/men, child/children) — the registry uses
    standard technical vocabulary where regular plurals dominate, and an
    irregular-aware singularizer would need a wordlist that is itself a
    maintenance burden.
    """
    if len(token) > 4 and token.endswith("ies"):
        return token[:-3] + "y"
    if len(token) > 5 and token.endswith("sses"):
        return token[:-2]
    if len(token) > 4 and token.endswith(("xes", "shes", "ches", "zes")):
        return token[:-2]
    if (
        len(token) > 3
        and token.endswith("s")
        and not token.endswith(_NON_PLURAL_S_TAIL)
    ):
        return token[:-1]
    return token


class ServiceSearchTool(BaseTool):
    name = "service_search"
    description = (
        "Search the sciagent service registry for a keyword (case-insensitive). "
        "Scans each service's name, description, packages, and capabilities. "
        "Call this BEFORE writing any cloud-bound code, not just to find a "
        "service — the returned entry includes the container's `workdir`, "
        "`runtime`, and (when declared) `env_setup` / `tool_paths` / "
        "`probe_command`. Use those values in your run script so paths "
        "reflect container reality, not local guesses. If the env_setup / "
        "tool_paths fields are absent for a service, that's the signal to "
        "probe (one compute_exec) before writing the run script — don't "
        "invent paths. Empty result means it really isn't in the registry; "
        "only then consider build-service."
    )

    parameters = {
        "type": "object",
        "properties": {
            "keyword": {
                "type": "string",
                "description": (
                    "Substring to match. Case-insensitive. Matches against "
                    "service name, description, packages, and capabilities."
                ),
            },
        },
        "required": ["keyword"],
    }

    def __init__(self, registry_path: Optional[str] = None):
        if registry_path is None:
            registry_path = str(
                Path(__file__).parent.parent.parent / "services" / "registry.yaml"
            )
        self._registry_path = registry_path

    def _load(self) -> Dict[str, Any]:
        path = Path(self._registry_path)
        if not path.exists():
            return {}
        try:
            with open(path) as f:
                return yaml.safe_load(f) or {}
        except Exception:
            return {}

    @staticmethod
    def _service_haystack(name: str, entry: Dict[str, Any]) -> str:
        """Concatenate the searchable fields of a service entry. Lowercased
        once so callers can substring-match without per-call .lower() calls.
        """
        parts: List[str] = [name, entry.get("description") or ""]
        packages = entry.get("packages") or []
        if isinstance(packages, list):
            parts.extend(str(p) for p in packages)
        capabilities = entry.get("capabilities") or []
        if isinstance(capabilities, list):
            parts.extend(str(c) for c in capabilities)
        return "\n".join(parts).lower()

    # Common kwarg names the model reaches for when it forgets the exact one.
    # Accepting aliases keeps the tool useful across model retries instead of
    # surfacing an "unexpected keyword argument" error every time.
    _KEYWORD_ALIASES = ("keyword", "query", "q", "search", "name", "service", "pattern")

    def execute(self, keyword: str = "", **kwargs) -> ToolResult:
        # Pull the search term from any of the common aliases.
        if not keyword:
            for alias in self._KEYWORD_ALIASES[1:]:
                value = kwargs.get(alias)
                if isinstance(value, str) and value.strip():
                    keyword = value
                    break

        registry = self._load()
        services = registry.get("services") or {}
        if not services:
            return ToolResult(
                success=False,
                output=None,
                error=f"No services found in registry at {self._registry_path}.",
            )

        # No keyword → return a lightweight catalog (name + description) of
        # every service. Cheaper than reading the full YAML; gives the agent
        # a way to see what's available before refining a query.
        if not keyword or not keyword.strip():
            catalog = [
                {
                    "name": name,
                    "description": (entry.get("description") or "")[:120],
                }
                for name, entry in services.items()
                if isinstance(entry, dict)
            ]
            return ToolResult(
                success=True,
                output={
                    "keyword": None,
                    "match_count": len(catalog),
                    "matches": catalog,
                    "registry_path": self._registry_path,
                    "hint": (
                        "No keyword given — returning catalog. Call again with "
                        "keyword=<term> for description/packages/capabilities."
                    ),
                },
            )

        needle = keyword.strip().lower()
        matches: List[Dict[str, Any]] = []

        # Pass 1: exact substring against the joined haystack — the historical
        # behavior. Most agent queries are a single keyword that's literally
        # present in the registry, and this path keeps them stable.
        for name, entry in services.items():
            if not isinstance(entry, dict):
                continue
            haystack = self._service_haystack(name, entry)
            if needle in haystack:
                matches.append(self._summarize(name, entry, "exact", None))

        # Pass 2: token-fallback. Multi-word queries whose tokens span fields
        # ("molecular dynamics" — molecular in description, dynamics in
        # capabilities) miss the substring pass entirely. So do plural forms
        # whose haystack carries the singular ("simulations" vs "simulation").
        # We tokenize and guarded-singularize both sides, then require every
        # query token to appear as a substring of the joined haystack. Token
        # mode runs only when exact returned nothing — fallback semantics
        # keep results stable when the exact term is present.
        if not matches:
            query_tokens = [_singularize(t) for t in _tokenize(needle)]
            query_tokens = [t for t in query_tokens if t]
            if query_tokens:
                for name, entry in services.items():
                    if not isinstance(entry, dict):
                        continue
                    haystack = self._service_haystack(name, entry)
                    # Singularize the haystack tokens too so a singular query
                    # still hits a plural haystack ("simulation" finds
                    # "simulations"). We feed the singularized tokens back
                    # into a joined string and substring-test against it —
                    # cheaper than building per-token sets and equivalent for
                    # the prefix-substring queries the agent typically issues
                    # (e.g., "swak" matching "swak4foam").
                    haystack_singular = " ".join(
                        _singularize(t) for t in _tokenize(haystack)
                    )
                    if all(t in haystack_singular for t in query_tokens):
                        matches.append(
                            self._summarize(name, entry, "token", query_tokens)
                        )

        return ToolResult(
            success=True,
            output={
                "keyword": keyword,
                "match_count": len(matches),
                "matches": matches,
                "registry_path": self._registry_path,
            },
        )

    @staticmethod
    def _summarize(
        name: str,
        entry: Dict[str, Any],
        match_mode: str,
        matched_tokens: Optional[List[str]],
    ) -> Dict[str, Any]:
        """Per-service result row. ``match_mode`` is ``"exact"`` (substring on
        the joined haystack) or ``"token"`` (all query tokens present as
        substrings of the singularized haystack). ``matched_tokens`` is the
        list of singularized query tokens for token-mode hits, ``None`` for
        exact hits. Adding fields here is safe — consumers read by key.
        """
        return {
            "name": name,
            "description": entry.get("description") or "",
            "image": entry.get("image") or "",
            "extends": entry.get("extends"),
            "packages": list(entry.get("packages") or []),
            # Cap capabilities to keep the result token-light; full entry is
            # one Read away if the agent wants more.
            "capabilities": list((entry.get("capabilities") or [])[:6]),
            # Env contract — surface the registry's promise about the
            # container's runtime layout so the agent can write code against
            # observed paths instead of locally-imagined ones. Missing fields
            # fall through as null; the agent knows to probe rather than
            # assume in that case.
            "workdir": entry.get("workdir"),
            "runtime": entry.get("runtime"),
            "env_setup": entry.get("env_setup"),
            "tool_paths": entry.get("tool_paths"),
            "probe_command": entry.get("probe_command"),
            "match_mode": match_mode,
            "matched_tokens": matched_tokens,
        }


class ServiceDetailTool(BaseTool):
    name = "service_detail"
    description = (
        "Return the full registry entry for ONE service by name. Use this "
        "after `service_search` has identified the right service and you "
        "need fields it doesn't surface — full capabilities list, examples, "
        "outputs/post_processing contracts, resources, dockerfile reference. "
        "Avoids reading registry.yaml directly (which file_ops truncates "
        "past 200 lines)."
    )

    parameters = {
        "type": "object",
        "properties": {
            "name": {
                "type": "string",
                "description": "Exact service name (case-sensitive registry key).",
            },
        },
        "required": ["name"],
    }

    _NAME_ALIASES = ("name", "service", "service_name")

    def __init__(self, registry_path: Optional[str] = None):
        if registry_path is None:
            registry_path = str(
                Path(__file__).parent.parent.parent / "services" / "registry.yaml"
            )
        self._registry_path = registry_path

    def _load(self) -> Dict[str, Any]:
        path = Path(self._registry_path)
        if not path.exists():
            return {}
        try:
            with open(path) as f:
                return yaml.safe_load(f) or {}
        except Exception:
            return {}

    def execute(self, name: str = "", **kwargs) -> ToolResult:
        if not name:
            for alias in self._NAME_ALIASES[1:]:
                value = kwargs.get(alias)
                if isinstance(value, str) and value.strip():
                    name = value
                    break
        if not isinstance(name, str) or not name.strip():
            return ToolResult(
                success=False,
                output=None,
                error="name (service name) is required.",
            )
        name = name.strip()

        registry = self._load()
        services = registry.get("services") or {}
        if not services:
            return ToolResult(
                success=False,
                output=None,
                error=f"No services found in registry at {self._registry_path}.",
            )

        entry = services.get(name)
        if entry is None:
            close = [
                n for n in services.keys()
                if isinstance(n, str) and name.lower() in n.lower()
            ]
            return ToolResult(
                success=False,
                output={"name": name, "did_you_mean": close[:5]},
                error=(
                    f"Service '{name}' not found in registry. "
                    f"Try service_search for fuzzy lookup."
                ),
            )

        # Surface the full entry. Fields are passed through as-is so any
        # registry additions (outputs, post_processing, etc.) flow without
        # tool changes.
        return ToolResult(
            success=True,
            output={
                "name": name,
                "entry": entry,
                "registry_path": self._registry_path,
            },
        )
