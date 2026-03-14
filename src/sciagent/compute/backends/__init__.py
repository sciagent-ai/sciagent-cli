"""
Compute backends for sciagent.

MVP: LocalBackend only (Docker via ProcessManager)
Future: SkyPilotBackend, ModalBackend
"""

from .local import LocalBackend

__all__ = ["LocalBackend"]
