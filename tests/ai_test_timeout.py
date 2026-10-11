"""Per-test timeout guard for the AI test modules.

A hang must fail loudly with a traceback, never block a run. For every TestCase in a
module, ``protect(globals())`` wraps ``run`` so that each test gets:

1. ``faulthandler.dump_traceback_later(limit)``: all thread stacks to stderr at the limit;
2. POSIX ``SIGALRM`` at the limit: raises ``TestTimeout`` in the main thread, so the test
   is reported as an error and the run continues;
3. a hard backstop at ``limit + 15`` s that dumps stacks and exits the process, in case the
   alarm cannot be delivered (blocked in C code, or not the main thread).

Limit: ``AI_TEST_TIMEOUT_SEC`` (default 30).
"""
import faulthandler
import os
import signal
import sys
import threading
import unittest

LIMIT_SEC = max(1, int(os.environ.get("AI_TEST_TIMEOUT_SEC", "30")))
BACKSTOP_EXTRA_SEC = 15


class TestTimeout(Exception):
    pass


def _wrap(cls):
    if cls.__dict__.get("_ai_timeout_wrapped"):
        return
    original_run = cls.run

    def run(self, result=None):
        can_alarm = hasattr(signal, "SIGALRM") and threading.current_thread() is threading.main_thread()
        previous = None

        def on_alarm(signum, frame):
            faulthandler.dump_traceback(file=sys.stderr, all_threads=True)
            raise TestTimeout(f"{self.id()} exceeded {LIMIT_SEC}s")

        # One timer slot: the hard backstop. It also prints every thread's stack before exiting.
        faulthandler.dump_traceback_later(LIMIT_SEC + BACKSTOP_EXTRA_SEC, exit=True, file=sys.stderr)
        if can_alarm:
            previous = signal.signal(signal.SIGALRM, on_alarm)
            signal.alarm(LIMIT_SEC)
        try:
            return original_run(self, result)
        finally:
            if can_alarm:
                signal.alarm(0)
                signal.signal(signal.SIGALRM, previous)
            faulthandler.cancel_dump_traceback_later()

    cls.run = run
    cls._ai_timeout_wrapped = True


def protect(namespace):
    """Call as ``protect(globals())`` at the end of a test module."""
    for value in list(namespace.values()):
        if isinstance(value, type) and issubclass(value, unittest.TestCase) and value.__module__ == namespace.get("__name__"):
            _wrap(value)
