from __future__ import annotations

import os
import sys
import tempfile
import threading
import time
import unittest
import wave

import numpy as np

from voicekey.recorder import FRAME_SAMPLES, Recorder, RecordingError

PYTHON = sys.executable


def _wait(predicate, timeout=5.0) -> None:
    deadline = time.monotonic() + timeout
    while not predicate():
        assert time.monotonic() < deadline, "timed out"
        time.sleep(0.02)


class RecorderTests(unittest.TestCase):
    def test_frames_reach_callback_and_stop_returns_all_samples(self):
        source = [PYTHON, "-c", (
            "import sys, numpy as np; "
            "sys.stdout.buffer.write((np.arange(4000) % 1000).astype('<i2').tobytes())"
        )]
        frames = []
        recorder = Recorder(source)
        recorder.start(frames.append)
        _wait(lambda: recorder.finished)
        samples, _duration = recorder.stop()
        self.assertEqual(len(samples), 4000)
        self.assertEqual([len(f) for f in frames], [FRAME_SAMPLES, FRAME_SAMPLES, 800])
        self.assertAlmostEqual(float(samples[999]), 999 / 32768, places=6)
        self.assertFalse(recorder.active)

    def test_source_failure_is_reported_with_stderr(self):
        source = [PYTHON, "-c", "import sys; sys.stderr.write('no microphone'); sys.exit(1)"]
        recorder = Recorder(source)
        recorder.start(lambda frame: None)
        _wait(lambda: recorder.finished)
        with self.assertRaisesRegex(RecordingError, "no microphone"):
            recorder.stop()

    def test_spawn_failure_leaves_recorder_inactive(self):
        recorder = Recorder(["/nonexistent/pw-record"])
        with self.assertRaises(OSError):
            recorder.start(lambda frame: None)
        self.assertFalse(recorder.active)

    def test_replay_source_paces_a_wav_file(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "half-second.wav")
            with wave.open(path, "wb") as wav:
                wav.setnchannels(1)
                wav.setsampwidth(2)
                wav.setframerate(16000)
                wav.writeframes(np.zeros(8000, dtype=np.int16).tobytes())
            recorder = Recorder([PYTHON, "-m", "voicekey.replay", path])
            started = time.monotonic()
            recorder.start(lambda frame: None)
            _wait(lambda: recorder.finished)
            samples, duration = recorder.stop()
        self.assertEqual(len(samples), 8000)
        self.assertGreaterEqual(time.monotonic() - started, 0.4)
        self.assertGreaterEqual(duration, 0.4)


if __name__ == "__main__":
    unittest.main()

class RecorderFailureTests(unittest.TestCase):
    def test_requested_stop_may_exit_nonzero_before_finalizer_runs(self):
        source = [PYTHON, '-c', 'import signal,sys,time; '
                  'signal.signal(signal.SIGINT, lambda *_: sys.exit(1)); '
                  'sys.stdout.buffer.write(bytes(3200)); sys.stdout.buffer.flush(); time.sleep(30)']
        ready = threading.Event()
        recorder = Recorder(source)
        recorder.start(lambda _: ready.set())
        self.addCleanup(recorder.abort)
        self.assertTrue(ready.wait(2))
        recorder.request_stop()
        _wait(lambda: recorder.finished)
        samples, _duration = recorder.stop()
        self.assertEqual(len(samples), 1600)

    def test_stop_request_after_unexpected_exit_does_not_hide_failure(self):
        recorder = Recorder([PYTHON, '-c', "import sys; sys.stderr.write('microphone failed'); sys.exit(1)"])
        recorder.start(lambda _: None)
        self.addCleanup(recorder.abort)
        _wait(lambda: recorder.finished)
        recorder.request_stop()
        with self.assertRaisesRegex(RecordingError, 'microphone failed'):
            recorder.stop()

    def test_closed_stdout_with_live_child_does_not_hang_stop(self):
        source = [PYTHON, '-c', "import signal,os,time; signal.signal(signal.SIGINT, signal.SIG_IGN); os.close(1); time.sleep(30)"]
        recorder = Recorder(source)
        recorder.start(lambda frame: None)
        proc = recorder.proc
        try:
            recorder.thread.join(2)
            self.assertFalse(recorder.thread.is_alive())
            began = time.monotonic()
            recorder.stop()
            self.assertLess(time.monotonic() - began, 3.5)
            self.assertIsNotNone(proc.poll())
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait()

    def test_loud_stderr_cannot_block_capture_and_failure_retains_samples(self):
        source = [PYTHON, '-c', "import os,sys; os.write(1, b'\\x00\\x01' * 1600); os.write(2, b'x' * 200000); sys.exit(1)"]
        recorder = Recorder(source)
        recorder.start(lambda frame: None)
        _wait(lambda: recorder.finished)
        with self.assertRaises(RecordingError) as error:
            recorder.stop()
        self.assertEqual(len(error.exception.samples), 1600)
        self.assertLess(len(str(error.exception)), 1000)

    def test_sample_limit_bounds_memory_even_when_listener_does_not_tick(self):
        source = [PYTHON, '-c', "import os; os.write(1, b'\\x00\\x01' * 16000)"]
        recorder = Recorder(source)
        recorder.max_samples = 3200
        recorder.start(lambda frame: None)
        _wait(lambda: recorder.finished)
        with self.assertRaises(RecordingError) as error:
            recorder.stop()
        self.assertEqual(len(error.exception.samples), 3200)
