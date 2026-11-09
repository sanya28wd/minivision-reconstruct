"""Bound stage wait time without unbounded worker creation or late publication.

A blocked OS call cannot be cancelled by Python on Windows. A timed-out daemon
worker retains its slot until it exits; it must check its deadline before further
writes/publication. An already-started atomic OS publication can finish late;
its fingerprint must be recorded before starting it. File staging belongs to
the caller and closes on timeout.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from concurrent.futures import Future
from functools import lru_cache

from incident_report.domain import InvalidIncident


@lru_cache(maxsize=1)
def _slots(workers: int) -> threading.BoundedSemaphore:
    return threading.BoundedSemaphore(workers)


def run_stage[T](name: str, seconds: float, workers: int, operation: Callable[[float], T]) -> T:
    deadline = time.monotonic() + seconds
    slots = _slots(workers)
    if not slots.acquire(timeout=seconds):
        raise InvalidIncident(f"The {name} stage timed out while waiting for capacity.")
    future: Future[T] = Future()

    def execute() -> None:
        try:
            future.set_result(operation(deadline))
        except BaseException as exc:
            future.set_exception(exc)
        finally:
            slots.release()

    threading.Thread(target=execute, daemon=True, name="evidence-" + name).start()
    try:
        return future.result(timeout=max(0, deadline - time.monotonic()))
    except TimeoutError:
        raise InvalidIncident(f"The {name} stage timed out.") from None
