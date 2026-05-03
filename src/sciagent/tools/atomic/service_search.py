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

from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml

from ..registry import BaseTool, ToolResult


class ServiceSearchTool(BaseTool):
    name = "service_search"
    description = (
        "Search the sciagent service registry for a keyword (case-insensitive). "
        "Scans each service's name, description, packages, and capabilities. "
        "Use this BEFORE assuming a package/tool is missing from the registry — "
        "the YAML is large and case-sensitive grep / truncated reads miss "
        "services. Returns matching service names with description, packages, "
        "and a short capability blurb so you can pick one and move on to writing "
        "the run script. Empty result means it really isn't there; only then "
        "consider build-service."
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
        for name, entry in services.items():
            if not isinstance(entry, dict):
                continue
            haystack = self._service_haystack(name, entry)
            if needle in haystack:
                matches.append(
                    {
                        "name": name,
                        "description": entry.get("description") or "",
                        "image": entry.get("image") or "",
                        "extends": entry.get("extends"),
                        "packages": list(entry.get("packages") or []),
                        # Cap capabilities to keep the result token-light;
                        # full entry is one Read away if the agent wants more.
                        "capabilities": list((entry.get("capabilities") or [])[:6]),
                    }
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
