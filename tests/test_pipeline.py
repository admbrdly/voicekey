from __future__ import annotations

import json
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import numpy as np

from voicekey.capture import Session
from voicekey.config import Config
from voicekey.pipeline import Pipeline
from voicekey.recovery import Journal
from voicekey.target import Landing, Outcome


class FakeRecorder:
    def __init__(self, duration=1):
        self.active = False
        self.finished = False
        self.elapsed = duration
        self.duration = duration
        self.samples = np.zeros(int(duration * 16000), dtype=np.float32)
        self.stopping = threading.Event()

    def start(self, on_frame):
        self.active = True
        self.on_frame = on_frame

    def request_stop(self):
        self.stopping.set()

    def stop(self):
        self.active = False
        return self.samples, self.duration


class FakeTarget:
    def __init__(self, outcome=Outcome.CONFIRMED):
        self.window_id = 7
        self.app_id = 'fake'
        self.preview = self
        self.closed = False
        self.cancelled = threading.Event()
        self.calls = []
        self.shown = []
        self.outcome = outcome

    def before(self, wait=0):
        return None

    def show(self, text):
        if not self.closed:
            self.shown.append(text)

    def clear(self):
        self.closed = True

    def cancel(self):
        self.cancelled.set()
        self.clear()

    def land(self, text, deadline, **kwargs):
        self.calls.append((text, deadline, kwargs))
        return Landing(self.outcome)


def wait_for(predicate, timeout=3):
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() > deadline:
            raise AssertionError('timed out')
        time.sleep(0.005)


class StartupRecoveryTests(unittest.TestCase):
    def test_startup_reports_recovery_before_accepting_new_work_without_model_calls(self):
        with tempfile.TemporaryDirectory() as directory:
            journal = Journal(directory + '/sessions')
            journal.append('f', 'session-start')
            journal.append('a', 'segment', session_id='f', sequence=0)
            journal.append('a', 'final', final='Saved before the crash.')
            backend, polisher = Mock(), Mock()
            pipeline = Pipeline(Config(), backend=backend, polisher=polisher, journal=journal)
            with patch('voicekey.pipeline.notify') as notify:
                pipeline.start()
                self.addCleanup(pipeline.close)
            self.assertIn(str(journal.path('f', '.recovery.txt')), notify.call_args.args[1])
            backend.assert_not_called()
            polisher.assert_not_called()
            self.assertFalse(pipeline._storage_failed)
            self.assertFalse(pipeline.ledger.busy)
            identity = pipeline.admit()
            self.assertIsNotNone(identity)
            pipeline.ledger.complete(identity, 'dropped')


class PipelineTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.notice = patch('voicekey.pipeline.notify').start()
        self.copy = patch('voicekey.pipeline.inject.copy').start()
        self.addCleanup(patch.stopall)
        self.cfg = Config()
        self.cfg.pipeline.shutdown_seconds = 0.3
        self.cfg.pipeline.journal_seconds = 0.3
        self.backend = Mock(transcribe=Mock(return_value='hello'))
        self.polisher = None
        self.journal = Journal(self.tmp.name + '/sessions')
        self.pipeline = Pipeline(self.cfg, backend=lambda: self.backend,
                                 polisher=lambda: self.polisher, journal=self.journal)
        self.pipeline.start()
        self.addCleanup(self.pipeline.close)

    def submit(self, target=None, *, live='live fallback', age=0, action='dictate'):
        identity = self.pipeline.admit()
        self.assertIsNotNone(identity)
        session = Session(action, 'hold', frozenset(), 'fake', identity=identity)
        session.target = target or FakeTarget()
        session.text = live
        self.pipeline.submit(session, FakeRecorder(), time.monotonic() - age)
        return session

    def done(self):
        wait_for(lambda: not self.pipeline.ledger.busy)

    def events(self, identity):
        return [json.loads(line) for line in self.journal.path(identity, '.jsonl').read_text().splitlines()]

    def test_audio_precedes_transcription_and_text_precedes_attempt(self):
        target = FakeTarget()
        def transcribe(samples):
            self.assertTrue(list(Path(self.journal.directory).glob('*.wav')))
            return 'hello'
        self.backend.transcribe.side_effect = transcribe
        original = target.land
        def land(text, deadline, **kwargs):
            events = self.events(session.id)
            self.assertEqual([e['event'] for e in events],
                             ['captured', 'transcribed', 'final', 'delivery-attempt'])
            return original(text, deadline, **kwargs)
        target.land = land
        session = self.submit(target)
        self.done()
        self.assertEqual(target.calls[0][0], 'hello')
        self.assertFalse(self.journal.path(session.id, '.wav').exists())
        self.assertEqual(self.events(session.id)[-1]['outcome'], 'confirmed')

    def test_short_text_skips_polish_and_threshold_is_configurable(self):
        self.polisher = Mock(polish=Mock(return_value='Hello.'))
        first = self.submit()
        self.done()
        self.polisher.polish.assert_not_called()
        self.cfg.polish.min_words = 0
        second = self.submit()
        self.done()
        self.polisher.polish.assert_called_once()
        self.assertEqual(self.polisher.polish.call_args.kwargs, {'app_id': 'fake'})
        self.assertEqual(second.target.calls[0][0], 'Hello.')

    def test_hung_polish_adopts_raw_and_late_result_never_lands(self):
        entered, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)
        self.cfg.polish.min_words = 0
        self.cfg.polish.max_wait_seconds = 0.05
        def polish(text, wait, *, app_id=None):
            entered.set()
            release.wait(2)
            return 'late rewrite'
        self.polisher = Mock(polish=polish)
        first = self.submit()
        self.assertTrue(entered.wait(1))
        second = self.submit()
        self.done()
        self.assertEqual(first.target.calls[0][0], 'hello')
        self.assertEqual(second.target.calls[0][0], 'hello')
        release.set()
        self.pipeline._slots['polish'].join(1)
        self.assertEqual(len(first.target.calls), 1)

    def test_short_dictation_does_not_overtake_earlier_polish(self):
        entered, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)
        self.backend.transcribe.side_effect = ['this is a long enough sentence to receive polish', 'short']
        def polish(text, wait, *, app_id=None):
            entered.set()
            release.wait(2)
            return 'polished sentence'
        self.polisher = Mock(polish=polish)
        first = self.submit()
        self.assertTrue(entered.wait(1))
        second = self.submit()
        wait_for(lambda: self.backend.transcribe.call_count == 2)
        self.assertEqual(second.target.calls, [])
        release.set()
        self.done()
        self.assertEqual([x.id for x in self.pipeline.ledger.history], [first.id, second.id])

    def test_blocked_delivery_does_not_delay_transcription(self):
        entered, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)
        target = FakeTarget()
        def land(*args, **kwargs):
            entered.set()
            release.wait(2)
            return Landing(Outcome.CONFIRMED)
        target.land = land
        self.submit(target)
        self.assertTrue(entered.wait(1))
        self.submit()
        wait_for(lambda: self.backend.transcribe.call_count == 2)
        release.set()
        self.done()

    def test_failed_transcription_uses_live_and_retains_audio_when_no_live(self):
        self.backend.transcribe.side_effect = RuntimeError('native failed')
        first = self.submit()
        self.done()
        self.assertEqual(first.target.calls[0][0], 'live fallback')
        second = self.submit(live='')
        self.done()
        self.assertTrue(self.journal.path(second.id, '.wav').exists())
        self.assertEqual(self.events(second.id)[-1]['outcome'], 'saved')

    def test_expired_work_is_preserved_before_copy_without_delivery(self):
        session = self.submit(age=20)
        self.done()
        self.assertEqual(session.target.calls, [])
        self.copy.assert_called_once_with('live fallback')
        self.assertIn('live fallback', (Path(self.tmp.name) / 'last-recovery.txt').read_text())
        self.assertTrue(self.journal.path(session.id, '.wav').exists())

    def test_uncertain_delivery_never_copies_or_retries(self):
        session = self.submit(FakeTarget(Outcome.UNKNOWN))
        self.done()
        self.copy.assert_not_called()
        self.assertEqual(self.events(session.id)[-1]['outcome'], 'unknown')
        self.assertEqual(len(session.target.calls), 1)

    def test_refusals_preserve_each_transcript_when_clipboard_is_overwritten(self):
        self.backend.transcribe.side_effect = ['first', 'second']
        first = self.submit(FakeTarget(Outcome.REFUSED))
        second = self.submit(FakeTarget(Outcome.REFUSED))
        self.done()
        self.assertIn('first', self.journal.path(first.id, '.txt').read_text())
        self.assertIn('second', self.journal.path(second.id, '.txt').read_text())
        self.assertEqual(self.copy.call_count, 2)

    def test_empty_polish_result_never_discards_raw(self):
        self.cfg.polish.min_words = 0
        self.polisher = Mock(polish=Mock(return_value=''))
        session = self.submit()
        self.done()
        self.assertEqual(session.target.calls[0][0], 'hello')

    def test_empty_transcript_drops_only_after_recording_was_saved(self):
        self.backend.transcribe.return_value = ''
        session = self.submit(live='')
        self.done()
        self.assertEqual(self.events(session.id)[0]['event'], 'captured')
        self.assertEqual(self.events(session.id)[-1]['outcome'], 'dropped')
        self.assertEqual(session.target.calls, [])

    def test_admission_bounds_all_stages(self):
        entered, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)
        def transcribe(samples):
            entered.set()
            release.wait(2)
            return 'hello'
        self.backend.transcribe.side_effect = transcribe
        self.submit()
        self.assertTrue(entered.wait(1))
        admitted = []
        while (identity := self.pipeline.admit()) is not None:
            admitted.append(identity)
        self.assertTrue(admitted)
        self.assertLessEqual(len(admitted) + 1, self.cfg.pipeline.max_pending)
        for identity in admitted:
            self.pipeline.ledger.complete(identity, 'dropped')
        release.set()
        self.done()

    def test_disk_failure_disables_new_capture_and_does_not_deliver(self):
        with patch.object(self.journal, 'capture', side_effect=OSError('disk full')):
            session = self.submit()
            self.done()
        self.assertEqual(session.target.calls, [])
        self.assertIsNone(self.pipeline.admit())

    def test_shutdown_invalidates_late_polish_and_revokes_delivery_permission(self):
        entered, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)
        self.cfg.polish.min_words = 0
        def polish(text, wait, *, app_id=None):
            entered.set()
            release.wait(2)
            return 'late'
        self.polisher = Mock(polish=polish)
        session = self.submit()
        self.assertTrue(entered.wait(1))
        self.pipeline.close(timeout=0.03)
        release.set()
        self.pipeline._slots['polish'].join(1)
        self.assertEqual(session.target.calls, [])
        self.assertFalse(self.pipeline.ledger.busy)
        self.assertFalse(self.journal.path(session.id, '.permit').exists())
        self.assertIn('hello', self.journal.path(session.id, '.txt').read_text())

    def test_live_fallback_keeps_original_audio_after_insertion(self):
        self.backend.transcribe.side_effect = RuntimeError('failed recognizer')
        session = self.submit()
        self.done()
        self.assertEqual(session.target.calls[0][0], 'live fallback')
        self.assertTrue(self.journal.path(session.id, '.wav').exists())
        self.assertFalse(self.journal.path(session.id, '.done').exists())

    def test_storage_capacity_is_reserved_before_admission(self):
        self.journal.available = self.pipeline._disk_reservation - 1
        self.assertIsNone(self.pipeline.admit())
        self.journal.available = self.pipeline._disk_reservation
        first = self.pipeline.admit()
        self.assertIsNotNone(first)
        self.assertIsNone(self.pipeline.admit())
        self.pipeline.ledger.complete(first, 'dropped')

    def test_optional_corpus_failure_does_not_disable_dictation(self):
        self.cfg.recordings_dir = self.tmp.name + '/corpus'
        with patch('voicekey.pipeline.recovery.keep', side_effect=OSError('corpus disk full')):
            session = self.submit()
            self.done()
        self.assertEqual(session.target.calls[0][0], 'hello')
        self.assertFalse(self.pipeline._storage_failed)

    def test_full_capture_is_preserved_on_shutdown_during_finalization(self):
        entered, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)
        identity = self.pipeline.admit()
        session = Session('dictate', 'hold', frozenset(), 'fake', identity=identity)
        session.target = FakeTarget()
        recorder = FakeRecorder()
        original = recorder.stop
        def stop():
            entered.set()
            release.wait(2)
            return original()
        recorder.stop = stop
        self.pipeline.submit(session, recorder, time.monotonic())
        self.assertTrue(entered.wait(1))
        self.pipeline.close(timeout=0.02)
        release.set()
        for thread in self.pipeline._threads:
            thread.join(0.3)
        self.assertTrue(self.journal.path(identity, '.wav').exists())
        self.assertEqual(session.target.calls, [])

    def test_busy_agent_has_its_own_cap_and_does_not_block_dictation(self):
        entered, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)
        self.cfg.pipeline.max_pending = self.pipeline.ledger.max_pending = 1
        def send(text):
            entered.set()
            release.wait(2)
        self.pipeline._send_agent = Mock(side_effect=send)
        first = self.submit(action='agent')
        self.assertTrue(entered.wait(1))
        self.assertFalse(self.pipeline.ledger.gated)
        second = self.submit()
        wait_for(lambda: self.pipeline.ledger.get(second.id) is None)
        self.assertEqual(second.target.calls[0][0], 'hello')
        extra = self.submit(action='agent')
        wait_for(lambda: self.pipeline.ledger.get(extra.id) is None)
        self.assertEqual(self.events(extra.id)[-1]['outcome'], 'saved')
        self.assertEqual(self.pipeline._send_agent.call_count, 1)
        release.set()
        self.done()
