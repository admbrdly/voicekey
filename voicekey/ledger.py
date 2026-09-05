"""Authoritative dictation lifecycle. No I/O and no callbacks under its lock."""
from __future__ import annotations

import threading
import uuid
from collections import deque
from dataclasses import dataclass, replace
from enum import StrEnum


class Stage(StrEnum):
    CAPTURING = "capturing"
    FINALIZING = "finalizing"
    TRANSCRIBING = "transcribing"
    POLISHING = "polishing"
    READY = "ready"
    DELIVERING = "delivering"
    TERMINAL = "terminal"


NEXT = {Stage.CAPTURING: Stage.FINALIZING, Stage.FINALIZING: Stage.TRANSCRIBING,
        Stage.TRANSCRIBING: Stage.POLISHING, Stage.POLISHING: Stage.READY,
        Stage.READY: Stage.DELIVERING}


@dataclass(frozen=True)
class Utterance:
    id: str
    session_id: str = ""
    sequence: int = 0
    stage: Stage = Stage.CAPTURING
    revision: int = 0
    audio_seconds: float = 0.0
    live: str = ""
    raw: str = ""
    final: str = ""
    attempt: str = ""
    outcome: str = ""
    gated: bool = True
    storage_bytes: int = 0


class Ledger:
    def __init__(self, max_pending: int = 8, max_audio_seconds: float = 180.0):
        self.max_pending = max_pending
        self.max_audio_seconds = max_audio_seconds
        self._lock = threading.Lock()
        self._pending: dict[str, Utterance] = {}
        self.history: deque[Utterance] = deque(maxlen=32)

    def admit(self, audio_seconds: float, *, retained_audio: float = 0.0,
              storage_bytes: int = 0, storage_available: float = float("inf"),
              session_id: str = "", sequence: int = 0, gated: bool = True) -> str | None:
        with self._lock:
            # Agent dispatch has its own bounded backlog after releasing its
            # gate ownership. It must not consume all dictation admission.
            if (sum(u.gated or bool(u.session_id) for u in self._pending.values()) >= self.max_pending or
                    sum(u.audio_seconds for u in self._pending.values()) + audio_seconds + retained_audio > self.max_audio_seconds):
                return None
            if sum(u.storage_bytes for u in self._pending.values()) + storage_bytes > storage_available:
                return None
            identity = uuid.uuid4().hex
            self._pending[identity] = Utterance(identity, session_id=session_id, sequence=sequence,
                                               gated=gated, audio_seconds=audio_seconds, storage_bytes=storage_bytes)
            return identity

    def speech(self, identity: str) -> bool:
        with self._lock:
            current = self._pending.get(identity)
            if current is None or current.stage != Stage.CAPTURING:
                return False
            self._pending[identity] = replace(current, gated=True)
            return True

    def get(self, identity: str) -> Utterance | None:
        with self._lock:
            return self._pending.get(identity)

    def snapshots(self) -> tuple[Utterance, ...]:
        with self._lock:
            return tuple(self._pending.values())

    @property
    def busy(self) -> bool:
        with self._lock:
            return bool(self._pending)

    @property
    def gated(self) -> bool:
        with self._lock:
            return any(item.gated for item in self._pending.values())

    def transition(self, identity: str, expected: Stage, stage: Stage, **changes) -> bool:
        with self._lock:
            current = self._pending.get(identity)
            if current is None or current.stage != expected:
                return False
            if NEXT.get(expected) != stage:
                raise ValueError(f"invalid transition: {expected} -> {stage}")
            self._pending[identity] = replace(current, stage=stage, revision=current.revision + 1, **changes)
            return True

    def live(self, identity: str, text: str) -> bool:
        with self._lock:
            current = self._pending.get(identity)
            if current is None or current.stage not in (Stage.CAPTURING, Stage.FINALIZING):
                return False
            self._pending[identity] = replace(current, live=text, revision=current.revision + 1)
            return True

    def reserve(self, identity: str) -> str | None:
        """A commit is reserved once; rendering never reserves an operation."""
        attempt = uuid.uuid4().hex
        if self.transition(identity, Stage.READY, Stage.DELIVERING, attempt=attempt):
            return attempt
        return None

    def complete(self, identity: str, outcome: str) -> bool:
        with self._lock:
            current = self._pending.pop(identity, None)
            if current is None:
                return False
            self.history.append(replace(current, stage=Stage.TERMINAL,
                                        revision=current.revision + 1, outcome=outcome))
            return True
