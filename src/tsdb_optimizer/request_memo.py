"""Bounded memoization of deterministic facts within one synchronous request.

Never stores strategy eligibility, resource state, database results or advice.
The context is discarded on success and failure; no cross-request trust.
"""
import logging
import time
from contextvars import ContextVar
from functools import wraps

_current = ContextVar('tsdb_request_facts', default=None)
MAX_ENTRIES = 32
MAX_SQL_CHARS = 65536


def request_scope(function):
    @wraps(function)
    def wrapped(*args, **kwargs):
        token = _current.set({})
        try:
            return function(*args, **kwargs)
        finally:
            _current.reset(token)
    return wrapped


def bucket(name, sql):
    state = _current.get()
    if state is None or not isinstance(sql, str) or len(sql) > MAX_SQL_CHARS:
        return None
    return state.setdefault(name, {})


def remember(cache, key, value):
    if cache is not None and len(cache) < MAX_ENTRIES:
        cache[key] = value


def execution_store_session(function):
    """Reuse the existing connection session for OptimizationRun services.

    The caller's outer E2E timer includes opening and closing. The report's
    historical internal cutoff remains unchanged, as does its stored value.
    """
    @wraps(function)
    def wrapped(self, *args, **kwargs):
        result = None
        try:
            with self.store.request_session():
                result = function(self, *args, **kwargs)
        except Exception as exc:
            if result is None:
                raise
            # A completed query must not be dispatched again due to cleanup.
            logging.getLogger(__name__).exception('request store cleanup failed')
            result.execution.warnings.append('STORE_SESSION_CLOSE_FAILED: '+type(exc).__name__)
        return result
    return wrapped


def store_session(function):
    @wraps(function)
    def wrapped(self, *args, **kwargs):
        result = None
        started = time.perf_counter_ns()
        try:
            with self.store.request_session():
                opened = time.perf_counter_ns()
                result = function(self, *args, **kwargs)
                completed = time.perf_counter_ns()
        except Exception as exc:
            if result is None:
                raise
            logging.getLogger(__name__).exception('request store cleanup failed')
            result.trace.warnings.append('STORE_SESSION_CLOSE_FAILED: '+type(exc).__name__)
        closed = time.perf_counter_ns()
        from .orchestration.models import RequestTiming
        result.request_timing = RequestTiming(
            store_open_ms=(opened-started)/1e6, body_ms=(completed-opened)/1e6,
            store_close_ms=(closed-completed)/1e6, total_ms=(closed-started)/1e6)
        return result
    return wrapped
