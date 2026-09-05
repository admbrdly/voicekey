"""Continuous microphone owner; audio-clock cuts feed the ordinary pipeline.

Only one segmentation worker and one supervised VAD slot exist at a time.
Recorder callbacks wake the worker; recognition, disk and desktop I/O never
run on the audio callback or key listener. Off is irreversible for a session.
"""
import logging
import threading
import time
import uuid

import numpy as np

from .capture import Session
from .notify import notify
from .recorder import AudioBuffer, CutRecording, RecordingError, SAMPLE_RATE
from .segment import Segmenter, WINDOW
from .session_target import SessionTarget
from .target import ClipboardTarget, EmacsTarget, NotifyPreview, Window
from .spacing import owed

log = logging.getLogger("voicekey.persistent")
CAPTURE_SLACK = 4.0


class PersistentSession:
    def __init__(self, cfg, pipeline, recorder, target, vad, vad_slot, streaming,
                 *, device, chord):
        self.id = uuid.uuid4().hex
        self.cfg, self.pipeline, self.recorder = cfg, pipeline, recorder
        self.device, self.chord = device, chord
        self._bind = target if callable(target) else lambda: target
        self.target = SessionTarget(self.id, ClipboardTarget(NotifyPreview("dictate"), Window(None, True), None), pipeline.ledger)
        self.ready = threading.Event()
        self.vad, self.vad_slot, self.streaming = vad, vad_slot, streaming
        self.segmenter = Segmenter(cfg.persistent)
        self.reservation = cfg.persistent.max_utterance_seconds + CAPTURE_SLACK
        self.recorder.buffer = AudioBuffer(int(self.reservation * SAMPLE_RATE))
        self.wake = threading.Event()
        self.stopping = threading.Event()
        self.done = threading.Event()
        self._stop_lock = threading.Lock()
        self.deadline = float("inf")
        self.reason = ""
        self.paused = False
        self.sequence = 0
        self.current = None
        self.active = False
        self.has_speech = False
        self.start_sample = self.live_at = 0
        self.assigned = 0
        self.last_live = None
        self.thread = None
        self._last_status = 0.0

    def _admit(self):
        identity = self.pipeline.admit(audio_seconds=self.reservation, session_id=self.id,
                                       sequence=self.sequence, gated=False)
        if identity is None:
            return None
        session = Session("dictate", "persistent", self.chord, self.device,
                          identity=identity, on_text=self.pipeline.ledger.live)
        session.session_id, session.sequence = self.id, self.sequence
        session.target = self.target.attempt(identity)
        self.sequence += 1
        return session

    def start(self):
        self.current = self._admit()
        if self.current is None:
            self.target.close()
            return False
        try:
            self.recorder.start(lambda _frame: self.wake.set())
        except Exception:
            self.pipeline.ledger.complete(self.current.id, "failed")
            self.target.close()
            raise
        self.thread = threading.Thread(target=self._run, name="persistent-capture", daemon=True)
        self.thread.start()
        self.status()
        return True

    def status(self):
        if self.stopping.is_set():
            return
        self._last_status = time.monotonic()
        notify("● Persistent listening", "press the persistent key to stop", ms=0, channel="persistent")

    def request_stop(self, reason="stopped by key", *, paused=False):
        with self._stop_lock:
            if self.stopping.is_set():
                return
            self.reason, self.paused = reason, paused
            self.deadline = time.monotonic() + self.cfg.pipeline.shutdown_seconds
            self.pipeline.stop_session(self.id, self.deadline)
            self.stopping.set()
        self.recorder.request_stop()
        self.wake.set()
        notify("⋯ Persistent dictation finishing", reason + "; microphone stopping",
               ms=0, channel="persistent")

    def tick(self):
        if self.done.is_set():
            return
        if not self.stopping.is_set():
            if self.pipeline._storage_failed:
                self.request_stop("recovery storage unavailable", paused=True)
            elif self.ready.is_set() and not self.target.available():
                self.request_stop("destination unavailable; start again to bind a destination", paused=True)
            elif time.monotonic() - self._last_status >= 30:
                self.status()
        self.target.render()

    def _begin(self, event, speech):
        self.active = True
        self.has_speech = speech and event.end > event.start
        self.start_sample = event.start
        self.live_at = event.start
        self.pipeline.ledger.speech(self.current.id)
        self.pipeline.settled()
        if self.streaming is not None and (self.last_live is None or not self.last_live.stuck):
            self.current.attach(self.streaming.session)
            self.last_live = self.current

    def _live(self, end):
        if self.active and self.current is not None and end > self.live_at:
            if self.current.live:
                samples = self.recorder.buffer.read(self.live_at, end)
                for offset in range(0, len(samples), 1600):
                    self.current.feed(samples[offset:offset + 1600])
            self.live_at = end

    def _cut(self, event):
        # Reserve the next capture before releasing the old reservation. On
        # overload, keep the whole remaining buffer in this final utterance.
        following = self._admit()
        if following is None:
            self.request_stop("pending dictation limit reached", paused=True)
            return False
        old = self.current
        samples = self.recorder.buffer.read(self.start_sample, event.end)
        old.cancel()  # suppress results from a decoder which has seen the suffix
        old.finish(timeout=0.05)
        if self.has_speech and len(samples):
            self.pipeline.submit(old, CutRecording(samples, start=self.start_sample, reason=event.kind),
                                 self.recorder.started + event.end / SAMPLE_RATE)
            self.assigned += len(samples)
        else:
            self.pipeline.ledger.complete(old.id, "dropped")
        self.recorder.buffer.discard_before(event.end)
        self.current, self.active = following, False
        self.start_sample = event.end
        return True

    def _process(self, samples):
        # The native detector is supervised in batches (~100 ms of audio).
        def classify():
            return [self.vad.speech(samples[i:i + WINDOW]) for i in range(0, len(samples), WINDOW)]
        labels = self.vad_slot.call(classify, time.monotonic() + 1.0)
        for offset, speech in zip(range(0, len(samples), WINDOW), labels):
            count = min(WINDOW, len(samples) - offset)
            if self.active and speech:
                self.has_speech = True
            events = self.segmenter.feed(count, speech)
            for event in events:
                if event.kind == "start":
                    self._begin(event, speech)
                elif not self._cut(event):
                    return False
            self._live(self.segmenter.position)
            self.recorder.buffer.discard_before(self.segmenter.owned)
        return True

    def _run(self):
        failure = ""
        try:
            self.target.target = self._bind()
            if not isinstance(self.target.target, EmacsTarget):
                self.target.target.window.verify = True
            self.target.target.prefix = owed(self.target.target.before(0),
                self.pipeline.spacing.prefix(self.target.target.window_id))
            self.current.target = self.target.attempt(self.current.id)
            self.ready.set()
            if isinstance(self.target.target, ClipboardTarget):
                raise RuntimeError("persistent mode needs an insertion destination")
            self.pipeline._save("session", lambda: self.pipeline.journal.append(
                self.id, "session-start", target=self.target.target.kind,
                pause_seconds=self.cfg.persistent.pause_seconds,
                silence_seconds=self.cfg.persistent.silence_seconds))
            self.vad_slot.call(self.vad.reset, time.monotonic() + 1.0)
            last_audio = time.monotonic()
            while True:
                self.wake.wait(0.05)
                self.wake.clear()
                available = self.recorder.buffer.end - self.segmenter.position
                if available >= WINDOW:
                    count = min(3 * WINDOW, available // WINDOW * WINDOW)
                    samples = self.recorder.buffer.read(self.segmenter.position, self.segmenter.position + count)
                    if not self._process(samples):
                        break
                    self.target.render()
                    last_audio = time.monotonic()
                    if self.segmenter.silent:
                        self.request_stop("silence timeout")
                    # Keep consuming a source which produced more than one batch.
                    self.wake.set()
                elif self.recorder.finished:
                    break
                elif time.monotonic() - last_audio > 2:
                    failure = "microphone stopped producing audio"
                    break
                if self.target.failed.is_set() or self.pipeline._storage_failed:
                    self.request_stop("delivery or recovery unavailable", paused=True)
                if self.stopping.is_set() and time.monotonic() >= self.deadline:
                    break
        except Exception as exc:
            failure = f"speech segmentation failed: {exc}"
            log.exception("continuous capture failed")
        finally:
            self.request_stop(failure or "audio source ended", paused=bool(failure))
            try:
                try:
                    self.recorder.stop()
                except RecordingError as exc:
                    failure = failure or str(exc)
                except Exception as exc:
                    failure = failure or f"capture cleanup failed: {exc}"
                self._flush(failure)
                while self._pending() and time.monotonic() < self.deadline:
                    self.target.render()
                    time.sleep(0.02)
                if self._pending():
                    self.pipeline.expire_session(self.id)
                self.pipeline._save("session", lambda: self.pipeline.journal.append(self.id, "session-closed"))
            except Exception:
                log.exception("persistent shutdown failed")
                self.pipeline._storage_failed = True
            finally:
                self.target.close()
                self.pipeline.forget_session(self.id)
                self.pipeline.settled()
                self.done.set()
                notify("Ⅱ Persistent dictation paused" if self.paused else "■ Persistent dictation off",
                       self.reason + "; press the key to start a new session", channel="persistent", ms=0)

    def _pending(self):
        return any(u.session_id == self.id for u in self.pipeline.ledger.snapshots())

    def _flush(self, failure):
        buffer = self.recorder.buffer
        # EOF may contain a final partial detector window. Classify it with
        # zero padding, but only genuine captured samples enter the utterance.
        if not self.active and buffer.end > self.segmenter.position and not failure:
            remaining = buffer.read(self.segmenter.position)
            padded = np.pad(remaining, (0, max(0, WINDOW - len(remaining))))
            try:
                speech = self.vad_slot.call(lambda: self.vad.speech(padded), time.monotonic() + 1)
                if speech:
                    self.active = True
                    self.has_speech = True
                    self.start_sample = buffer.start
                    self.pipeline.ledger.speech(self.current.id)
            except Exception as exc:
                failure = str(exc)
        if self.current is not None:
            self.current.cancel()
            if self.active and self.has_speech or failure:
                start = max(buffer.start, self.start_sample if self.active else buffer.start)
                samples = buffer.read(start)
                self.pipeline.submit(self.current, CutRecording(samples, start=start, reason=self.reason, failure=failure),
                                     self.recorder.started + buffer.end / SAMPLE_RATE)
                self.assigned += len(samples)
            else:
                self.pipeline.ledger.complete(self.current.id, "dropped")
            self.current = None
        self.pipeline._save("session", lambda: self.pipeline.journal.append(self.id, "session-end",
            samples=buffer.end, assigned_samples=self.assigned,
            discarded_silence_samples=buffer.end - self.assigned, reason=self.reason, failure=failure))
        buffer.discard_before(buffer.end)

    def close(self):
        self.request_stop("daemon stopping")
        if self.thread is not None:
            self.thread.join(max(0, self.deadline - time.monotonic()) + 2 * self.cfg.pipeline.journal_seconds + 5)
