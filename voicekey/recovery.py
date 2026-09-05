"""Keep transcripts that could not be delivered, and (opt-in) recordings."""

from __future__ import annotations

import contextlib
import json
import os
import tempfile
import threading
import time
import wave
import uuid
from pathlib import Path

import numpy as np

STATE_DIR = os.path.join(
    os.environ.get("XDG_STATE_HOME", os.path.expanduser("~/.local/state")),
    "voicekey",
)
LAST_RECOVERY = os.path.join(STATE_DIR, "last-recovery.txt")


_saving = threading.Lock()


def save(text: str) -> str:
    """Retain an undelivered transcript, mode 0600, and return its path.

    Written whole to a private temporary file and renamed into place, under
    a lock: the delivery and transcription workers can both fail at once,
    and the file must always hold one complete transcript."""
    with _saving:
        os.makedirs(STATE_DIR, mode=0o700, exist_ok=True)
        os.chmod(STATE_DIR, 0o700)
        fd, temporary = tempfile.mkstemp(prefix=".last-recovery-", dir=STATE_DIR)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                os.fchmod(fd, 0o600)
                handle.write(text)
                handle.write("\n")
            os.replace(temporary, LAST_RECOVERY)
            # A second failure must not erase the first one. The last-file
            # remains a convenience; the unique record is the recovery source.
            archive = os.path.join(STATE_DIR, "recovered")
            os.makedirs(archive, mode=0o700, exist_ok=True)
            with open(os.path.join(archive, uuid.uuid4().hex + ".txt"), "x", encoding="utf-8") as handle:
                os.fchmod(handle.fileno(), 0o600)
                handle.write(text + "\n")
        except BaseException:
            with contextlib.suppress(OSError):
                os.unlink(temporary)
            raise
    return LAST_RECOVERY


