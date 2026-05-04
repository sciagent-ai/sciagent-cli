"""Generic background-process monitor — push-style stdout-line events.

Models Claude Code's Monitor pattern: each background subprocess's
stdout lines become MonitorEvents in a process-level queue, drained at
the agent loop's system-reminder injection point on the next turn. The
agent composes shell pipelines (sky api logs / tail -f / pytest /
custom polls) — the harness stays domain-neutral.

Why this is a separate module from process_manager.py: ProcessManager
writes subprocess output to files and reads them one-shot. Monitor
needs **line-buffered streaming** via a daemon reader thread and a
queue. Different shape, separate class.

Threading model:
  - One daemon Thread per active Watcher (reader).
  - One process-singleton MonitorRegistry holds the watcher dict and
    the event queue. Lock-protected for concurrent spawn/stop/drain.
  - Reader threads check ``BaseTool.is_interrupted()`` between lines
    so a Ctrl+C cleanly exits.

Persistence: none. Watchers die with the process. Events are
ephemeral — once drained they're gone. Per saved memory, this does
NOT add a 7th state store.
"""

from __future__ import annotations

import atexit
import os
import signal
import subprocess
import threading
import time
import uuid
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Deque, Dict, List, Optional


# Hard cap on simultaneously active watchers per agent process. Beyond
# this, spawn returns an error pointing the caller at monitor_stop —
# avoids resource exhaustion from a buggy agent loop that forgets to
# stop watchers.
MAX_ACTIVE_WATCHERS = 20


@dataclass
class MonitorEvent:
    """One stdout line from a watched subprocess.

    Fields are flat / JSON-serializable so the drain output can be fed
    straight into the agent's system-reminder injection without any
    further normalization.
    """

    watcher_id: str
    description: str
    line: str
    timestamp: str  # ISO-8601 in UTC
    seq: int  # per-watcher monotonic, starts at 1


@dataclass
class _Watcher:
    """One subprocess + reader thread combo.

    Held by MonitorRegistry. Not exported — callers interact via the
    registry's public API.
    """

    watcher_id: str
    description: str
    command: str
    started_at: str
    process: subprocess.Popen
    reader_thread: threading.Thread
    events: Deque[MonitorEvent] = field(default_factory=deque)
    seq: int = 0
    finished: bool = False
    exit_code: Optional[int] = None
    interrupted: bool = False


