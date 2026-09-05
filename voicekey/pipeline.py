"""Bounded hold-to-talk pipeline, independent of keys and capture sources.

The ledger owns admission and terminal state. FIFO stage queues carry each
utterance once, including utterances which skip polish, so final delivery stays
in speech order. Supervised execution slots give callers control of deadlines
without spawning replacement threads for a hung model or application.
"""
from __future__ import annotations

import logging
import queue
import threading
import time
from dataclasses import dataclass, replace

import numpy as np

from . import agent, inject, polish, recovery
from .ledger import Ledger, Stage
from .notify import notify
from .recorder import RecordingError
from .spacing import Spacing
from .target import Landing, Outcome
from .work import Slot, WorkBusy, WorkTimeout

log = logging.getLogger("voicekey.pipeline")
MAX_TEXT_BYTES = 100000
# Room for live/raw/final/intent in JSONL and readable form, even if JSON
# escapes every byte. Reserved separately from audio before capture starts.
TEXT_RESERVE = 4 * 1024 * 1024


@dataclass(frozen=True)
class Job:
    id: str
    action: str
    target: object
    samples: np.ndarray | None
    finished_at: float
    deadline: float
    live: str = ""
    raw: str = ""
    final: str = ""
    polish_deadline: float = 0.0
    failure: str = ""


