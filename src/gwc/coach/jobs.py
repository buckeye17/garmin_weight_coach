"""Background jobs for LLM calls, so a long Claude turn never blocks a web request.

Pages start a job, then poll it on a `dcc.Interval` for streamed text, tool
activity and the final result. Jobs live in memory; results that matter (chat
replies, summaries) are written to the database by the job itself.
"""

import logging
import threading
import time
import uuid
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Any

from gwc.coach.llm import LLMError

log = logging.getLogger(__name__)

KEEP_JOBS = 50


@dataclass
class Job:
    id: str
    kind: str
    key: str | None = None  # e.g. the conversation id, so one runs at a time per key
    status: str = "running"  # running | done | error
    text: str = ""  # streamed text of the current assistant turn
    activity: str = "Thinking…"
    result: Any = None
    error: str | None = None
    started: float = field(default_factory=time.monotonic)

    @property
    def running(self) -> bool:
        return self.status == "running"

    @property
    def elapsed(self) -> float:
        return time.monotonic() - self.started

    # Callbacks wired into LLMRequest.
    def on_text(self, delta: str) -> None:
        self.text += delta
        self.activity = "Writing…"

    def on_turn(self) -> None:
        self.text = ""

    def on_activity(self, message: str) -> None:
        self.activity = message


class JobRunner:
    def __init__(self) -> None:
        self._jobs: OrderedDict[str, Job] = OrderedDict()
        self._lock = threading.Lock()

    def start(self, kind: str, fn: Callable[[Job], Any], key: str | None = None) -> Job:
        with self._lock:
            if key is not None:
                for job in self._jobs.values():
                    if job.key == key and job.running:
                        return job
            job = Job(id=uuid.uuid4().hex, kind=kind, key=key)
            self._jobs[job.id] = job
            while len(self._jobs) > KEEP_JOBS:
                self._jobs.popitem(last=False)
        threading.Thread(
            target=self._run, args=(job, fn), name=f"coach-{kind}", daemon=True
        ).start()
        return job

    def _run(self, job: Job, fn: Callable[[Job], Any]) -> None:
        try:
            job.result = fn(job)
            job.status = "done"
        except LLMError as e:
            job.error, job.status = str(e), "error"
        except Exception as e:
            log.exception("Coach job %s failed", job.kind)
            job.error, job.status = f"Something went wrong: {e}", "error"

    def get(self, job_id: str | None) -> Job | None:
        return self._jobs.get(job_id) if job_id else None

    def running(self, kind: str) -> list[Job]:
        return [j for j in self._jobs.values() if j.kind == kind and j.running]


@lru_cache
def get_jobs() -> JobRunner:
    return JobRunner()
