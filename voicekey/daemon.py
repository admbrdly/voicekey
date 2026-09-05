"""Keyboard controller and resource ownership for hold/toggle dictation.

Capture decoding, utterance state, pipeline stages and target transactions
live in their own modules. A release transfers ownership to the pipeline;
the ledger retains its gate token throughout that transfer.
"""
from __future__ import annotations

import glob
import logging
import os
import sys
import time

from evdev import ecodes

from . import polish as polish_mod
from . import target as target_mod
from .backends import BackendUnavailable, create_backend, create_streaming
from .capture import Session
from .config import Config, ConfigError, key_chord_names
from .gate import Gate
from .ime import ImeUnavailable, InputMethod
from .listener import KeyboardListener
from .notify import notify
from .pipeline import Pipeline
from .persistent import PersistentSession
from .segment import SpeechDetector
from .work import Slot
from .recorder import Recorder
from .spacing import owed
from .target import LABEL, ClipboardTarget, NotifyPreview, Window

log = logging.getLogger("voicekey.daemon")
HOLD, TOGGLE = "hold", "toggle"

def _keycode(name: str) -> int:
    code = ecodes.ecodes.get(name)
    if not isinstance(code, int):
        raise ConfigError(f"unknown key name {name!r} (want evdev names like 'KEY_F9')")
    return code


def _key_chord(value: str) -> frozenset[int]:
    return frozenset(_keycode(name) for name in key_chord_names(value))


def fix_environment() -> None:
    """Fill session variables commonly absent from a systemd user service."""
    if not os.environ.get("WAYLAND_DISPLAY"):
        runtime = os.environ.get("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}")
        sockets = sorted(path for path in glob.glob(os.path.join(runtime, "wayland-*"))
                         if not path.endswith(".lock"))
        if sockets:
            os.environ["WAYLAND_DISPLAY"] = os.path.basename(sockets[0])
            log.info("WAYLAND_DISPLAY not set; using %s", os.environ["WAYLAND_DISPLAY"])
    local_bin = os.path.expanduser("~/.local/bin")
    if local_bin not in os.environ.get("PATH", "").split(":"):
        os.environ["PATH"] = f"{local_bin}:{os.environ.get('PATH', '')}"