class Journal:
    """Per-dictation records, written on pipeline workers before side effects.

    Unresolved audio is retained until recovery. Successful audio is removed
    after its transcripts and outcome are saved. Successful text expires after
    history_days. Unresolved entries are never automatically removed; reaching
    the quota refuses new work. This protects process-crash recovery, not an
    in-memory recording or an unflushed filesystem after power loss.
    """
    SUCCESS = {"confirmed", "submitted", "dropped"}

    def __init__(self, directory: str | None = None, *, megabytes=256, history_days=7):
        self.directory = Path(directory or os.path.join(STATE_DIR, "sessions"))
        self.limit = megabytes * 1024 * 1024
        self.history_days = history_days
        self._lock = threading.Lock()
        self.available = 0

    def prepare(self, required: int = 0) -> None:
        with self._lock:
            self._prepare(required)

    def _prepare(self, required: int = 0) -> None:
        self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(self.directory, 0o700)
        cutoff = time.time() - self.history_days * 86400
        for path in self.directory.glob("*.done"):
            if path.stat().st_mtime < cutoff:
                for suffix in (".jsonl", ".txt", ".wav", ".done"):
                    path.with_suffix(suffix).unlink(missing_ok=True)
        size = sum(p.stat().st_size for p in self.directory.iterdir() if p.is_file())
        if size + required > self.limit:
            for done in sorted(self.directory.glob("*.done"), key=lambda p: p.stat().st_mtime):
                for suffix in (".jsonl", ".txt", ".wav", ".done"):
                    path = done.with_suffix(suffix)
                    if path.exists():
                        size -= path.stat().st_size
                        path.unlink()
                if size + required <= self.limit:
                    break
        if size + required > self.limit:
            raise OSError(f"recovery quota reached; recover or remove files in {self.directory}")
        self.available = self.limit - size

    def path(self, identity: str, suffix: str) -> Path:
        if not identity or any(c not in "0123456789abcdef" for c in identity):
            raise ValueError("invalid utterance id")
        return self.directory / (identity + suffix)

    def capture(self, identity: str, samples: np.ndarray, live: str) -> str:
        with self._lock:
            path = self.path(identity, ".wav")
            if path.exists():
                return str(path)
            self._prepare(samples.nbytes // 2 + 4096)
            fd, temporary = tempfile.mkstemp(prefix=".audio-", dir=self.directory)
            try:
                with os.fdopen(fd, "wb") as handle:
                    os.fchmod(handle.fileno(), 0o600)
                    with wave.open(handle, "wb") as wav:
                        wav.setnchannels(1)
                        wav.setsampwidth(2)
                        wav.setframerate(16000)
                        wav.writeframes((np.clip(samples, -1, 1) * 32767).astype(np.int16).tobytes())
                os.replace(temporary, path)
                self.available -= path.stat().st_size
            finally:
                with contextlib.suppress(FileNotFoundError):
                    os.unlink(temporary)
            self._append(identity, "captured", live=live, samples=len(samples))
            return str(path)

    def append(self, identity: str, event: str, **data) -> str:
        with self._lock:
            return self._append(identity, event, **data)

    def recover(self, identity: str, text: str) -> str:
        """Refresh the convenience file; the per-ID journal remains authoritative."""
        destination = self.directory.parent / "last-recovery.txt"
        with self._lock:
            fd, temporary = tempfile.mkstemp(prefix=".recovery-", dir=destination.parent)
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as handle:
                    os.fchmod(fd, 0o600)
                    handle.write(text + "\n")
                os.replace(temporary, destination)
            finally:
                with contextlib.suppress(FileNotFoundError):
                    os.unlink(temporary)
        return str(self.path(identity, ".txt"))

    def revoke(self, identity: str) -> None:
        self.path(identity, ".permit").unlink(missing_ok=True)

    def _append(self, identity: str, event: str, **data) -> str:
        self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        entry = {"id": identity, "event": event, "time": time.time(), **data}
        encoded = json.dumps(entry, ensure_ascii=False) + "\n"
        with open(self.path(identity, ".jsonl"), "a", encoding="utf-8") as handle:
            os.fchmod(handle.fileno(), 0o600)
            handle.write(encoded)
        self.available -= len(encoded.encode())
        # This is a readable append-only companion, not a second mutable truth.
        with open(self.path(identity, ".txt"), "a", encoding="utf-8") as handle:
            os.fchmod(handle.fileno(), 0o600)
            readable = f"[{event}]\n"
            for key, value in data.items():
                if value:
                    readable += f"{key}: {value}\n"
            handle.write(readable)
        self.available -= len(readable.encode())
        if event == "delivery-attempt":
            with open(self.path(identity, ".permit"), "w") as handle:
                os.fchmod(handle.fileno(), 0o600)
        if event == "outcome":
            self.revoke(identity)
        if event == "outcome" and data.get("outcome") in self.SUCCESS and not data.get("recovery_needed"):
            audio = self.path(identity, ".wav")
            if audio.exists():
                size = audio.stat().st_size
                audio.unlink()
                self.available += size
            with open(self.path(identity, ".done"), "w") as handle:
                os.fchmod(handle.fileno(), 0o600)
        return str(self.path(identity, ".txt"))


def keep(directory: str, samples: np.ndarray, live_text: str, text: str,
         polished: str | None = None) -> str:
    """Debug aid (``recordings_dir``): store the audio and every transcript
    of a recording so models can be compared on real speech later."""
    os.makedirs(directory, mode=0o700, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S") + f"-{int(time.time() * 1000) % 1000:03d}"
    base = os.path.join(directory, stamp)
    with wave.open(base + ".wav", "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(16000)
        wav.writeframes((np.clip(samples, -1, 1) * 32767).astype(np.int16).tobytes())
    with open(base + ".txt", "w", encoding="utf-8") as handle:
        handle.write(f"live:  {live_text}\nfinal: {text}\n")
        if polished is not None:
            handle.write(f"polished: {polished}\n")
    return base
