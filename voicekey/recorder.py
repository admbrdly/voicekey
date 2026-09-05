"""Microphone capture. ``pw-record`` streams raw 16 kHz mono PCM to its stdout;
a reader thread hands every 100 ms frame to a callback (live recognition) and
keeps it for the final pass. Any command with the same contract can be the
source — ``voicekey.replay`` plays a WAV file at real-time pace for tests."""

from __future__ import annotations

import logging
import signal
import subprocess
import threading
import time
from collections import deque

import numpy as np

log = logging.getLogger("voicekey.recorder")

SAMPLE_RATE = 16000
FRAME_SAMPLES = SAMPLE_RATE // 10
PW_RECORD = [
    "pw-record", "--raw", "--format", "s16",
    "--rate", str(SAMPLE_RATE), "--channels", "1", "-",
]


class RecordingError(Exception):
    def __init__(self, message: str, samples=None, duration: float = 0.0):
        super().__init__(message)
        self.samples = samples if samples is not None else np.zeros(0, dtype=np.float32)
        self.duration = duration


class Recorder:
    def __init__(self, argv: list[str] = PW_RECORD) -> None:
        self.argv = argv
        self.proc: subprocess.Popen | None = None
        self.thread: threading.Thread | None = None
        self.frames: list[np.ndarray] = []
        self.started = 0.0
        self.stopped_at: float | None = None
        self._errors = deque(maxlen=8)
        self._stderr_thread: threading.Thread | None = None
        self._failure: str | None = None
        self.max_samples: int | None = None

    @property
    def active(self) -> bool:
        return self.proc is not None

    @property
    def elapsed(self) -> float:
        return (self.stopped_at or time.monotonic()) - self.started if self.active else 0.0

    @property
    def finished(self) -> bool:
        """The source exited by itself: end of a replayed file, or a failure."""
        return self.proc is not None and (self.proc.poll() is not None or self._failure is not None)

    def start(self, on_frame) -> None:
        assert not self.active
        self.proc = subprocess.Popen(
            self.argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE
        )
        self.frames = []
        self.started = time.monotonic()
        self.stopped_at = None
        self._failure = None
        self._errors.clear()
        self._stderr_thread = threading.Thread(
            target=self._drain_errors, args=(self.proc.stderr,), name="recorder-stderr", daemon=True,
        )
        self._stderr_thread.start()
        self.thread = threading.Thread(
            target=self._pump, args=(self.proc.stdout, on_frame),
            name="recorder", daemon=True,
        )
        self.thread.start()
        log.info("recording")

    def _pump(self, stdout, on_frame) -> None:
        count = 0
        try:
            while data := stdout.read(FRAME_SAMPLES * 2):
                if len(data) % 2:
                    self._failure = "audio source returned a partial PCM sample"
                    data = data[:-1]
                frame = np.frombuffer(data, dtype=np.int16).astype(np.float32) / 32768.0
                if self.max_samples is not None:
                    frame = frame[:max(0, self.max_samples - count)]
                count += len(frame)
                self.frames.append(frame)
                try:
                    on_frame(frame)
                except Exception:
                    log.exception("live recognition failed on a frame")
                if self.max_samples is not None and count >= self.max_samples:
                    self._failure = "recording reached its sample limit"
                    self.request_stop()
                    break
        except Exception as exc:
            self._failure = str(exc)
        finally:
            stdout.close()

    def _drain_errors(self, stderr) -> None:
        try:
            while chunk := stderr.read(512):
                self._errors.append(chunk)
        except (OSError, ValueError):
            pass
        finally:
            stderr.close()

    def request_stop(self) -> None:
        """Signal promptly at key release; joining and decoding happen elsewhere."""
        if self.stopped_at is None:
            self.stopped_at = time.monotonic()
        if self.proc is not None and self.proc.poll() is None:
            try:
                self.proc.send_signal(signal.SIGINT)
            except ProcessLookupError:
                pass

    def stop(self) -> tuple[np.ndarray, float]:
        """Stop the source; return (samples, duration). Raises RecordingError
        if the source failed before we stopped it (no microphone, PipeWire down)."""
        assert self.proc is not None and self.thread is not None
        proc, thread = self.proc, self.thread
        duration = self.elapsed
        failed = proc.poll() not in (None, 0)
        self.request_stop()
        try:
            proc.wait(timeout=2.0)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=1.0)
        thread.join(1.0)
        self._stderr_thread.join(1.0)
        # Never wait on a BufferedReader's internal lock if a bad callback or
        # inherited descriptor left its reader stuck. That reader owns closure.
        if not thread.is_alive():
            proc.stdout.close()
        if not self._stderr_thread.is_alive():
            proc.stderr.close()
        self.proc = self.thread = None
        stderr = b"".join(self._errors).decode(errors="replace").strip()[-500:]
        frames = tuple(self.frames)
        samples = np.concatenate(frames) if frames else np.zeros(0, dtype=np.float32)
        if failed or self._failure or thread.is_alive():
            raise RecordingError(
                f"{self.argv[0]} exited early (rc={proc.returncode}): "
                f"{self._failure or stderr or 'audio reader did not stop'}",
                samples, duration,
            )
        log.info("recorded %.2fs", duration)
        return samples, duration

    def abort(self) -> None:
        """Stop and discard (accidental tap, stuck key, device disconnect)."""
        if not self.active:
            return
        try:
            self.stop()
        except RecordingError:
            pass
