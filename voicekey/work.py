"""One bounded execution slot for an operation that may ignore its timeout.

The caller owns deadlines. A timed-out task keeps its slot until it returns;
we never replace a stuck thread with another thread sharing the same model.
Late results are discarded. These threads are daemons because native inference
cannot be forcibly unwound safely in Python.
"""
from __future__ import annotations

import threading
import time


class WorkTimeout(Exception):
    pass


class WorkBusy(Exception):
    pass


class Slot:
    def __init__(self, name: str):
        self.name = name
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._running = False

    @property
    def busy(self) -> bool:
        with self._lock:
            return self._running

    def call(self, function, deadline: float):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise WorkTimeout(self.name)
        done = threading.Event()
        result, errors = [], []

        def run():
            try:
                if time.monotonic() >= deadline:
                    raise WorkTimeout(f"{self.name} expired before starting")
                result.append(function())
            except BaseException as exc:
                errors.append(exc)
            finally:
                with self._lock:
                    self._running = False
                done.set()

        with self._lock:
            if self._running:
                raise WorkBusy(self.name)
            self._running = True
            self._thread = threading.Thread(target=run, name=self.name, daemon=True)
            self._thread.start()
        if not done.wait(max(0.0, deadline - time.monotonic())):
            raise WorkTimeout(self.name)
        if time.monotonic() >= deadline:
            raise WorkTimeout(self.name)
        if errors:
            if isinstance(errors[0], Exception):
                raise errors[0]
            raise RuntimeError(f"{self.name} exited: {errors[0]}") from errors[0]
        return result[0]

    def join(self, timeout: float = 0.0) -> None:
        with self._lock:
            thread = self._thread
        if thread is not None:
            thread.join(max(0.0, timeout))
