"""Connection-owned capture using the daemon's recorder and shared pipeline."""
from __future__ import annotations

import math
from pathlib import Path
import sys
import threading
import time

from .capture import Session
from .recorder import Recorder
from .target import Landing, Outcome, Target, Window


class ClientTarget(Target):
    kind = "client"
    clipboard_fallback = False

    def __init__(self, control, client, request_id, capture_id):
        super().__init__(None, Window(None, False), None)
        self.control, self.client = control, client
        self.request_id, self.capture_id = request_id, capture_id
        self._result_lock = threading.Lock()
        self.terminal = False
        self.discarded = False

    def show(self, text):
        pass

    def clear(self):
        pass

    def describe(self):
        return "control socket client"

    def event(self, event_type, **fields):
        return {'type': event_type, 'id': self.request_id,
                'capture_id': self.capture_id, **fields}

    def progress(self, state, **fields):
        if not self.terminal:
            self.control.emit(self.client, self.event('capture-progress', state=state, **fields))

    def fail(self, error, reason):
        with self._result_lock:
            if self.terminal:
                return False
            self.terminal = True
            self.control.emit(self.client, self.event('capture-result', text=None, error=error, reason=reason))
            return True

    def discard(self):
        with self._result_lock:
            if self.terminal:
                raise ValueError("Capture result already committed")
            self.discarded = True
            self.cancelled.set()
            self.terminal = True
            self.control.emit(self.client, self.event('capture-result', text=None,
                                                     error='cancelled', reason='Capture cancelled'))

    def _land(self, text, deadline, operation_id, prefix):
        with self._result_lock:
            if self.terminal or self.cancelled.is_set():
                return Landing(reason="client capture cancelled")
            if self.client.fileno() < 0:
                return Landing(reason="client disconnected")
            self.terminal = True
            receipt = self.control.emit(self.client,
                self.event('capture-result', text=text, error=None, reason=None),
                deadline=deadline, cancelled=self.cancelled)
        receipt.done.wait(max(0, deadline - time.monotonic()))
        if receipt.sent:
            # The socket accepted the bytes; application consumption is unknown.
            return Landing(Outcome.SUBMITTED)
        self.cancelled.set()
        return Landing(reason="client disconnected or delivery timed out")


class ClientCapture:
    @staticmethod
    def arguments(args, cfg):
        if set(args) - {'seconds', 'wav'}:
            raise ValueError("Unknown capture-start argument")
        seconds = args.get('seconds', cfg.max_seconds)
        if (isinstance(seconds, bool) or not isinstance(seconds, (int, float))
                or seconds <= 0 or isinstance(seconds, float) and not math.isfinite(seconds)):
            raise ValueError("seconds must be a positive finite number")
        wav = args.get('wav')
        if wav is not None and (not isinstance(wav, str) or not Path(wav).is_absolute() or '\0' in wav):
            raise ValueError("wav must be an absolute local path")
        return min(seconds, cfg.max_seconds), wav

    def __init__(self, daemon, client, request_id, identity, seconds, wav):
        self.daemon = daemon
        self.id, self.client, self.seconds = identity, client, seconds
        self.session = Session('dictate', 'client', frozenset(), 'client', identity=identity)
        self.session.target = self.target = ClientTarget(daemon.control, client, request_id, identity)
        # A client has no ageing window binding. Include finalization and text
        # processing in addition to the configured recognition budget.
        self.session.processing_seconds = (daemon.cfg.pipeline.transcription_seconds
                                           + daemon.cfg.polish.max_wait_seconds + 10)
        self.session.discard = False
        self.recorder = (Recorder([sys.executable, '-m', 'voicekey.replay', wav])
                         if wav else daemon.recorder_factory())
        self.started = self.submitted = False
        self.finish_requested = False

    @property
    def listening(self):
        return not self.submitted and not self.session.discard

    def finish(self):
        self.finish_requested = True
        if self.started and not self.submitted:
            self.recorder.request_stop()
            self.submitted = True
            self.target.progress('transcribing')
            self.daemon.pipeline.submit(self.session, self.recorder, time.monotonic())

    def cancel(self):
        self.target.discard()
        self.session.discard = True
        if not self.started:
            self.daemon.pipeline.ledger.complete(self.id, 'dropped')
        else:
            self.finish()

    def completed(self, outcome, reason):
        self.target.fail('no_speech' if outcome == 'dropped' else 'capture_failed',
                         reason or ('No speech recognized' if outcome == 'dropped' else str(outcome)))

    def tick(self):
        if not self.started and not self.session.discard:
            self.started = True
            try:
                if self.daemon.model_state == 'loading':
                    self.target.progress('loading')
                self.recorder.max_samples = int(self.seconds * 16000)
                self.recorder.start(self.session.feed)
                self.target.progress('recording', models=self.daemon.model_state)
            except Exception as exc:
                self.target.fail('capture_failed', f'Recording could not start: {exc}')
                self.daemon.pipeline.ledger.complete(self.id, 'failed')
                return
        if not self.submitted and not self.session.discard and (
                self.finish_requested or self.client.fileno() < 0 or self.recorder.finished
                or self.recorder.elapsed >= self.seconds):
            self.finish()
