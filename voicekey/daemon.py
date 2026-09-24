"""Keyboard controller and resource ownership for hold/toggle dictation.

Capture decoding, utterance state, pipeline stages and target transactions
live in their own modules. A release transfers ownership to the pipeline;
the ledger retains its gate token throughout that transfer.
"""
from __future__ import annotations

import gc
import glob
import logging
import os
import sys
import threading
import time

from evdev import ecodes

from . import focus
from .control import ControlServer
from .follow import NiriFocusWatch
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
HOLD, TOGGLE, TAP_HOLD = "hold", "toggle", "tap/hold"

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
        self.actions = {_key_chord(cfg.dictate_key): ("persistent", TAP_HOLD),
                        _key_chord(cfg.agent_key): ("agent", HOLD)}
        for chord, action in ((cfg.dictate_toggle_key, "persistent"), (cfg.agent_toggle_key, "agent")):
            if chord:
                self.actions[_key_chord(chord)] = (action, TOGGLE)
        if cfg.persistent.key:
            self.actions[_key_chord(cfg.persistent.key)] = ("persistent", TOGGLE)
        self.recorder_factory = recorder_factory
        self.recorder = recorder_factory()
        self.session = None
        self.persistent = None
        self._pause_reason = ""
        self._typing_fallback = False
        self._gesture = None  # (session, key-down time), until its chord is released
        self.vad = None
        self._vad_slot = Slot("speech-detector")
        self.pressed = {}
        self.backend = self.streaming = self.ime = self.polisher = self.polish_server = None
        self.backend_error = None
        self.gate = Gate()
        self.pipeline = Pipeline(cfg, backend=lambda: self if self.model_state == "loading" else self.backend, polisher=lambda: self.polisher,
                                 settled=self._settle_gate, journal=journal)
        self._live_session = None
        self._stopping = False
        self._closed = False
        self.control = None
        self.listener = None
        self.model_state = "ready"
        self._models_ready = threading.Event()
        self._models_ready.set()
        self._model_thread = None
        self._model_lock = threading.Lock()
        self._unload_requested = False
        self.model_error = ""

    def load(self) -> None:
        """Load both models and register as the input method; each failure
        is reported and degrades the daemon rather than stopping it."""
        self._load_models()
        if self.cfg.dictation.ime:
            try:
                self.ime = InputMethod()
            except ImeUnavailable as exc:
                log.info("no in-field preview: %s", exc)
            except Exception:
                log.exception("input method failed to start; previews use notifications")

    def _load_models(self):
        self.backend_error = None
        try:
            self.backend = create_backend(self.cfg.backend, self.cfg.language)
        except BackendUnavailable as exc:
            self.backend_error = str(exc)
        except Exception as exc:
            self.backend_error = f"{type(exc).__name__}: {exc}"
            log.exception("transcription backend failed to load")
        if self.backend_error:
            notify("voicekey: transcription unavailable", self.backend_error, error=True)
        try:
            self.vad = SpeechDetector(self.cfg.persistent.vad_model)
        except Exception as exc:
            notify("voicekey: dictation unavailable", str(exc), error=True)
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
    def _ensure_models(self):
        if self._unload_requested or self.model_state == "unloading":
            raise ValueError("Wait for memory release to finish")
        if self.model_state != "unloaded":
            return
        self.model_state = "loading"
        self.model_error = ""
        self._models_ready.clear()
        def load():
            with self._model_lock:
                try:
                    self._load_models()
                except Exception as exc:
                    self.backend_error = f"Model loading failed: {exc}"
                    log.exception("model reload failed")
                finally:
                    try:
                        if self._stopping:
                            self._release_models()
                    finally:
                        self.model_state = "ready"
                        self._models_ready.set()
        self._model_thread = threading.Thread(target=load, name="model-load", daemon=True)
        self._model_thread.start()

    def transcribe(self, samples):
        """Cold agent/replay jobs wait inside the supervised transcription slot."""
        if not self._models_ready.wait(self.cfg.pipeline.transcription_seconds):
            raise RuntimeError("Model loading timed out")
        if self.backend is None:
            raise RuntimeError(self.backend_error or "Transcription model unavailable")
        return self.backend.transcribe(samples)

    def _wait_for_models(self, session):
        # Destination binding and capture already happened. Keep all audio in
        # the recorder's bounded buffer while waiting, including a released hold.
        deadline = time.monotonic() + self.cfg.pipeline.transcription_seconds
        while not self._models_ready.wait(.05):
            if self._stopping or time.monotonic() >= min(deadline, session.deadline):
                raise RuntimeError("Model loading interrupted or timed out; audio preserved")
        if self.vad is None or self.backend is None:
            raise RuntimeError(self.backend_error or "Speech models unavailable; audio preserved")
        session.vad, session.streaming = self.vad, self.streaming

    def _release_models(self):
        # Only called with no model users, or during final process shutdown.
        if self.polish_server is not None:
            self.polish_server.stop()
        self.polisher = self.polish_server = None
        self.backend = self.streaming = self.vad = None
        self._live_session = None
        self.backend_error = None
        gc.collect()
        # glibc can retain freed native allocations in its arenas. Returning
        # free pages is best effort; other allocators need no such call here.
        try:
            import ctypes
            libc = ctypes.CDLL(None)
            trim = libc.malloc_trim
            trim.argtypes, trim.restype = [ctypes.c_size_t], ctypes.c_int
            trim(0)
        except (AttributeError, OSError):
            pass

    def _unload_when_idle(self):
        if not self._unload_requested or self.model_state in ("loading", "unloading"):
            return
        if self.persistent is not None or self.session is not None or self.pipeline.ledger.busy:
            return
        # Ledger completion does not imply a timed-out native call has exited.
        if (self._vad_slot.busy or any(slot.busy for slot in self.pipeline._slots.values())
                or self._live_session is not None and self._live_session.stuck):
            self.model_error = "Waiting for a background operation before freeing memory"
            return
        self.model_state = "unloading"
        self.model_error = ""
        def unload():
            with self._model_lock:
                try:
                    self._release_models()
                    self.model_state = "unloaded"
                except Exception as exc:
                    self.model_error = f"Could not free memory: {exc}"
                    self.model_state = "ready"
                    log.exception("model unload failed")
                finally:
                    self._unload_requested = False
        self._model_thread = threading.Thread(target=unload, name="model-unload", daemon=True)
        self._model_thread.start()

    def start_workers(self):
        self.pipeline.start()

    def close(self):
        if self._closed:
            return
        self._closed = self._stopping = True
        if self.control is not None:
            self.control.close()
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
                # A reload finishing after shutdown cleans up its own child.
                # Never block microphone shutdown on a native model loader.
                if self._model_lock.acquire(blocking=False):
                    try:
                        if self.polish_server is not None:
                            self.polish_server.stop()
                    finally:
                        self._model_lock.release()

    def run(self):
        fix_environment()
        self.gate.open()
        self.control = ControlServer()
        self.control.start()
        self.load()
        self.start_workers()
        self.listener = listener = KeyboardListener(
            keycodes=set().union(*self.actions), on_key=self._on_key,
            on_device_lost=self._on_device_lost, on_tick=self._on_tick,
            on_no_access=lambda message: notify("voicekey: no keyboard access", message, error=True),
            on_activity=self._on_activity, wake_fd=self.control.wake_fd,
            enabled=self.cfg.evdev,
        )
        if self.cfg.evdev:
            log.info("listening: %s", ", ".join(self.bindings()))
        else:
            log.info("evdev off: no input devices opened; start and stop through the control socket")
        try:
            listener.run()
        finally:
            listener.close()

    def bindings(self):
        return [f"{key}={action}({behavior})" for key, action, behavior in (
            (self.cfg.dictate_key, "dictate", TAP_HOLD), (self.cfg.agent_key, "agent", HOLD),
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
            if self._gesture is not None:
                persistent, started = self._gesture
                if device == persistent.device and code in persistent.chord:
                    self._gesture = None
                    if persistent is self.persistent and not persistent.stopping.is_set():
                        if time.monotonic() - started >= self.cfg.tap_seconds:
                            persistent.request_stop("key released")
                        else:
                            persistent.stop_instruction = "press a dictation key to stop"
                            persistent.status()
            if (session is not None and session.behavior == HOLD
                    and session.device == device and code in session.chord):
                self._finish()
            pressed.discard(code)
            return
        if value != 1 or code in pressed:
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
                self._retire_persistent()
            else:
                if action == "persistent":
                    self.persistent.request_stop()
                else:
                    notify("voicekey: busy", "stop persistent dictation before using another dictation key")
                return
        if session is not None:
            if behavior == TOGGLE and chord == session.chord and device == session.device:
                self._finish()
            return
        if action == "persistent":
            started = time.monotonic()
            instruction = ("release to stop; tap to keep listening" if behavior == TAP_HOLD
                           else "press a dictation key to stop")
            self._start_persistent(device, chord, instruction=instruction)
            if behavior == TAP_HOLD and self.persistent is not None:
                self._gesture = (self.persistent, started)
            return
        self._start(device, chord, behavior, action,
                    "press again to stop" if behavior == TOGGLE else "release to stop")

    def _retire_persistent(self):
        self._pause_reason = self.persistent.reason if self.persistent.paused else ""
        self._typing_fallback = self.persistent.typing_fallback
        self._live_session = self.persistent.last_live or self._live_session
        self.persistent = None

    def _start_persistent(self, device, chord, recorder=None, *, instruction="press a dictation key to stop",
                          allow_typing=False):
        if allow_typing and self.cfg.dictation.inject != "wtype":
            raise ValueError("Simulated typing is disabled by dictation.inject")
        try:
            self._ensure_models()
        except ValueError as exc:
            notify("voicekey: busy", str(exc), error=True)
            return
        if (self.model_state != "loading" and (self.vad is None or self.backend is None)) or self._vad_slot.busy:
            notify("voicekey: persistent mode unavailable", "speech models unavailable or a detector call is still running", error=True)
            return
        # A new session cannot share the previous gesture's decoder or preview.
        if self.pipeline.ledger.busy or self._live_session is not None and self._live_session.stuck:
            notify("voicekey: busy", "let pending dictation finish before starting persistent mode", error=True)
            return
        track_windows = allow_typing or self.cfg.persistent.destination_policy != "pin"
        watch_factory = (NiriFocusWatch if track_windows and focus.compositor() == "niri"
                         and device != "replay" else None)
        activation_wait = 1.0 if device == "panel" else target_mod.ACTIVATION_WAIT
        def bind():
            nonlocal activation_wait
            wait, activation_wait = activation_wait, target_mod.ACTIVATION_WAIT
            return target_mod.bind(self.ime, self.cfg.dictation, False, activation_wait=wait)
        session = PersistentSession(self.cfg, self.pipeline, recorder or self.recorder_factory(),
            bind, self.vad, self._vad_slot,
            self.streaming, device=device, chord=chord,
            allow_typing=allow_typing,
            prepare_models=self._wait_for_models if self.model_state == "loading" else None,
            watch_factory=watch_factory)
        session.stop_instruction = instruction
        self._pause_reason = ""
        self._typing_fallback = False
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
        try:
            self._ensure_models()
        except ValueError as exc:
            notify("voicekey: busy", str(exc), error=True)
            return
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
        if self._gesture is not None and self._gesture[0].device == device:
            self._gesture = None
        if self.persistent is not None and device == self.persistent.device:
            self.persistent.request_stop("keyboard disconnected", paused=True)
        if self.session is not None and device == self.session.device:
            notify("voicekey", "keyboard disconnected; preserving the recording", error=True)
            self._finish()

    def _on_activity(self):
        self.pipeline.spacing.user_typed()

    def _settle_gate(self):
        self.gate.settle(lambda: self.pipeline.ledger.gated)

    def status(self):
        persistent = self.persistent
        listening = bool(persistent is not None and not persistent.stopping.is_set()) or self.session is not None
        busy = persistent is not None or self.pipeline.ledger.busy
        pause_reason = persistent.reason if persistent is not None and persistent.paused else self._pause_reason
        state = ("listening" if listening else "finishing" if busy else
                 "unloading" if self._unload_requested else
                 self.model_state if self.model_state in ("loading", "unloading", "unloaded") else
                 "unavailable" if self.backend is None or self.vad is None else
                 "paused" if pause_reason else "idle")
        destination = persistent.target.target if persistent is not None else None
        return {"state": state, "listening": listening,
                "binding": persistent is not None and not persistent.ready.is_set(),
                "models": self.model_state, "unload_pending": self._unload_requested,
                "destination_policy": self.cfg.persistent.destination_policy,
                "follow_focus": self.cfg.persistent.destination_policy == "follow",
                "pause_reason": pause_reason,
                "can_type": self._typing_fallback and self.cfg.dictation.inject == "wtype",
                "tracking_notice": persistent.tracking_notice if persistent is not None else "",
                "allow_typing": persistent.allow_typing if persistent is not None else False,
                "can_follow": focus.compositor() == "niri",
                "destination": destination.describe() if destination is not None else "",
                "destination_name": destination.application_name if destination is not None else "",
                "model": self.cfg.backend.type,
                "error": self.model_error or (self.backend_error or
                    ("Speech detector unavailable" if self.vad is None else "")
                    if self.model_state == "ready" else "")}

    def command(self, command):
        if self._stopping:
            raise ValueError("Voicekey is shutting down")
        if command == "free-memory":
            if self.model_state == "unloaded":
                return
            self._unload_requested = True
            self.command("stop")
            self._unload_when_idle()
        elif command == "stop":
            self._pause_reason = ""
            self._typing_fallback = False
            self._gesture = None
            if self.persistent is not None:
                self.persistent.request_stop("stopped from panel")
            if self.session is not None:
                self._finish()
        elif command in ("start", "start-typing"):
            if self.persistent is not None or self.session is not None or self.pipeline.ledger.busy:
                raise ValueError("Finish the current dictation before starting another")
            if self.listener is not None and not self.listener.devices:
                # Typing is invisible without key devices; do not assume the
                # previous dictation still ends at the cursor.
                self.pipeline.spacing.user_typed()
            self._start_persistent("panel", frozenset(), allow_typing=command == "start-typing")
            if self.persistent is None:
                raise ValueError("Could not start dictation; check Voicekey notifications")
        elif command in ("pause-on-switch", "follow-focus", "pin"):
            if self.persistent is not None or self.session is not None or self.pipeline.ledger.busy:
                raise ValueError("Stop dictation before changing destination policy")
            if command == "follow-focus" and focus.compositor() != "niri":
                raise ValueError("Follow focus currently requires Niri")
            policies = {"pause-on-switch": "pause", "follow-focus": "follow", "pin": "pin"}
            self.cfg.persistent.destination_policy = policies[command]
        else:
            raise ValueError("Unknown control command")

    def _on_tick(self):
        if self.control is not None:
            self.control.drain(self.command)
            self.control.publish(self.status())
        self._settle_gate()  # retry a shared lock which was occupied at key-down
        if self.persistent is not None:
            self.persistent.tick()
            if self.persistent.done.is_set():
                self._retire_persistent()
        self._unload_when_idle()
        if self.session is None:
            return
        if self.recorder.finished or self.recorder.elapsed >= self.cfg.max_seconds:
            if self.recorder.elapsed >= self.cfg.max_seconds:
                log.warning("recording stopped at %.0fs; preserving available audio", self.cfg.max_seconds)
            self._finish()