class Daemon:
    def __init__(self, cfg: Config, *, recorder_factory=Recorder, journal=None):
        self.cfg = cfg
        self.actions = {_key_chord(cfg.dictate_key): ("dictate", HOLD),
                        _key_chord(cfg.agent_key): ("agent", HOLD)}
        for chord, action in ((cfg.dictate_toggle_key, "dictate"), (cfg.agent_toggle_key, "agent")):
            if chord:
                self.actions[_key_chord(chord)] = (action, TOGGLE)
        if cfg.persistent.key:
            self.actions[_key_chord(cfg.persistent.key)] = ("persistent", TOGGLE)
        self.recorder_factory = recorder_factory
        self.recorder = recorder_factory()
        self.session = None
        self.persistent = None
        self.vad = None
        self._vad_slot = Slot("speech-detector")
        self.pressed = {}
        self.backend = self.streaming = self.ime = self.polisher = self.polish_server = None
        self.backend_error = None
        self.gate = Gate()
        self.pipeline = Pipeline(cfg, backend=lambda: self.backend, polisher=lambda: self.polisher,
                                 settled=self._settle_gate, journal=journal)
        self._live_session = None
        self._stopping = False
        self._closed = False

    def load(self) -> None:
        """Load both models and register as the input method; each failure
        is reported and degrades the daemon rather than stopping it."""
        try:
            self.backend = create_backend(self.cfg.backend, self.cfg.language)
        except BackendUnavailable as exc:
            self.backend_error = str(exc)
        except Exception as exc:
            self.backend_error = f"{type(exc).__name__}: {exc}"
            log.exception("transcription backend failed to load")
        if self.backend_error:
            notify("voicekey: transcription unavailable", self.backend_error, error=True)
        if self.cfg.persistent.key:
            try:
                self.vad = SpeechDetector(self.cfg.persistent.vad_model)
            except Exception as exc:
                notify("voicekey: persistent mode unavailable", str(exc), error=True)
        try:
            self.streaming = create_streaming(self.cfg.streaming)
        except BackendUnavailable as exc:
            notify("voicekey: live preview unavailable", str(exc), error=True)
        except Exception as exc:
            log.exception("streaming backend failed to load")
            notify("voicekey: live preview unavailable", f"{type(exc).__name__}: {exc}", error=True)
        if self.cfg.polish.backend != "none":
            try:
                self.polish_server = polish_mod.start_server(self.cfg.polish)
                self.polisher = polish_mod.create_polisher(self.cfg.polish, self.polish_server)
            except polish_mod.PolishError as exc:
                notify("voicekey: polish unavailable", f"{exc}; transcripts land unpolished", error=True)
            except Exception as exc:
                log.exception("polish failed to start")
                notify("voicekey: polish unavailable", f"{type(exc).__name__}: {exc}", error=True)
        if self.cfg.dictation.ime:
            try:
                self.ime = InputMethod()
            except ImeUnavailable as exc:
                log.info("no in-field preview: %s", exc)
            except Exception:
                log.exception("input method failed to start; previews use notifications")

    def start_workers(self):
        self.pipeline.start()

    def close(self):
        if self._closed:
            return
        self._closed = self._stopping = True
        try:
            if self.persistent is not None:
                self.persistent.close()
            if self.session is not None:
                self._finish()
            self.pipeline.close()
        finally:
            try:
                if self.ime is not None:
                    self.ime.close()
            finally:
                self.gate.close()
                if self.polish_server is not None:
                    self.polish_server.stop()

    def run(self):
        fix_environment()
        self.gate.open()
        self.load()
        self.start_workers()
        listener = KeyboardListener(
            keycodes=set().union(*self.actions), on_key=self._on_key,
            on_device_lost=self._on_device_lost, on_tick=self._on_tick,
            on_no_access=lambda message: notify("voicekey: no keyboard access", message, error=True),
            on_activity=self._on_activity,
        )
        log.info("listening: %s", ", ".join(self.bindings()))
        try:
            listener.run()
        finally:
            listener.close()

    def bindings(self):
        return [f"{key}={action}({behavior})" for key, action, behavior in (
            (self.cfg.dictate_key, "dictate", HOLD), (self.cfg.agent_key, "agent", HOLD),
            (self.cfg.dictate_toggle_key, "dictate", TOGGLE),
            (self.cfg.agent_toggle_key, "agent", TOGGLE),
            (self.cfg.persistent.key, "persistent", TOGGLE)) if key]

    def replay(self, path, action="dictate"):
        self.gate.open()
        self.recorder = Recorder([sys.executable, "-m", "voicekey.replay", path])
        if action == "persistent":
            self._start_persistent("replay", frozenset(), self.recorder)
        else:
            self._start("replay", frozenset(), HOLD, action, "replaying")
        while self.session is not None or self.persistent is not None:
            self._on_tick()
            time.sleep(0.02)
        while self.pipeline.ledger.busy:
            time.sleep(0.02)

    def _on_key(self, device, code, value):
        if self._stopping:
            return
        pressed = self.pressed.setdefault(device, set())
        session = self.session
        if value == 0:
            if (session is not None and session.behavior == HOLD
                    and session.device == device and code in session.chord):
                self._finish()
            pressed.discard(code)
            return
        pressed.add(code)
        matches = [(chord, action) for chord, action in self.actions.items()
                   if code in chord and chord <= pressed]
        if not matches:
            return
        longest = max(len(chord) for chord, _ in matches)
        matches = [item for item in matches if len(item[0]) == longest]
        if len(matches) != 1:
            log.warning("ambiguous dictation chord")
            return
        chord, (action, behavior) = matches[0]
        if self.persistent is not None:
            if self.persistent.done.is_set():
                self._live_session = self.persistent.last_live or self._live_session
                self.persistent = None
            else:
                if action == "persistent":
                    self.persistent.request_stop()
                return
        if session is not None:
            if behavior == TOGGLE and chord == session.chord and device == session.device:
                self._finish()
            return
        if action == "persistent":
            self._start_persistent(device, chord)
            return
        self._start(device, chord, behavior, action,
                    "press again to stop" if behavior == TOGGLE else "release to stop")

    def _start_persistent(self, device, chord, recorder=None):
        if self.vad is None or self.backend is None or self._vad_slot.busy:
            notify("voicekey: persistent mode unavailable", "speech models unavailable or a detector call is still running", error=True)
            return
        # A new session cannot share the previous gesture's decoder or preview.
        if self.pipeline.ledger.busy or self._live_session is not None and self._live_session.stuck:
            notify("voicekey: busy", "let pending dictation finish before starting persistent mode", error=True)
            return
        session = PersistentSession(self.cfg, self.pipeline, recorder or self.recorder_factory(),
            lambda: target_mod.bind(self.ime, self.cfg.dictation, False), self.vad, self._vad_slot,
            self.streaming, device=device, chord=chord)
        self.persistent = session
        try:
            if not session.start():
                self.persistent = None
                notify("voicekey: busy", "pending work or recovery storage is full", error=True)
        except Exception as exc:
            self.persistent = None
            self._settle_gate()
            notify("voicekey: persistent capture failed", str(exc), error=True)

    def _start(self, device, chord, behavior, action, instruction):
        identity = self.pipeline.admit()
        if identity is None:
            notify("voicekey: busy", "recording did not start; pending work or recovery storage is full", error=True)
            return
        session = Session(action, behavior, chord, device, identity=identity, on_text=self.pipeline.ledger.live)
        if action == "dictate":
            session.target = ClipboardTarget(NotifyPreview(action), Window(None, True), None)
        self.session = session
        self._settle_gate()
        try:
            self.recorder.max_samples = int(self.cfg.max_seconds * 16000)
            self.recorder.start(session.feed)
        except OSError as exc:
            self.session = None
            self.pipeline.ledger.complete(identity, "failed")
            self._settle_gate()
            notify("voicekey: recording failed", str(exc), error=True)
            return
        try:
            if action == "dictate":
                session.target = target_mod.bind(self.ime, self.cfg.dictation,
                                                  sum(u.gated for u in self.pipeline.ledger.snapshots()) > 1)
                session.target.prefix = owed(session.target.before(0.0),
                                             self.pipeline.spacing.prefix(session.target.window_id))
            if self.streaming is not None:
                if self._live_session is None or not self._live_session.stuck:
                    session.attach(self.streaming.session)
                    self._live_session = session
                else:
                    log.warning("previous live decoder still running; preview skipped")
        except Exception as exc:
            log.exception("preview setup failed; capture continues")
            session.cancel()
            notify("voicekey: preview unavailable", str(exc), error=True)
        notify(f"● Recording ({LABEL[action]})", instruction, ms=60000, channel=action)
        log.info("recording %s (%s)", identity, action)

    def _finish(self):
        session = self.session
        if session is None:
            return
        finished_at = time.monotonic()
        recorder = self.recorder
        recorder.request_stop()
        self.session = None
        self.recorder = self.recorder_factory()
        self.pipeline.submit(session, recorder, finished_at)
        notify("⋯ Processing", "microphone stopped", ms=30000, channel=session.action)
        self._settle_gate()

    def _on_device_lost(self, device):
        self.pressed.pop(device, None)
        if self.persistent is not None and device == self.persistent.device:
            self.persistent.request_stop("keyboard disconnected", paused=True)
        if self.session is not None and device == self.session.device:
            notify("voicekey", "keyboard disconnected; preserving the recording", error=True)
            self._finish()

    def _on_activity(self):
        self.pipeline.spacing.user_typed()

    def _settle_gate(self):
        self.gate.settle(lambda: self.pipeline.ledger.gated)

    def _on_tick(self):
        self._settle_gate()  # retry a shared lock which was occupied at key-down
        if self.persistent is not None:
            self.persistent.tick()
            if self.persistent.done.is_set():
                self._live_session = self.persistent.last_live or self._live_session
                self.persistent = None
        if self.session is None:
            return
        if self.recorder.finished or self.recorder.elapsed >= self.cfg.max_seconds:
            if self.recorder.elapsed >= self.cfg.max_seconds:
                log.warning("recording stopped at %.0fs; preserving available audio", self.cfg.max_seconds)
            self._finish()
