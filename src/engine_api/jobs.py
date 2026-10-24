"""Background job runner and registry for TeamTrau Engine API."""

from __future__ import annotations

import logging
import threading
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable, Literal

logger = logging.getLogger(__name__)

JobRunner = Callable[
    [dict[str, Any], Callable[[dict[str, Any]], None], threading.Event],
    dict[str, Any],
]
JobStatus = Literal["running", "done", "failed", "cancelled"]


@dataclass
class _JobEntry:
    job_id: str
    kind: str
    params: dict[str, Any]
    status: JobStatus = "running"
    result: Any = None
    error: str | None = None
    events: list[dict[str, Any]] = field(default_factory=list)
    cancel_event: threading.Event = field(default_factory=threading.Event)
    lock: threading.Lock = field(default_factory=threading.Lock)


class JobRegistry:
    """Thread-safe registry for background asynchronous jobs."""

    def __init__(self, max_finished: int = 200) -> None:
        self._lock = threading.Lock()
        self._jobs: dict[str, _JobEntry] = {}
        self.max_finished = max_finished

    def submit(self, kind: str, runner: JobRunner, params: dict[str, Any]) -> str:
        """Submit a job to be executed in a background daemon thread.

        Args:
            kind: Name/type of the job runner.
            runner: Callable executing the job.
            params: Parameters dictionary.

        Returns:
            Unique job_id string.
        """
        job_id = uuid.uuid4().hex[:12]
        entry = _JobEntry(
            job_id=job_id,
            kind=kind,
            params=dict(params),
        )

        with self._lock:
            finished_keys: list[str] = []
            for jid, j in self._jobs.items():
                with j.lock:
                    if j.status != "running":
                        finished_keys.append(jid)
            excess = len(finished_keys) - max(0, self.max_finished)
            if excess > 0:
                for jid in finished_keys[:excess]:
                    del self._jobs[jid]

            self._jobs[job_id] = entry

        def _worker() -> None:
            def emit(event: dict[str, Any]) -> None:
                with entry.lock:
                    entry.events.append(dict(event))

            try:
                res = runner(entry.params, emit, entry.cancel_event)
                with entry.lock:
                    if entry.cancel_event.is_set():
                        entry.status = "cancelled"
                        entry.events.append({"type": "cancelled"})
                    else:
                        entry.status = "done"
                        entry.result = res
                        entry.events.append({"type": "done", "result": res})
            except InterruptedError:
                with entry.lock:
                    entry.status = "cancelled"
                    entry.events.append({"type": "cancelled"})
            except Exception as exc:
                with entry.lock:
                    if entry.cancel_event.is_set():
                        entry.status = "cancelled"
                        entry.events.append({"type": "cancelled"})
                    else:
                        entry.status = "failed"
                        entry.error = str(exc)
                        entry.events.append({"type": "error", "error": str(exc)})
                logger.warning("Job %s (%s) failed: %s", job_id, kind, exc)

        thread = threading.Thread(target=_worker, daemon=True, name=f"job-{job_id}")
        thread.start()
        return job_id

    def get(self, job_id: str) -> dict[str, Any] | None:
        """Get snapshot of job status and events.

        Returns:
            Dictionary with job_id, kind, status, result, error, events or None.
        """
        with self._lock:
            entry = self._jobs.get(job_id)
            if entry is None:
                return None
            with entry.lock:
                return {
                    "job_id": entry.job_id,
                    "kind": entry.kind,
                    "status": entry.status,
                    "result": entry.result,
                    "error": entry.error,
                    "events": list(entry.events),
                }

    def cancel(self, job_id: str) -> bool:
        """Signal cancellation to a running job.

        Returns:
            True if cancellation was signalled to a running job, False otherwise.
        """
        with self._lock:
            entry = self._jobs.get(job_id)
            if entry is None:
                return False
            with entry.lock:
                if entry.status == "running":
                    entry.cancel_event.set()
                    return True
                return False
