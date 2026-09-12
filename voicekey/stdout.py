"""One bounded recording through the ordinary pipeline, delivered to stdout."""
from __future__ import annotations

import contextlib
import signal
import sys
import threading
import time
from dataclasses import replace
from pathlib import Path

from .backends import create_backend
from .capture import Session
from .pipeline import Pipeline
from .polish import diagnostic_polisher
from .recorder import Recorder
from .recovery import Journal, STATE_DIR
from .target import Landing, Outcome


class StdoutTarget:
    kind = "stdout"
    app_id = None
    window_id = None
    clipboard_fallback = False

    def __init__(self):
        self.cancelled = threading.Event()
        self.delivered = False

    def show(self, text):
        pass

    def clear(self):
        pass

    def cancel(self):
        self.cancelled.set()

    def describe(self):
        return "standard output"

    def land(self, text, deadline, **kwargs):
        if self.cancelled.is_set() or time.monotonic() >= deadline:
            return Landing(reason="stdout delivery expired")
        sys.stdout.write(text)
        sys.stdout.flush()
        self.delivered = True
        return Landing(Outcome.CONFIRMED)


def capture(cfg, *, wav=None, seconds=None) -> int:
    """Ctrl-C finishes recording; SIGTERM aborts. No keyboard or desktop access."""
    stopped = threading.Event()
    aborted = threading.Event()

    def stop(signum, frame):
        stopped.set()
        if signum == signal.SIGTERM:
            aborted.set()

    previous = {sig: signal.signal(sig, stop) for sig in (signal.SIGINT, signal.SIGTERM)}
    recorder = Recorder([sys.executable, "-m", "voicekey.replay", wav]) if wav else Recorder()
    pipeline = None
    try:
        backend = create_backend(cfg.backend, cfg.language)
        with diagnostic_polisher(cfg.polish) as polisher:
            # This command has no ageing desktop destination. Allow the configured
            # recognition budget, then cleanup and a bounded stdout write.
            budget = cfg.pipeline.transcription_seconds + cfg.polish.max_wait_seconds + 10
            cfg = replace(cfg, dictation=replace(cfg.dictation, max_delay_seconds=budget))
            pipeline = Pipeline(cfg, backend=lambda: backend, polisher=lambda: polisher,
                                notifier=lambda *args, **kwargs: None,
                                journal=Journal(str(Path(STATE_DIR) / "stdout"),
                                    megabytes=cfg.pipeline.recovery_megabytes, history_days=cfg.pipeline.history_days))
            pipeline.start(recover=False)
            if stopped.is_set():
                return 130
            identity = pipeline.admit()
            if identity is None:
                raise RuntimeError("capture unavailable; check recovery storage")
            session = Session("dictate", "hold", frozenset(), "stdout", identity=identity)
            session.target = target = StdoutTarget()
            limit = min(seconds if seconds is not None else cfg.max_seconds, cfg.max_seconds)
            recorder.max_samples = int(limit * 16000)
            recorder.start(session.feed)
            print("Recording; Ctrl-C finishes, SIGTERM cancels.", file=sys.stderr)
            while not stopped.wait(0.05) and not recorder.finished and recorder.elapsed < limit:
                pass
            recorder.request_stop()
            if aborted.is_set():
                return 130
            pipeline.submit(session, recorder, time.monotonic())
            deadline = time.monotonic() + budget
            while pipeline.ledger.busy and time.monotonic() < deadline:
                if aborted.wait(0.02):
                    target.cancel()
                    return 130
            if not target.delivered:
                print(f"No transcript written; inspect {pipeline.journal.directory}", file=sys.stderr)
                return 1
            return 0
    except Exception as exc:
        print(f"voicekey capture: {exc}", file=sys.stderr)
        return 1
    finally:
        if pipeline is not None:
            pipeline.close(timeout=0)
        if recorder.active:
            with contextlib.suppress(Exception):
                recorder.stop()
        for sig, handler in previous.items():
            signal.signal(sig, handler)
