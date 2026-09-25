"""Continuous microphone owner; audio-clock cuts feed the ordinary pipeline.

Only one segmentation worker and one supervised VAD slot exist at a time.
Recorder callbacks wake the worker; recognition, disk and desktop I/O never
run on the audio callback or key listener. Off is irreversible for a session.
"""
import logging
import queue
import threading
import time
import uuid

import numpy as np

from . import focus
from .capture import Session
from .draft import DraftTarget, DraftUnsupported
from .notify import notify
from .recorder import AudioBuffer, CutRecording, RecordingError, SAMPLE_RATE
from .segment import Boundary, Segmenter, WINDOW
from .session_target import SessionTarget
from .target import ClipboardTarget, PinnedEditorTarget, NotifyPreview, Window, WtypeTarget, Outcome
from .spacing import owed

log = logging.getLogger("voicekey.persistent")
CAPTURE_SLACK = 4.0


class DetectionError(RuntimeError):
    """A supervised detector call failed, rather than segment bookkeeping."""


class PersistentSession:
    def __init__(self, cfg, pipeline, recorder, target, vad, vad_slot, streaming,
                 *, device, chord, watch_factory=None, prepare_models=None, allow_typing=False):
        self.prepare_models = prepare_models
        self.id = uuid.uuid4().hex
        self.cfg, self.pipeline, self.recorder = cfg, pipeline, recorder
        self.draft = cfg.persistent.draft
        self.mode_resolved = threading.Event()
        if not self.draft:
            self.mode_resolved.set()
        self._starting_release = None
        self._cancel_during_binding = False
        self.draft_waiting = threading.Event()
        self._draft_decision = threading.Event()
        self._draft_action = ""
        self._decision_lock = threading.Lock()
        self._hotkey_lock = threading.Lock()
        self._hotkey_action = ""
        self._hotkey_thread = None
        # The compatibility escape hatch is local to one window and one session.
        self.policy = "pause" if allow_typing or self.draft else cfg.persistent.destination_policy
        self.allow_typing = allow_typing
        self.device, self.chord = device, chord
        self._bind = target if callable(target) else lambda: target
        self.target = self._new_target(ClipboardTarget(NotifyPreview("dictate"), Window(None, True), None))
        self.targets = [self.target]
        self.watch_factory, self.watcher = watch_factory, None
        self.focus_events = queue.Queue(maxsize=64)
        self._focus_lock = threading.Lock()
        self.focused = None
        self._focus_serial = 0
        self._editor_events = {}
        self.ready = threading.Event()
        self.vad, self.vad_slot, self.streaming = vad, vad_slot, streaming
        self.segmenter = Segmenter(cfg.persistent)
        self.reservation = cfg.persistent.max_utterance_seconds + CAPTURE_SLACK
        self.recorder.buffer = AudioBuffer(int(self.reservation * SAMPLE_RATE))
        self.wake = threading.Event()
        self.stopping = threading.Event()
        self.done = threading.Event()
        self._stop_lock = threading.Lock()
        self._stop_attention = False
        self.deadline = float("inf")
        self._drain_deadline = float("inf")
        self.reason = ""
        self.stop_instruction = "press a dictation key to stop"
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
        self._destination_issue = None
        self._last_focus_poll = float("-inf")
        self._focus_unknown = False
        self.typing_fallback = False
        self.tracking_notice = ""

    def _new_target(self, target):
        cls = DraftTarget if self.draft else SessionTarget
        return cls(self.id, target, self.pipeline.ledger, scoped=True, allow_typing=self.allow_typing)

    def _resolve_mode(self, draft):
        with self._stop_lock, self._decision_lock:
            if self.draft and not draft:
                old = self.target
                self.draft = False
                self.policy = "pause" if self.allow_typing else self.cfg.persistent.destination_policy
                self.target = self._new_target(old.target)
                # Transfer ownership without closing the preview or editor pin.
                self.targets[self.targets.index(old)] = self.target
                if old.cancelled.is_set() or self._cancel_during_binding:
                    self.target.failed.set()
                if self.stopping.is_set():
                    self._drain_deadline = self.deadline
                    self.pipeline.stop_session(self.id, self._drain_deadline)
            self.current.target = self.target.attempt(self.current.id)
            self.mode_resolved.set()
            release = self._starting_release
        if release is not None:
            self.starting_key_released(release)

    def starting_key_released(self, held):
        """Retain releases during binding, then apply the actual session mode."""
        with self._stop_lock:
            self._starting_release = held
            if not self.mode_resolved.is_set() or self.draft or self.stopping.is_set():
                return
        if held:
            self.request_stop("key released")
        else:
            self.stop_instruction = "press a dictation key to stop"
            if self.ready.is_set():
                self.status()

    def accept(self, reason="accepted by key"):
        if self.draft:
            with self._decision_lock:
                if not self._draft_action:
                    self._draft_action = "accept"
                    self._draft_decision.set()
            # Remaining speech is still transcribed; queued cleanup shares one
            # budget, after which chunks join the draft raw.
            if hasattr(self.target, "hurry"):
                self.target.hurry(self.cfg.polish.max_wait_seconds)
        self.request_stop(reason)

    def cancel_draft(self):
        with self._decision_lock:
            if not self.draft:
                raise ValueError("Only draft sessions can be cancelled as a whole")
            self.target.cancelled.set()
            self._draft_action = "cancel"
            self._draft_decision.set()
        self.request_stop("Draft cancelled")

    def request_draft_key(self, action):
        """Check current focus off the key thread; retain only the latest request."""
        with self._decision_lock:
            if action == "cancel" and not self.mode_resolved.is_set():
                # Block ordinary delivery before publishing a downgrade; the
                # focus worker may not be scheduled before audio processing.
                self._cancel_during_binding = True
        with self._hotkey_lock:
            self._hotkey_action = action
            if self._hotkey_thread is not None and self._hotkey_thread.is_alive():
                return
            def check():
                self.mode_resolved.wait()
                allowed = self.target.hotkey_focused() if self.draft else True
                with self._hotkey_lock:
                    requested, self._hotkey_action = self._hotkey_action, ""
                    try:
                        if self.done.is_set() or not requested:
                            return
                        if allowed is None:
                            notify("Draft focus could not be verified", "Retry in the original window, "
                                   "or use Accept draft / Discard draft in the widget.",
                                   channel="persistent", attention=True)
                        elif not allowed:
                            notify("Draft still pending", "Return to its original window to use the dictation/cancel keys, "
                                   "or use Accept draft / Discard draft in the widget.",
                                   channel="persistent", attention=True)
                        elif requested == "cancel":
                            if self.draft:
                                self.cancel_draft()
                            else:
                                self.target.failed.set()
                                self.request_stop("Cancelled during destination binding")
                        else:
                            self.accept("accepted by key")
                    finally:
                        self._hotkey_thread = None
            self._hotkey_thread = threading.Thread(target=check, name="draft-key-focus", daemon=True)
            self._hotkey_thread.start()

    def focus_changed(self, destination, *, force=False, expected=None):
        """Called by the event reader; no desktop I/O on this thread."""
        with self._focus_lock:
            if expected is not None and self.focused != expected:
                return
            if (not force and self.focused is not None and (destination.id, destination.app_id) == (self.focused.id, self.focused.app_id)) or self.stopping.is_set():
                return
            self.focused = destination
            self._focus_serial += 1
            # Immediately prevent an old generic delivery, even if the capture
            # worker is behind on audio or binding. Emacs retains its buffer pin.
            self.target.departed.set()
            if self.policy == "follow":
                try:
                    self.focus_events.put_nowait((self.recorder.buffer.end, destination, self._focus_serial))
                except queue.Full:
                    self.request_stop("too many focus changes; audio preserved", paused=True)
        if self.policy == "pause":
            self.request_stop("Window changed", paused=True, attention=False)
        self.wake.set()

    def editor_focus_changed(self, event):
        """Terminal-local focus evidence uses the same PCM boundary as Niri.

        Only discovery knows terminal/editor details. The session still rebinds
        through its original resolver, and retains old pins until work drains.
        """
        destination = self.focused
        if destination is None:
            return
        key = (event['pid'], event['server'])
        identity = getattr(self.target.target, 'focus_identity', None)
        if not event['focused'] and key != identity:
            return  # a background editor cannot pause the foreground destination
        with self._focus_lock:
            previous = self._editor_events.get(key)
            if previous is not None and event['sequence'] <= previous[0]:
                return
            self._editor_events[key] = (event['sequence'], event['focused'])
            if previous is None and event['focused'] and key == identity:
                return  # startup/gain report for the editor already bound
            if previous is not None and event['focused'] == previous[1]:
                return
        self.focus_changed(destination, force=True, expected=destination)

    def _drain_audio(self, end):
        """Classify through a focus boundary, including a partial VAD window."""
        while self.segmenter.position < end:
            start = self.segmenter.position
            count = min(3 * WINDOW, end - start)
            if not self._process(self.recorder.buffer.read(start, start + count)):
                return False
        return True

    def _move_destination(self, end, destination, serial):
        if not self._drain_audio(end):
            return False
        old = self.target
        old.leave()
        # An explicit cut owns exactly the prefix before this focus event.
        if not self._cut(Boundary("focus", self.start_sample, end, end)):
            return False
        self.segmenter.start = None
        self.segmenter.owned = self.segmenter.position = end
        self.segmenter.last_speech = end
        self.start_sample = self.live_at = end
        self.has_speech = False
        self.vad_slot.call(self.vad.reset, time.monotonic() + 1)
        # Never bind a historical event to whichever window happens to be
        # focused now. Rapid switches get recovery-only segments until caught up.
        candidate = ClipboardTarget(NotifyPreview("dictate"), Window(destination.id, True), destination.app_id)
        with self._focus_lock:
            current = serial == self._focus_serial
        if current and destination.id is not None:
            proposed = self._bind()
            before = proposed.before(getattr(proposed, "pin_timeout", 0.25))
            with self._focus_lock:
                stable = serial == self._focus_serial
            if (stable and proposed.window_id == destination.id
                    and (not isinstance(proposed, PinnedEditorTarget) or proposed.pin_valid)):
                candidate = proposed
                candidate.window.verify = True
                candidate.prefix = owed(before, self.pipeline.spacing.prefix(candidate.window_id))
            else:
                proposed.cancel()
                if isinstance(proposed, PinnedEditorTarget):
                    proposed.unpin()
        target = self._new_target(candidate)
        with self._focus_lock:
            if serial != self._focus_serial:
                target.departed.set()
            self.target = target
        self.targets.append(target)
        # _cut reserved this utterance before releasing the previous one.
        old.members.discard(self.current.id)
        self.current.target = target.attempt(self.current.id)
        self.pipeline._save("session", lambda: self.pipeline.journal.append(
            self.id, "destination-changed", sample=end, window=destination.id,
            app_id=destination.app_id, target=candidate.describe()))
        log.info("persistent destination: %s", candidate.describe())
        self._destination_issue = None
        if current and not target.departed.is_set():
            self.status()
        return True

    def _check_destination(self):
        """Debounce field/failure observations across independent focus and IME events.

        Delivery still checks its original field immediately. Only stopping
        capture is delayed; a stale observation must never stop a new target.
        Window subprocesses run at most once a second, separately from fields.
        """
        if self.stopping.is_set():
            return
        with self._focus_lock:
            target, serial = self.target, self._focus_serial
            if not self.focus_events.empty():
                self._destination_issue = None
                return
        if self.watcher is None and self.policy == "pause":
            now = time.monotonic()
            if target.target.window_id is not None and now - self._last_focus_poll >= 1:
                identity = focus.window_id(timeout=0.2)
                self._last_focus_poll = time.monotonic()
                if identity is not None and identity != target.target.window_id:
                    target.departed.set()
                    self.request_stop("Window changed", paused=True, attention=False)
                    return
                if identity is None and self._focus_unknown:
                    self.request_stop("Window tracking unavailable", paused=True)
                    return
                self._focus_unknown = identity is None
        issue = target.field_issue()
        if not issue and target.failed.is_set():
            issue = "Delivery unavailable"
        with self._focus_lock:
            if serial != self._focus_serial or not self.focus_events.empty():
                self._destination_issue = None
                return
            observation = (target, issue) if issue else None
            persistent = observation is not None and observation == self._destination_issue
            self._destination_issue = observation
            if persistent:
                self.typing_fallback = (issue == "No text field detected" and
                                       isinstance(target.target, WtypeTarget) and not self.allow_typing)
                self.request_stop(issue, paused=True)

    def _collect_targets(self):
        pending = {u.id for u in self.pipeline.ledger.snapshots() if u.session_id == self.id}
        for target in tuple(self.targets):
            if target is not self.target and not target.members.intersection(pending):
                target.close()
                self.targets.remove(target)

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
        # Capture onset before destination queries, pinning, journal I/O or VAD
        # setup. The worker can consume this bounded buffer once binding ends.
        try:
            self.recorder.start(lambda _frame: self.wake.set())
        except Exception:
            self.pipeline.ledger.complete(self.current.id, "failed")
            self.current = None
            self.target.close()
            raise
        self.thread = threading.Thread(target=self._run, name="persistent-capture", daemon=True)
        self.thread.start()
        return True

    def status(self):
        if self.stopping.is_set():
            return
        self._last_status = time.monotonic()
        destination = self.target.target.application_name
        detail = self.tracking_notice or self.stop_instruction
        summary = f"● Listening → {destination}" if destination else "● Listening · no destination"
        if self.draft:
            summary = f"● Draft → {destination}"
            detail = f"press the dictation key to accept; {self.cfg.persistent.draft_cancel_key} cancels"
        notify(summary, detail, ms=0, channel="persistent")

    def request_stop(self, reason="stopped by key", *, paused=False, attention=None):
        with self._stop_lock:
            if self.draft and self._draft_action in ("cancel", "preserve"):
                self._drain_deadline = min(self._drain_deadline,
                    time.monotonic() + self.cfg.pipeline.shutdown_seconds)
                self.pipeline.stop_session(self.id, self._drain_deadline)
            if self.stopping.is_set():
                return
            self.reason, self.paused = reason, paused
            self._stop_attention = paused if attention is None else attention
            self.deadline = time.monotonic() + self.cfg.pipeline.shutdown_seconds
            if not self.draft:
                self._drain_deadline = self.deadline
            # Draft preparation has bounded per-stage operations but must not
            # lose a healthy backlog merely because recording has stopped.
            self.pipeline.stop_session(self.id, self._drain_deadline)
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
            elif self.ready.is_set() and time.monotonic() - self._last_status >= 30:
                self.status()
        # Draft rendering uses editor RPC. The capture worker renders during
        # speech, draining, and before review; never bind/render on the key thread.
        if self.mode_resolved.is_set() and not self.draft:
            self.target.render()

    def _begin(self, event, speech):
        self.active = True
        self.has_speech = speech and event.end > event.start
        self.start_sample = event.start
        self.live_at = event.start
        self.pipeline.ledger.speech(self.current.id)
        self.pipeline.settled()

    def _attach_live(self):
        if (self.streaming is not None and self.current.decoder is None
                and (self.last_live is None or not self.last_live.stuck)):
            self.current.attach(self.streaming.session)
            self.last_live = self.current

    def _live(self, end):
        if self.active and self.current is not None and end > self.live_at:
            self._attach_live()
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
        samples = (self.recorder.buffer.read(self.start_sample, event.end)
                   if self.active and self.has_speech else np.zeros(0, dtype=np.float32))
        old.cancel()  # suppress results from a decoder which has seen the suffix
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

    def _classify(self, samples):
        # The native detector is supervised in batches (~100 ms of audio).
        def classify():
            return [self.vad.speech(np.pad(samples[i:i + WINDOW],
                        (0, max(0, WINDOW - len(samples[i:i + WINDOW])))))
                    for i in range(0, len(samples), WINDOW)]
        return self.vad_slot.call(classify, time.monotonic() + 1.0)

    def _process(self, samples):
        try:
            labels = self._classify(samples)
        except Exception as exc:
            raise DetectionError(f"speech detection failed: {exc}") from exc
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
        detector_failed = False
        detector_ready = False
        bound = False
        recovery_path = None
        phase = "destination binding"
        try:
            self.target.target = self._bind()
            bound = True  # A refused destination still owns its captured audio for recovery.
            # Always attach the bound destination, including on early refusal.
            self.current.target = self.target.attempt(self.current.id)
            if self.draft and (self.allow_typing
                               or not isinstance(self.target.target, PinnedEditorTarget)
                               or self.target.target.window_id is None):
                self._resolve_mode(False)
            if not isinstance(self.target.target, PinnedEditorTarget):
                self.target.target.window.verify = True
            before = self.target.target.before(getattr(self.target.target, "pin_timeout", 0.25))
            if isinstance(self.target.target, PinnedEditorTarget) and not self.target.target.pin_valid:
                # A late acknowledgement cannot authorize a session already refused.
                self.target.failed.set()
                self.request_stop(self.target.target.pin_reason
                                  or f"{self.target.target.application_name} did not acknowledge the session buffer", paused=True)
                return
            if issue := self.target.field_issue():
                # Capture already started. Preserve opening speech for recovery,
                # but the same guard in delivery prevents it becoming keystrokes.
                self.typing_fallback = isinstance(self.target.target, WtypeTarget) and not self.allow_typing
                self.request_stop(issue, paused=True)
                return
            self.target.target.prefix = owed(before,
                self.pipeline.spacing.prefix(self.target.target.window_id))
            if self.draft:
                try:
                    self.target.initialize()
                except DraftUnsupported:
                    self._resolve_mode(False)
            if not self.mode_resolved.is_set():
                self._resolve_mode(self.draft)
            self.focused = focus.Focus(self.target.target.window_id, self.target.target.app_id,
                                       self.target.target.window.pid)
            if (self.policy == "pause" and self.target.target.window_id is None
                    and isinstance(self.target.target, PinnedEditorTarget)):
                self.tracking_notice = "Window tracking unavailable; dictating to the original buffer"
            if self.watch_factory is not None and self.policy != "pin":
                self.watcher = self.watch_factory(self.focus_changed,
                    lambda reason: self.request_stop("focus tracking lost: " + reason, paused=True))
            self.pipeline._save("session", lambda: self.pipeline.journal.append(
                self.id, "session-start", target=self.target.target.kind,
                draft=self.draft,
                destination_policy=self.policy, allow_typing=self.allow_typing,
                pause_seconds=self.cfg.persistent.pause_seconds,
                silence_seconds=self.cfg.persistent.silence_seconds))
            phase = "speech detector initialization"
            if self.prepare_models is not None:
                self.prepare_models(self)
            self.vad_slot.call(self.vad.reset, time.monotonic() + 1.0)
            detector_ready = True
            self.ready.set()
            self.status()
            phase = "speech segmentation"
            poll_seconds = 0.2
            last_poll = 0.0
            last_audio = time.monotonic()
            while True:
                self.wake.wait(0.05)
                self.wake.clear()
                while not self.focus_events.empty():
                    end, destination, serial = self.focus_events.get_nowait()
                    if not self._move_destination(end, destination, serial):
                        break
                self._collect_targets()
                if time.monotonic() - last_poll >= poll_seconds:
                    self._check_destination()
                    last_poll = time.monotonic()
                with self._focus_lock:
                    end = self.recorder.buffer.end
                    if not self.focus_events.empty():
                        continue
                    available = end - self.segmenter.position
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
                if self.pipeline._storage_failed:
                    self.request_stop("Recovery unavailable", paused=True)
                if self.stopping.is_set() and time.monotonic() >= self.deadline:
                    break
        except Exception as exc:
            detector_failed = isinstance(exc, DetectionError) or phase == "speech detector initialization"
            failure = str(exc) if isinstance(exc, DetectionError) else f"{phase} failed: {exc}"
            log.exception("continuous capture failed")
        finally:
            self.mode_resolved.set()  # release queued hotkeys even on binding failure
            try:
                if self.watcher is not None:
                    self.watcher.close()
                try:
                    if bound:
                        self.recorder.stop()
                    else:
                        self.recorder.abort()
                except RecordingError as exc:
                    failure = failure or str(exc)
                except Exception as exc:
                    failure = failure or f"capture cleanup failed: {exc}"
                self.request_stop(failure or "audio source ended", paused=bool(failure),
                                  attention=self.device != "replay")
                if self.draft and self.device == "replay":
                    self.accept("replay ended")
                if failure:
                    # A stop requested earlier must not hide a microphone error
                    # discovered when collecting its exit status and stderr.
                    with self._stop_lock:
                        self.reason, self.paused = failure, True
                if bound:
                    # Binding can reject before models load or the VAD resets.
                    # Preserve that unclassified audio without calling the VAD.
                    self._flush(failure, detector_failed=detector_failed or not detector_ready)
                else:
                    self.current.cancel()
                    self.pipeline.ledger.complete(self.current.id, "dropped")
                    self.current = None
                    buffer = self.recorder.buffer
                    self.pipeline._save("session", lambda: self.pipeline.journal.append(
                        self.id, "session-rejected", reason=self.reason, failure=failure,
                        discarded_startup_samples=buffer.end))
                    buffer.discard_before(buffer.end)
                while self._pending() and time.monotonic() < self._drain_deadline:
                    self.target.render()
                    time.sleep(0.02)
                if self._pending():
                    self.pipeline.expire_session(self.id)
                if self.draft:
                    self._finish_draft(failure)
                recovery_path = self.pipeline._save("session", lambda: self.pipeline.journal.close_session(self.id))
            except Exception as exc:
                log.exception("persistent shutdown failed")
                self.pipeline._storage_failed = True
                failure = failure or f"dictation cleanup failed: {exc}"
            finally:
                for target in self.targets:
                    target.close()
                self.pipeline.forget_session(self.id)
                self.pipeline.settled()
                self.done.set()
                detail = self.reason + "; press the key to start a new session"
                if failure and failure not in detail:
                    detail += f"; {failure}"
                if recovery_path:
                    detail += f"; recovery: {recovery_path}"
                notify("■ Dictation stopped" if self.paused else "■ Persistent dictation off",
                       detail, channel="persistent", attention=self._stop_attention,
                       error=bool(failure or recovery_path))

    def _finish_draft(self, failure):
        target = self.target
        issue = failure or target.field_issue() or ("Draft processing failed" if target.failed.is_set() else "")
        outcome, reason = None, ""
        action = self._draft_action
        while not issue and target.text and not target.cancelled.is_set():
            target.render()
            # A prepared draft needs its editor pin and text, but no model pages.
            self.streaming = self.vad = None
            # Automatic stops pause here with the microphone off. A decision
            # made while recording/processing is already set and skips waiting.
            self.draft_waiting.set()
            if not self._draft_decision.is_set():
                notify("Draft ready", "Press the dictation key to accept; "
                       f"{self.cfg.persistent.draft_cancel_key} cancels", channel="persistent", attention=True)
            while not self._draft_decision.wait(.2):
                target.render()
            with self._decision_lock:
                action = self._draft_action
                if action == "accept":
                    self._draft_action = ""
                    self._draft_decision.clear()
            self.draft_waiting.clear()
            if action != "accept" or target.cancelled.is_set():
                break
            landing = target.commit(self.pipeline)
            if landing.outcome != Outcome.REFUSED:
                outcome, reason = landing.outcome, landing.reason
                break
            notify("Draft not inserted", landing.reason + "; fix the destination and accept again, "
                   "or discard the draft", channel="persistent", attention=True)
        if outcome is None:
            if target.cancelled.is_set():
                outcome, reason = Outcome.DROPPED, "Draft discarded"
                if target.text:
                    reason += ("; restore it with python -m voicekey --copy-last "
                               "(also in ~/.local/state/voicekey/last-recovery.txt)")
                    self._stop_attention = True
            elif not issue and not target.text:
                outcome, reason = Outcome.DROPPED, "No speech"
            else:
                outcome, reason = Outcome.SAVED, issue or "Draft preserved without insertion"
        self.reason = reason or ("Draft accepted" if outcome == Outcome.CONFIRMED else "Draft finished")
        if target.warning and outcome != Outcome.DROPPED:
            self.reason += "; " + target.warning
            self._stop_attention = True
        cancelled = target.cancelled.is_set()
        self.pipeline._save('session', lambda: self.pipeline.journal.append(self.id, 'outcome',
            draft=True, outcome=str(outcome), reason=self.reason, cancelled=cancelled))

    def _pending(self):
        return any(u.session_id == self.id for u in self.pipeline.ledger.snapshots())

    def _flush(self, failure, *, detector_failed=False):
        buffer = self.recorder.buffer
        unclassified = detector_failed and buffer.end > self.segmenter.position
        # EOF may contain a final partial detector window. Classify it with
        # zero padding, but only genuine captured samples enter the utterance.
        # A microphone failure does not invalidate a healthy detector.
        if not self.active and buffer.end > self.segmenter.position and not detector_failed:
            remaining = buffer.read(self.segmenter.position)
            try:
                speech = any(self._classify(remaining))
                if speech:
                    self.active = True
                    self.has_speech = True
                    self.start_sample = buffer.start
                    self.pipeline.ledger.speech(self.current.id)
            except Exception as exc:
                detail = f"final speech detection failed: {exc}"
                failure = f"{failure}; {detail}" if failure else detail
                unclassified = True
        if failure:
            with self._stop_lock:
                self.reason, self.paused = failure, True
        if self.current is not None:
            self.current.cancel()
            # Preserve unclassified audio on detector failure, but
            # never turn already classified idle silence into an utterance.
            if (self.active and self.has_speech or unclassified) and buffer.end > buffer.start:
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
        if self.draft:
            with self._decision_lock:
                if self._draft_action != "cancel":
                    self._draft_action = "preserve"
                self._draft_decision.set()
        self.request_stop("daemon stopping")
        thread = self._hotkey_thread
        if thread is not None:
            thread.join(.5)
        if self.thread is not None:
            self.thread.join(max(0, self._drain_deadline - time.monotonic()) + 2 * self.cfg.pipeline.journal_seconds + 5)
