"""Track background work so reset cannot race old workers or queued callbacks."""
from contextlib import contextmanager
from contextvars import ContextVar
from functools import wraps
import itertools
import threading

from .errors import UserError

_lock = threading.RLock()
_generation = 0
_ids = itertools.count(1)
_active = {}
_context = ContextVar("salesteam_background", default=None)


class StaleOperation(RuntimeError):
    """A callback belongs to data that has already been reset."""


def capture():
    context = _context.get()
    if context is not None:
        return context[0]
    with _lock:
        return _generation


@contextmanager
def operation(name="background", token=None):
    context = _context.get()
    token = capture() if token is None else token
    with _lock:
        if token != _generation:
            raise StaleOperation("background operation belongs to cleared data")
        if context is not None:
            if context[0] != token:
                raise StaleOperation("background operation generation mismatch")
            ident = None
        else:
            ident = next(_ids)
            _active[ident] = {"id": ident, "name": name, "thread": threading.current_thread().name}
    marker = _context.set((token, ident)) if ident is not None else None
    try:
        yield
    finally:
        if marker is not None:
            _context.reset(marker)
            with _lock:
                _active.pop(ident, None)


def tracked(name):
    def decorate(fn):
        @wraps(fn)
        def wrapped(*args, **kwargs):
            with operation(name):
                return fn(*args, **kwargs)
        return wrapped
    return decorate


def spawn(fn, *args, name="background", **kwargs):
    token = capture()

    def work():
        try:
            with operation(name, token=token):
                fn(*args, **kwargs)
        except StaleOperation:
            return
    thread = threading.Thread(target=work, daemon=True, name=name)
    thread.start()
    return thread


def state():
    with _lock:
        return {"generation": _generation, "active": [dict(v) for v in _active.values()]}


class _Reset:
    def __init__(self):
        self.committed = False

    def commit(self):
        global _generation
        if not self.committed:
            _generation += 1
            self.committed = True


@contextmanager
def reset_guard():
    # Never wait for workers here: one may be waiting for the task/schedule lock.
    # Holding this short lock across the SQL reset also blocks new registrations.
    with _lock:
        if _active:
            raise UserError("task_already_running")
        yield _Reset()