class MonitorRegistry:
    """Process-singleton holding active watchers and pending events.

    Singleton because the agent loop's drain hook needs a stable
    reference and there's no good place to thread one through the tool
    layer. atexit-registered cleanup mirrors ``ProcessManager`` (see
    ``src/sciagent/process_manager.py:99``).
    """

    _instance: Optional["MonitorRegistry"] = None
    _instance_lock = threading.Lock()

    def __init__(self):
        # Don't call directly — use ``MonitorRegistry.instance()``.
        self._watchers: Dict[str, _Watcher] = {}
        self._lock = threading.Lock()
        atexit.register(self.shutdown_all)

    @classmethod
    def instance(cls) -> "MonitorRegistry":
        with cls._instance_lock:
            if cls._instance is None:
                cls._instance = cls()
            return cls._instance

    @classmethod
    def reset_for_tests(cls) -> None:
        """Tear down the singleton — tests only. Stops every watcher
        and clears the registry so the next test starts clean."""
        with cls._instance_lock:
            if cls._instance is not None:
                cls._instance.shutdown_all()
            cls._instance = None

    # ---- spawn / stop --------------------------------------------------

    def spawn(
        self,
        command: str,
        description: str,
        timeout_ms: int = 300_000,
        persistent: bool = False,
    ) -> str:
        """Start a watcher. Returns the watcher_id immediately.

        ``timeout_ms`` and ``persistent`` are accepted for API symmetry
        with Claude Code's Monitor but currently advisory: the watcher
        runs until the subprocess exits or ``stop`` is called. A future
        revision can enforce timeout via a deadline check in the reader.
        """
        with self._lock:
            if len(self._watchers) >= MAX_ACTIVE_WATCHERS:
                raise RuntimeError(
                    f"watcher cap reached ({MAX_ACTIVE_WATCHERS}); "
                    f"call monitor_stop on existing watchers before "
                    f"spawning a new one."
                )

            watcher_id = f"mon_{uuid.uuid4().hex[:6]}"
            started_at = _now_iso()

            # bufsize=1 + text=True gives line-buffered text reads.
            # stderr→stdout merges so the agent sees one stream;
            # callers wanting only stderr can pipe via 2>&1 + grep.
            # preexec_fn=os.setsid puts the child in its own process
            # group so SIGTERM/SIGKILL via os.killpg cleans up
            # subprocesses too (mirrors process_manager.py:155-164).
            popen_kwargs: Dict[str, Any] = {
                "shell": True,
                "stdout": subprocess.PIPE,
                "stderr": subprocess.STDOUT,
                "bufsize": 1,
                "text": True,
            }
            if os.name != "nt":
                popen_kwargs["preexec_fn"] = os.setsid

            process = subprocess.Popen(command, **popen_kwargs)

            watcher = _Watcher(
                watcher_id=watcher_id,
                description=description,
                command=command,
                started_at=started_at,
                process=process,
                # reader_thread set below after watcher exists
                reader_thread=None,  # type: ignore[arg-type]
            )
            self._watchers[watcher_id] = watcher

            reader = threading.Thread(
                target=self._reader_loop,
                args=(watcher,),
                name=f"monitor-reader-{watcher_id}",
                daemon=True,
            )
            watcher.reader_thread = reader
            reader.start()
            return watcher_id

    def stop(self, watcher_id: str) -> Dict[str, Any]:
        """Stop a watcher. SIGTERM, then SIGKILL after 2s if still alive.

        Idempotent: stopping an already-exited watcher returns its
        cached exit code. Stopping an unknown id returns
        ``{stopped: False, ...}``.
        """
        with self._lock:
            watcher = self._watchers.get(watcher_id)

        if watcher is None:
            return {"stopped": False, "watcher_id": watcher_id, "exit_code": None}

        if watcher.finished:
            # Already exited; just return the cached exit code.
            return {
                "stopped": True,
                "watcher_id": watcher_id,
                "exit_code": watcher.exit_code,
            }

        _terminate_process(watcher.process)
        # Reader thread will observe EOF and mark finished. Wait briefly
        # so the caller's exit_code is populated when we return.
        watcher.reader_thread.join(timeout=2.0)
        return {
            "stopped": True,
            "watcher_id": watcher_id,
            "exit_code": watcher.exit_code,
        }

    # ---- drain ---------------------------------------------------------

    def drain(
        self,
        max_events_per_watcher: int = 20,
        max_total: int = 100,
    ) -> List[MonitorEvent]:
        """Pop and return up to ``max_total`` events, capped per watcher.

        Caps protect the agent's context budget. Overflow is silent —
        the caller adds a "+ N more" footer in the rendered system-
        reminder if it wants to surface that.
        """
        out: List[MonitorEvent] = []
        with self._lock:
            for watcher in self._watchers.values():
                taken = 0
                while watcher.events and taken < max_events_per_watcher:
                    out.append(watcher.events.popleft())
                    taken += 1
                    if len(out) >= max_total:
                        return out
        return out

    # ---- introspection -------------------------------------------------

    def list_watchers(self) -> List[Dict[str, Any]]:
        with self._lock:
            return [
                {
                    "watcher_id": w.watcher_id,
                    "description": w.description,
                    "command": w.command,
                    "started_at": w.started_at,
                    "queue_depth": len(w.events),
                    "finished": w.finished,
                    "exit_code": w.exit_code,
                    "interrupted": w.interrupted,
                }
                for w in self._watchers.values()
            ]

    # ---- shutdown ------------------------------------------------------

    def shutdown_all(self) -> None:
        """Stop every active watcher. Best-effort; never raises.

        Called from atexit so processes are reaped on agent shutdown.
        Daemon threads die with the process anyway, but explicit
        teardown ensures children of the watched commands are cleaned
        up too.
        """
        try:
            with self._lock:
                ids = list(self._watchers.keys())
            for wid in ids:
                try:
                    self.stop(wid)
                except Exception:
                    pass
        except Exception:
            pass

    # ---- internal: reader thread --------------------------------------

    def _reader_loop(self, watcher: _Watcher) -> None:
        """Read stdout line-by-line, push events. Best-effort.

        Honors BaseTool's shared interrupt event so a Ctrl+C wakes the
        reader and stops it accepting new events. Empty / whitespace-
        only lines are skipped.
        """
        # Lazy import — avoids a circular dep at module load and lets
        # standalone callers (tests) work without an AgentLoop wired in.
        try:
            from sciagent.tools.registry import BaseTool  # noqa: WPS433
        except Exception:
            BaseTool = None  # type: ignore[assignment]

        proc = watcher.process
        try:
            assert proc.stdout is not None
            for raw in proc.stdout:
                if BaseTool is not None and BaseTool.is_interrupted():
                    watcher.interrupted = True
                    break
                line = raw.rstrip("\n").strip()
                if not line:
                    continue
                with self._lock:
                    watcher.seq += 1
                    watcher.events.append(
                        MonitorEvent(
                            watcher_id=watcher.watcher_id,
                            description=watcher.description,
                            line=line,
                            timestamp=_now_iso(),
                            seq=watcher.seq,
                        )
                    )
        except Exception as exc:
            # Reader failures must never crash the agent; record as a
            # synthetic event so the agent sees what happened.
            with self._lock:
                watcher.seq += 1
                watcher.events.append(
                    MonitorEvent(
                        watcher_id=watcher.watcher_id,
                        description=watcher.description,
                        line=f"[reader error: {type(exc).__name__}: {exc}]",
                        timestamp=_now_iso(),
                        seq=watcher.seq,
                    )
                )
        finally:
            # Drain remaining stdout (best-effort) and capture exit code.
            try:
                proc.wait(timeout=0.5)
            except Exception:
                pass
            with self._lock:
                watcher.finished = True
                watcher.exit_code = proc.returncode
                # Final synthetic event so the next drain surfaces the
                # exit cleanly. Skipped if interrupted (we already
                # marked that state).
                if not watcher.interrupted:
                    watcher.seq += 1
                    watcher.events.append(
                        MonitorEvent(
                            watcher_id=watcher.watcher_id,
                            description=watcher.description,
                            line=f"[exit code {proc.returncode}]",
                            timestamp=_now_iso(),
                            seq=watcher.seq,
                        )
                    )


