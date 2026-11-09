"""Context-local wall durations, carried explicitly into analysis worker threads."""

from __future__ import annotations

import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from threading import Lock
from typing import Any


class StageTimings(dict[str, float]):
    """Completed and currently running durations; snapshots never wait for a worker."""

    def __init__(self) -> None:
        super().__init__()
        self.lock = Lock()
        self.active: dict[object, tuple[str, float]] = {}
        self.children: dict[int, StageTimings] = {}

    def snapshot(self) -> dict[str, float]:
        with self.lock:
            values = dict(self)
            now = time.perf_counter()
            for name, started in self.active.values():
                values[name] = values.get(name, 0.0) + (now - started) * 1000
            for child in self.children.values():
                for name, value in child.snapshot().items():
                    values[name] = values.get(name, 0.0) + value
            return values


_current: ContextVar[StageTimings | None] = ContextVar("stage_timings", default=None)


@contextmanager
def capture_timings(values: StageTimings | None = None, *, propagate: bool = False) -> Iterator[StageTimings]:
    values = values if values is not None else StageTimings()
    parent = _current.get() if propagate else None
    if parent is not None:
        with parent.lock:
            parent.children[id(values)] = values
    token = _current.set(values)
    try:
        yield values
    finally:
        _current.reset(token)
        if parent is not None:
            completed = values.snapshot()
            with parent.lock:
                del parent.children[id(values)]
                for name, value in completed.items():
                    parent[name] = parent.get(name, 0.0) + value


@contextmanager
def stage_timer(name: str) -> Iterator[None]:
    values = _current.get()
    span = object()
    started = time.perf_counter()
    if values is not None:
        with values.lock:
            values.active[span] = (name, started)
    try:
        yield
    finally:
        if values is not None:
            with values.lock:
                del values.active[span]
                values[name] = values.get(name, 0.0) + (time.perf_counter() - started) * 1000


def timed_call[T](name: str, operation: Callable[..., T], *args: Any, **kwargs: Any) -> T:
    with stage_timer(name):
        return operation(*args, **kwargs)
