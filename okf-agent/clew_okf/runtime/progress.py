"""Optional workflow progress callbacks, isolated per worker execution context."""

from contextlib import contextmanager
from contextvars import ContextVar
from typing import Callable

ProgressCallback = Callable[[str, str, dict], None]
_callback: ContextVar[ProgressCallback | None] = ContextVar("clew_workflow_progress", default=None)


def emit(stage: str, message: str, **details) -> None:
    callback = _callback.get()
    if callback is not None:
        callback(stage, message, details)


@contextmanager
def reporting(callback: ProgressCallback):
    token = _callback.set(callback)
    try:
        yield
    finally:
        _callback.reset(token)