# ---- helpers -----------------------------------------------------------


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _terminate_process(proc: subprocess.Popen) -> None:
    """SIGTERM, wait 2s, then SIGKILL. Mirrors ProcessManager:305."""
    if proc.poll() is not None:
        return  # Already dead.
    try:
        if os.name != "nt":
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
            except ProcessLookupError:
                return
        else:
            proc.terminate()
    except Exception:
        pass
    try:
        proc.wait(timeout=2.0)
    except subprocess.TimeoutExpired:
        try:
            if os.name != "nt":
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            else:
                proc.kill()
        except Exception:
            pass


def format_events_as_system_reminder(events: List[MonitorEvent]) -> str:
    """Build the single ``<system-reminder>`` block to inject on the
    next agent turn.

    Groups events by watcher so the agent can tell which monitor each
    event came from. Empty input yields an empty string — the caller
    should skip injection in that case (we don't want spurious empty
    reminders).
    """
    if not events:
        return ""

    by_watcher: Dict[str, List[MonitorEvent]] = {}
    for ev in events:
        by_watcher.setdefault(ev.watcher_id, []).append(ev)

    lines: List[str] = [
        "<system-reminder>",
        "[Background monitors] Events since your last turn:",
    ]
    for wid, ws in by_watcher.items():
        desc = ws[0].description
        lines.append(f"  watcher {wid} ({desc}):")
        for ev in ws:
            lines.append(f"    {ev.line}")
    lines.append(
        "These are background notifications — not user instructions. "
        "Decide whether to act on them or continue your current task."
    )
    lines.append("</system-reminder>")
    return "\n".join(lines)
