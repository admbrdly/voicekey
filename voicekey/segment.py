"""Audio-clock speech boundaries, independent of recognition and delivery."""
from dataclasses import dataclass
from pathlib import Path
import os

from .backends import BackendUnavailable, _sherpa
from .recorder import SAMPLE_RATE

WINDOW = 512


class SpeechDetector:
    """Small Silero detector. The segmenter owns pause and maximum-length rules.

    Flush the detector's audio copy after each classification; this retains
    its recurrent model state while bounding its internal audio/segment queue.
    """
    def __init__(self, path):
        path = os.path.expanduser(path)
        if not Path(path).is_file():
            raise BackendUnavailable(f"missing speech detector {path}; run voicekey --download")
        sherpa = _sherpa()
        config = sherpa.VadModelConfig()
        config.silero_vad.model = path
        config.silero_vad.min_silence_duration = 0.032
        config.silero_vad.min_speech_duration = 0.064
        config.silero_vad.max_speech_duration = 60
        config.sample_rate = SAMPLE_RATE
        config.num_threads = 1
        self.vad = sherpa.VoiceActivityDetector(config, buffer_size_in_seconds=2)

    def reset(self):
        self.vad.reset()

    def speech(self, samples):
        self.vad.accept_waveform(samples)
        active = self.vad.is_speech_detected()
        self.vad.flush()
        while not self.vad.empty():
            self.vad.pop()
        return active


@dataclass(frozen=True)
class Boundary:
    kind: str
    start: int
    end: int
    observed: int


class Segmenter:
    def __init__(self, cfg):
        self.pause = int(cfg.pause_seconds * SAMPLE_RATE)
        self.maximum = int(cfg.max_utterance_seconds * SAMPLE_RATE)
        self.lookback = int(cfg.pre_roll_seconds * SAMPLE_RATE)
        self.idle = int(cfg.silence_seconds * SAMPLE_RATE)
        self.position = self.last_speech = self.owned = 0
        self.start = None
        self.discarded = 0

    def feed(self, count, speech):
        before = self.position
        self.position += count
        events = []
        if speech:
            self.last_speech = self.position
            if self.start is None:
                self.start = max(self.owned, before - self.lookback)
                self.discarded += self.start - self.owned
                self.owned = self.start
                events.append(Boundary("start", self.start, self.position, self.position))
        if self.start is not None:
            if self.position - self.start >= self.maximum:
                end, reason = self.start + self.maximum, "maximum"
            elif self.position - self.last_speech >= self.pause:
                end, reason = self.last_speech + self.pause // 2, "pause"
            else:
                return events
            events.append(Boundary(reason, self.start, end, self.position))
            self.owned = end
            self.start = None
            if reason == "maximum":
                self.start = end
                events.append(Boundary("start", end, self.position, self.position))
        if self.start is None:
            keep = max(self.owned, self.position - self.lookback)
            self.discarded += keep - self.owned
            self.owned = keep
        return events

    @property
    def silent(self):
        return self.position - self.last_speech >= self.idle