class Pipeline:
    def __init__(self, cfg, *, backend, polisher, settled=lambda: None, journal=None, send_agent=None):
        self.cfg = cfg
        self.backend = backend
        self.polisher = polisher
        self.settled = settled
        limits = cfg.pipeline
        self.ledger = Ledger(limits.max_pending, limits.max_audio_seconds)
        self.journal = journal or recovery.Journal(megabytes=limits.recovery_megabytes,
                                                  history_days=limits.history_days)
        self._disk_reservation = int(cfg.max_seconds * 32000) + TEXT_RESERVE
        self.spacing = Spacing()
        self.captures, self.jobs, self.polishing, self.deliveries, self.agents = (
            queue.Queue(maxsize=limits.max_pending) for _ in range(5)
        )
        self._closed = threading.Event()
        self._accepting = True
        self._storage_failed = False
        self._stop_at = float("inf")
        self._threads = []
        self._items = {}
        self._recorders = {}
        self._stalled_audio = 0.0
        self._stalled_corpus_audio = 0.0
        self._items_lock = threading.Lock()
        self._slots = {name: Slot(name) for name in ("transcribe", "polish", "deliver", "agent", "corpus")}
        self._journal_slots = {name: Slot("journal-" + name) for name in
                               ("startup", "capture", "transcribe", "polish", "deliver", "agent", "close")}
        self._send_agent = send_agent or (lambda text: agent.send_prompt(
            cfg.agent, text, cancelled=self._closed,
            deadline=min(self._stop_at, time.monotonic() + cfg.agent.ready_timeout)))

    def start(self):
        if self._threads:
            return
        try:
            self._journal_slots["startup"].call(lambda: self.journal.prepare(self._disk_reservation),
                                                time.monotonic() + self.cfg.pipeline.journal_seconds)
        except Exception as exc:
            self._storage_error(exc)
        for name, source, handler, expected in (("finalize", self.captures, self._finalize, Stage.FINALIZING),
                                      ("transcription", self.jobs, self._transcribe, Stage.TRANSCRIBING),
                                      ("polishing", self.polishing, self._polish, Stage.POLISHING),
                                      ("delivery", self.deliveries, self._deliver, Stage.READY),
                                      ("agent-dispatch", self.agents, self._agent, Stage.READY)):
            thread = threading.Thread(target=self._consume, args=(source, handler, expected), name=name, daemon=True)
            thread.start()
            self._threads.append(thread)

    def admit(self):
        if not self._accepting or self._storage_failed or self._slots["transcribe"].busy and not self.ledger.gated:
            return None
        retained = self._stalled_audio if self._slots["transcribe"].busy else 0.0
        retained += self._stalled_corpus_audio if self._slots["corpus"].busy else 0.0
        return self.ledger.admit(self.cfg.max_seconds, retained_audio=retained,
                                 storage_bytes=self._disk_reservation, storage_available=self.journal.available)

    def submit(self, session, recorder, finished_at):
        if self.ledger.transition(session.id, Stage.CAPTURING, Stage.FINALIZING):
            with self._items_lock:
                self._items[session.id] = session
                self._recorders[session.id] = recorder
            self.captures.put_nowait((session, recorder, finished_at))

    def _consume(self, source, handler, expected):
        while not self._closed.is_set():
            try:
                item = source.get(timeout=0.05)
            except queue.Empty:
                continue
            try:
                identity = item[0].id if isinstance(item, tuple) else item.id
                current = self.ledger.get(identity)
                if not self._closed.is_set() and current is not None and current.stage == expected:
                    handler(item)
            except Exception as exc:
                log.exception("pipeline stage failed")
                target = item[0].target if isinstance(item, tuple) else item.target
                target.clear()
                self.ledger.complete(identity, "failed")
                with self._items_lock:
                    self._items.pop(identity, None)
                notify("voicekey: pipeline failed", f"{exc}; recovery is in {self.journal.directory}", error=True)
            finally:
                source.task_done()
                self.settled()
                item = None  # do not retain the previous audio while get() waits

    def _storage_error(self, exc):
        self._storage_failed = True
        notify("voicekey: recovery unavailable", f"{exc}; new recordings disabled until restart", error=True)

    def _save(self, lane, function):
        try:
            return self._journal_slots[lane].call(function, time.monotonic() + self.cfg.pipeline.journal_seconds)
        except Exception as exc:
            self._storage_error(exc)
            raise

    def _remember(self, job):
        with self._items_lock:
            self._items[job.id] = job

    def _complete(self, job, outcome, reason="", lane="deliver"):
        if self.ledger.get(job.id) is None:
            return
        try:
            self._save(lane, lambda: self.journal.append(job.id, "outcome", outcome=str(outcome), reason=reason,
                                                        recovery_needed=bool(job.failure)))
            try:
                self._save(lane, lambda: self.journal.prepare(self._disk_reservation))
            except Exception:
                pass  # outcome is saved; the storage error already disabled new admission
        finally:
            job.target.clear()
            self.ledger.complete(job.id, str(outcome))
            log.info("%s outcome=%s", job.id, outcome)
            with self._items_lock:
                self._items.pop(job.id, None)
            self.settled()

    def _finalize(self, item):
        session, recorder, finished_at = item
        failure = ""
        try:
            samples, duration = recorder.stop()
        except RecordingError as exc:
            samples, duration, failure = exc.samples, exc.duration, str(exc)
        # A deliberate tap is the only discarded audio path.
        if duration < self.cfg.min_seconds and not failure:
            session.cancel()
            session.target.clear()
            self.ledger.complete(session.id, "dropped")
            with self._items_lock:
                self._items.pop(session.id, None)
                self._recorders.pop(session.id, None)
            return
        self._save("capture", lambda: self.journal.capture(session.id, samples, session.text))
        with self._items_lock:
            self._recorders.pop(session.id, None)
        session.finish(timeout=min(1.0, max(0, self._stop_at - time.monotonic())))
        deadline = finished_at + (self.cfg.dictation.max_delay_seconds if session.action == "dictate"
                                  else self.cfg.pipeline.transcription_seconds)
        job = Job(session.id, session.action, session.target, samples, finished_at, deadline,
                  live=session.text, failure=failure)
        if not self.ledger.transition(job.id, Stage.FINALIZING, Stage.TRANSCRIBING,
                                      audio_seconds=len(samples) / 16000, live=session.text, storage_bytes=TEXT_RESERVE):
            return
        self._remember(job)
        self.jobs.put_nowait(job)

    def _transcribe(self, job):
        backend = self.backend()
        failure = job.failure
        try:
            if backend is None:
                raise RuntimeError("transcription backend unavailable")
            deadline = min(job.deadline, self._stop_at,
                           time.monotonic() + self.cfg.pipeline.transcription_seconds)
            raw = self._slots["transcribe"].call(lambda: backend.transcribe(job.samples), deadline)
            if not isinstance(raw, str) or len(raw.encode()) > MAX_TEXT_BYTES:
                raise ValueError("invalid or oversized transcript")
        except Exception as exc:
            if isinstance(exc, WorkTimeout) and self._slots["transcribe"].busy:
                self._stalled_audio = len(job.samples) / 16000
            raw = job.live
            failure = f"transcription failed ({exc}); using live text" if raw else f"transcription failed ({exc})"
        if self.ledger.get(job.id) is None:
            return
        transcribed_at = time.monotonic()
        log.info("%s transcribed (%d chars)", job.id, len(raw))
        self._save("transcribe", lambda: self.journal.append(job.id, "transcribed", raw=raw,
                                                             live=job.live, failure=failure))
        if not raw:
            self._complete(job, Outcome.SAVED if failure else Outcome.DROPPED, failure, "transcribe")
            if failure:
                notify("voicekey: recording saved", f"{failure}; audio in {self.journal.directory}", error=True)
            return
        job = replace(job, raw=raw, failure=failure,
                      polish_deadline=min(transcribed_at + self.cfg.polish.max_wait_seconds, job.deadline - 0.15))
        if not self.cfg.recordings_dir:
            job = replace(job, samples=None)
        if not self.ledger.transition(job.id, Stage.TRANSCRIBING, Stage.POLISHING, raw=raw,
                                      audio_seconds=0 if job.samples is None else len(job.samples) / 16000):
            return
        self._remember(job)
        # Every dictation traverses this queue, including short ones, preserving order.
        self.polishing.put_nowait(job)

    def _polish(self, job):
        final = job.raw
        polisher = self.polisher()
        deadline = min(job.polish_deadline, self._stop_at)
        eligible = (job.action == "dictate" and polisher is not None
                    and len(polish.words(job.raw)) >= self.cfg.polish.min_words)
        if eligible and time.monotonic() < deadline:
            job.target.show(job.raw)
            try:
                cleaned = self._slots["polish"].call(
                    lambda: polisher.polish(job.raw, max(0, deadline - time.monotonic())), deadline)
                if isinstance(cleaned, str) and cleaned.strip() and len(cleaned.encode()) <= MAX_TEXT_BYTES:
                    final = cleaned
            except Exception as exc:
                log.warning("polish skipped: %s", exc)
        if self.ledger.get(job.id) is None:
            return
        self._save("polish", lambda: self.journal.append(job.id, "final", raw=job.raw, final=final))
        if self.cfg.recordings_dir and job.samples is not None:
            try:
                self._slots["corpus"].call(lambda: recovery.keep(self.cfg.recordings_dir, job.samples,
                                                                job.live, job.raw, final),
                    min(job.deadline - 0.1, time.monotonic() + self.cfg.pipeline.journal_seconds))
            except Exception as exc:
                if isinstance(exc, WorkTimeout) and self._slots["corpus"].busy:
                    self._stalled_corpus_audio = len(job.samples) / 16000
                log.warning("optional recordings corpus unavailable: %s", exc)
        job = replace(job, final=final, samples=None)
        if not self.ledger.transition(job.id, Stage.POLISHING, Stage.READY, final=final,
                                      audio_seconds=0, gated=job.action == "dictate"):
            return
        self._remember(job)
        if job.action == "agent" and sum(not u.gated for u in self.ledger.snapshots()) > self.cfg.pipeline.max_pending:
            self._complete(job, Outcome.SAVED, "agent backlog full", "polish")
            notify("voicekey: agent busy", f"prompt saved to {self.journal.path(job.id, '.txt')}", error=True)
            return
        (self.agents if job.action == "agent" else self.deliveries).put_nowait(job)

    def _deliver(self, job):
        attempt = self.ledger.reserve(job.id)
        if attempt is None:
            return
        self._save("deliver", lambda: self.journal.append(job.id, "delivery-attempt", attempt=attempt,
                                                          final=job.final, deadline=job.deadline))
        job.target.permit = str(self.journal.path(job.id, ".permit"))
        deadline = min(job.deadline, self._stop_at)
        mark = self.spacing.mark()
        if time.monotonic() >= deadline:
            landing = Landing(reason="dictation expired before delivery")
        else:
            prefix = self.spacing.prefix(job.target.window_id)
            try:
                landing = self._slots["deliver"].call(
                    lambda: job.target.land(job.final, deadline, operation_id=attempt, prefix=prefix), deadline)
            except WorkBusy:
                landing = Landing(reason="a previous delivery has not returned")
            except Exception as exc:
                job.target.cancel()
                landing = Landing(Outcome.UNKNOWN, f"delivery may have started: {exc}")
        if self.ledger.get(job.id) is None:
            return
        if landing.landed:
            self.spacing.inserted(job.target.window_id, job.final, mark)
            self._complete(job, landing.outcome)
            if job.failure:
                notify("voicekey: recording warning", f"{job.failure}; audio saved in {self.journal.directory}", ms=10000)
            else:
                notify("✓ Inserted" if landing.outcome == Outcome.CONFIRMED else "✓ Sent to field", channel="dictate")
        elif landing.uncertain:
            self._save("deliver", lambda: self.journal.recover(job.id, job.final))
            self._complete(job, Outcome.UNKNOWN, landing.reason)
            notify("voicekey: delivery uncertain", f"{landing.reason}; inspect {self.journal.path(job.id, '.txt')}", error=True)
        else:
            # The final text and attempt were already saved before touching the clipboard.
            job.target.clear()
            outcome = Outcome.SAVED
            self._save("deliver", lambda: self.journal.recover(job.id, job.final))
            if not self._closed.is_set():
                try:
                    inject.copy(job.final)
                    outcome = Outcome.COPIED
                except Exception as exc:
                    log.warning("clipboard failed: %s", exc)
            self._complete(job, outcome, landing.reason)
            notify("📋 Copied" if outcome == Outcome.COPIED else "voicekey: transcript saved",
                   f"{landing.reason}; {self.journal.path(job.id, '.txt')}", channel="dictate", ms=10000)

    def _agent(self, job):
        attempt = self.ledger.reserve(job.id)
        if attempt is None:
            return
        self._save("agent", lambda: self.journal.append(job.id, "agent-attempt", attempt=attempt, raw=job.raw))
        try:
            self._slots["agent"].call(lambda: self._send_agent(job.raw),
                                       min(self._stop_at, time.monotonic() + self.cfg.agent.ready_timeout))
        except Exception as exc:
            self._complete(job, Outcome.UNKNOWN, str(exc), "agent")
            notify("voicekey: agent delivery uncertain", str(self.journal.path(job.id, '.txt')), error=True)
        else:
            self._complete(job, Outcome.SUBMITTED, lane="agent")
            notify("✓ Sent to agent", channel="agent")

    def close(self, timeout=None):
        if self._closed.is_set():
            return
        self._accepting = False
        self._stop_at = time.monotonic() + (self.cfg.pipeline.shutdown_seconds if timeout is None else timeout)
        while self.ledger.busy and time.monotonic() < self._stop_at:
            time.sleep(0.02)
        self._closed.set()
        with self._items_lock:
            items = list(self._items.values())
            recorders = dict(self._recorders)
        for item in items:
            if not isinstance(item, Job):
                item.cancel()
            if hasattr(item.target, "cancel"):
                item.target.cancel()
            else:
                item.target.clear()
        # Stop every remaining source before considering journal/drain cleanup.
        for recorder in recorders.values():
            proc = getattr(recorder, "proc", None)
            if proc is not None and proc.poll() is None:
                try:
                    proc.kill()
                except ProcessLookupError:
                    pass
        def preserve_remaining():
            for item in items:
                self.journal.revoke(item.id)
                recorder = recorders.get(item.id)
                if recorder is not None:
                    frames = tuple(getattr(recorder, "frames", ()))
                    samples = np.concatenate(frames) if frames else getattr(recorder, "samples", np.zeros(0, dtype=np.float32))
                    self.journal.capture(item.id, samples, getattr(item, "text", ""))
                current = self.ledger.get(item.id)
                self.journal.append(item.id, "shutdown", disposition="unknown" if current and
                                    current.stage == Stage.DELIVERING else "saved")
        try:
            self._save("close", preserve_remaining)
        except Exception:
            pass
        for item in items:
            # Known audio/text was journalled before entering a fallible stage.
            # A pending delivery intent remains unresolved, never silently retried.
            current = self.ledger.get(item.id)
            self.ledger.complete(item.id, "unknown" if current and current.stage == Stage.DELIVERING else "saved")
        with self._items_lock:
            self._items.clear()
            self._recorders.clear()
        self.settled()
        for thread in self._threads:
            thread.join(0.1)
