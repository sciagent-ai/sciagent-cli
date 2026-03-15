"""
Compute backends for sciagent.

Available backends:
- LocalBackend: Docker via ProcessManager (default)
- SkyPilotBackend: Cloud GPU/large memory via SkyPilot
- ModalBackend: Serverless (future)
"""

from .local import LocalBackend

# Lazy import for SkyPilot - don't break if not installed
try:
    from .skypilot import SkyPilotBackend
except ImportError:
    SkyPilotBackend = None  # type: ignore

__all__ = ["LocalBackend", "SkyPilotBackend"]
