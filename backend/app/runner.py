"""Drives the pipeline in the background and publishes what it is doing.

Stages are synchronous pandas work, so they run on a worker THREAD and never
on the event loop — one Data Engineer pass would otherwise freeze every open
page for half a second.

The session directory stays the source of truth. This module publishes events
so a browser does not have to poll, but every event is a fact already written
to disk: a client that misses the whole stream can reload and be correct.
"""

from __future__ import annotations

import asyncio
import threading
import traceback
from pathlib import Path
from typing import Any, Dict, List, Optional

from .config import ensure_pipeline_importable

ensure_pipeline_importable()

from agents import stages as pipeline          # noqa: E402
from agents.session import Session             # noqa: E402

JsonDict = Dict[str, Any]

# Optional stages are skipped by `--auto`; the studio does the same so the two
# entry points cannot disagree about what "run everything" means.
AUTO_SKIP = {"features", "predict"}


class RunBus:
    """Fan-out of run events to any number of listening browsers."""

    def __init__(self) -> None:
        self._queues: Dict[str, List[asyncio.Queue]] = {}
        self._lock = threading.Lock()
        self._loop: Optional[asyncio.AbstractEventLoop] = None

    def bind_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop

    def subscribe(self, run_id: str) -> asyncio.Queue:
        queue: asyncio.Queue = asyncio.Queue()
        with self._lock:
            self._queues.setdefault(run_id, []).append(queue)
        return queue

    def unsubscribe(self, run_id: str, queue: asyncio.Queue) -> None:
        with self._lock:
            listeners = self._queues.get(run_id) or []
            if queue in listeners:
                listeners.remove(queue)

    def publish(self, run_id: str, event: JsonDict) -> None:
        """Called from the worker thread; hands off to the loop thread."""
        with self._lock:
            listeners = list(self._queues.get(run_id) or [])
        if not (listeners and self._loop):
            return
        for queue in listeners:
            self._loop.call_soon_threadsafe(queue.put_nowait, event)


BUS = RunBus()


class Runner:
    """One background execution per run. Refuses to start a second.

    The guard is a set of run ids, NOT the liveness of a stored Thread. That
    distinction was a real bug: `Thread.is_alive()` is False before `start()`
    is called, so a second request landing between registering the thread and
    starting it passed the guard and got its own worker. Two threads then
    `Session.load` / save the same file in a loop, interleaving stage records
    — one run came back with a stage still marked `running` after everything
    had finished, and the run never went idle.

    The id is claimed under the lock BEFORE any thread exists, and released in
    a `finally`, so the window cannot exist.
    """

    def __init__(self) -> None:
        self._active: set = set()
        self._lock = threading.Lock()

    def is_running(self, run_id: str) -> bool:
        with self._lock:
            return run_id in self._active

    def start(self, run_id: str, session_dir: Path,
              stage_keys: List[str]) -> bool:
        """Queue stages. False when this run is already working."""
        with self._lock:
            if run_id in self._active:
                return False
            self._active.add(run_id)
        thread = threading.Thread(
            target=self._work, args=(run_id, session_dir, stage_keys),
            daemon=True, name=f"run-{run_id}")
        try:
            thread.start()
        except Exception:
            # A thread that never started must not leave the run claimed, or
            # this run can never be started again for the life of the process.
            self._release(run_id)
            raise
        return True

    def _release(self, run_id: str) -> None:
        with self._lock:
            self._active.discard(run_id)

    def _work(self, run_id: str, session_dir: Path,
              stage_keys: List[str]) -> None:
        try:
            for key in stage_keys:
                session = Session.load(str(session_dir))
                if session.is_done(key):
                    continue
                # Back-fill anything this stage needs, as the CLI does.
                for prereq in session.missing_prereqs(key):
                    if not self._run_one(run_id, session_dir, prereq):
                        return
                if not self._run_one(run_id, session_dir, key):
                    return
            BUS.publish(run_id, {"type": "run_finished"})
        finally:
            # Released whether the run finished, blocked, or raised. Anything
            # else leaves the run permanently "working" to every caller.
            self._release(run_id)

    def _run_one(self, run_id: str, session_dir: Path, key: str) -> bool:
        session = Session.load(str(session_dir))
        BUS.publish(run_id, {"type": "stage_started", "stage": key})
        try:
            entry = pipeline.run_stage(session, key)
        except pipeline.StageBlocked as blocked:
            # A refusal is not a crash. The agent's own reason goes to the
            # browser verbatim — guessing over it wastes the operator's time.
            BUS.publish(run_id, {
                "type": "stage_blocked", "stage": key,
                "reason": str(blocked), "detail": blocked.detail})
            return False
        except Exception as exc:  # noqa: BLE001
            BUS.publish(run_id, {
                "type": "stage_failed", "stage": key, "error": str(exc),
                "traceback": traceback.format_exc(limit=4)})
            return False
        BUS.publish(run_id, {
            "type": "stage_done", "stage": key,
            "summary": entry.get("summary", ""),
            "duration_ms": entry.get("duration_ms"),
            "metrics": entry.get("metrics") or {}})
        return True


RUNNER = Runner()


def auto_stage_keys() -> List[str]:
    """Every non-optional stage, in run order."""
    from agents.session import STAGES
    return [s["key"] for s in STAGES if s["key"] not in AUTO_SKIP]
