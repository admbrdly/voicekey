import json
import sys
import tempfile
import threading
import time
import unittest
import wave
from pathlib import Path
from unittest.mock import Mock, patch

import numpy as np
from evdev import ecodes

from voicekey.config import Config, PersistentConfig
from voicekey.daemon import Daemon
from voicekey.gate import Gate
from voicekey.ledger import Ledger, Stage
from voicekey.persistent import PersistentSession
from voicekey.pipeline import Pipeline
from voicekey.recorder import AudioBuffer, Recorder, RecordingError
from voicekey.recovery import Journal
from voicekey.segment import Segmenter, WINDOW
from voicekey.session_target import SessionTarget
from voicekey.target import EmacsTarget, ImeTarget, ImePreview, NotifyPreview, Window, Outcome
from voicekey.work import Slot
from tests.test_ime import _offline_input_method
from tests.test_pipeline import wait_for


class BufferAndSegmentTests(unittest.TestCase):
    def test_retrospective_cut_retains_exact_suffix_and_bounds_memory(self):
        audio = AudioBuffer(100)
        self.assertTrue(audio.append(np.arange(80, dtype=np.float32)))
        original = audio.read()
        head = audio.read(0, 37)
        audio.discard_before(37)
        np.testing.assert_array_equal(np.concatenate([head, audio.read()]), original)
        self.assertTrue(audio.append(np.arange(80, 120, dtype=np.float32)))
        self.assertFalse(audio.append(np.arange(120, 150, dtype=np.float32)))
        self.assertEqual((audio.start, audio.end), (37, 137))
        np.testing.assert_array_equal(audio.read(), np.arange(37, 137, dtype=np.float32))

    def test_pause_is_retrospective_and_hard_cut_is_exact_under_large_frames(self):
        cfg = PersistentConfig(pause_seconds=.4, max_utterance_seconds=1, pre_roll_seconds=.1)
        segment = Segmenter(cfg)
        self.assertEqual(segment.feed(1600, False), [])
        events = segment.feed(6400, True)
        self.assertEqual(events[0].start, 0)
        events = segment.feed(6400, False)
        self.assertEqual((events[0].kind, events[0].end, events[0].observed), ('pause', 11200, 14400))
        # Speech after the pause reuses the retained suffix, never old speech.
        events = segment.feed(1600, True)
        self.assertEqual(events[0].start, 12800)
        events = segment.feed(16000, True)
        cut = next(e for e in events if e.kind == 'maximum')
        self.assertEqual(cut.end - cut.start, 16000)
        self.assertEqual(events[-1].start, cut.end)

    def test_no_speech_releases_history_and_uses_audio_clock(self):
        s = Segmenter(PersistentConfig(silence_seconds=2, pre_roll_seconds=.1))
        for _ in range(100):
            s.feed(512, False)
            self.assertLessEqual(s.position - s.owned, 1600)
        self.assertTrue(s.silent)
        s.feed(512, True)
        self.assertFalse(s.silent)


class SessionTargetTests(unittest.TestCase):
    def setUp(self):
        self.ime = _offline_input_method()
        self.addCleanup(self.ime._close_pipe)
        self.ime._post = lambda fn: (fn() or True)
        self.ime._on_activate(None)
        self.ime._on_done(None)
        self.ledger = Ledger()
        self.focus = patch('voicekey.target.focus.window_id', return_value=7).start()
        patch('voicekey.target.notify').start()
        patch('voicekey.session_target.emacs.unpin').start()
        self.addCleanup(patch.stopall)

    def entry(self, text):
        identity = self.ledger.admit(1, session_id='session')
        self.ledger.live(identity, text)
        return identity

    def test_empty_notification_preview_is_silent_and_can_show_later_text(self):
        shared = SessionTarget('session', EmacsTarget(NotifyPreview('dictate'),
            Window(7, True), 'emacs', Mock(id='pin', valid=True)), self.ledger)
        with patch('voicekey.target.notify') as notify:
            shared.render()
            notify.assert_not_called()
            first = self.entry('Hello')
            shared.render()
            self.assertEqual(notify.call_args.args[1], 'Hello')
            notify.reset_mock()
            self.ledger.complete(first, 'confirmed')
            shared.render()
            notify.assert_not_called()

    def test_repeated_commits_keep_newer_pending_preview_in_same_protocol_transaction(self):
        binding = ImeTarget(self.ime, 1, Window(7, True), 'browser')
        shared = SessionTarget('session', binding, self.ledger)
        first, second = self.entry('first live'), self.entry('second live')
        shared.render()
        self.assertEqual(self.ime._preview_text, 'first live second live')
        result = shared.attempt(first).land('First.', time.monotonic()+1, operation_id='one')
        self.assertEqual(result.outcome, Outcome.SUBMITTED)
        self.assertIn(('preedit', ' second live', 12, 12), self.ime._im.calls)
        self.ledger.complete(first, 'submitted')
        shared.render()
        self.assertNotIn('first', self.ime._preview_text)
        result = shared.attempt(second).land('Second.', time.monotonic()+1, operation_id='two')
        self.assertEqual(result.outcome, Outcome.SUBMITTED)
        self.assertEqual([c[1] for c in self.ime._im.calls if c[0]=='commit_string'], ['First.', 'Second.'])

    def test_final_waiting_for_commit_stays_visible_and_unicode_preview_is_bounded(self):
        binding = ImeTarget(self.ime, 1, Window(7, True), 'browser')
        shared = SessionTarget('session', binding, self.ledger)
        first = self.entry('live')
        for before, after in ((Stage.CAPTURING, Stage.FINALIZING), (Stage.FINALIZING, Stage.TRANSCRIBING),
                              (Stage.TRANSCRIBING, Stage.POLISHING), (Stage.POLISHING, Stage.READY)):
            self.ledger.transition(first, before, after, final='Final.')
        self.entry('界' * 2000)
        shared.render()
        self.assertTrue(self.ime._preview_text.startswith('Final.'))
        self.assertLessEqual(len(self.ime._preview_text.encode()), 4000)

    def test_emacs_background_delivery_reuses_pin_and_does_not_show_in_other_field(self):
        binding = EmacsTarget(ImePreview(self.ime, 1), Window(7, True), 'emacs', Mock(id='pin', valid=True))
        shared = SessionTarget('session', binding, self.ledger)
        first = self.entry('one')
        self.ime._on_deactivate(None)
        self.ime._on_done(None)
        self.ime._on_activate(None)
        self.ime._on_done(None)
        self.focus.side_effect = AssertionError('must not depend on focus')
        with patch('voicekey.session_target.emacs.insert') as insert:
            self.assertTrue(shared.available())
            result = shared.attempt(first).land('One.', time.monotonic()+1, operation_id='one')
            self.assertEqual(result.outcome, Outcome.CONFIRMED)
            self.assertTrue(insert.call_args.kwargs['keep_pin'])
            self.assertEqual(insert.call_args.args[1], 'pin')
        self.assertEqual(self.ime._im.calls, [])

    def test_reactivated_generic_field_is_unavailable_and_never_rebound(self):
        binding = ImeTarget(self.ime, 1, Window(7, True), 'browser')
        shared = SessionTarget('session', binding, self.ledger)
        identity = self.entry('old')
        self.ime._on_deactivate(None)
        self.ime._on_done(None)
        self.ime._on_activate(None)
        self.ime._on_done(None)
        self.assertFalse(shared.available())
        result = shared.attempt(identity).land('old', time.monotonic()+1, operation_id='one')
        self.assertEqual(result.outcome, Outcome.REFUSED)
        self.assertTrue(shared.failed.is_set())
        self.assertEqual(self.ime._im.calls, [])

    def test_finalized_filler_is_removed_from_preview_while_its_drop_waits_in_queue(self):
        shared = SessionTarget('session', ImeTarget(self.ime, 1, Window(7, True), 'browser'), self.ledger)
        filler = self.entry('Um')
        self.entry('Actual words')
        for before, after in ((Stage.CAPTURING, Stage.FINALIZING), (Stage.FINALIZING, Stage.TRANSCRIBING),
                              (Stage.TRANSCRIBING, Stage.POLISHING), (Stage.POLISHING, Stage.READY)):
            self.ledger.transition(filler, before, after, raw='Um', final='')
        shared.render()
        self.assertEqual(self.ime._preview_text, 'Actual words')


class ControlledRecorder:
    def __init__(self):
        self.active = self.finished = False
        self.buffer = None
        self.failure = ''

    def start(self, callback):
        self.started = time.monotonic()
        self.active = True
        self.callback = callback

    def push(self, samples):
        self.assert_active()
        if not self.buffer.append(samples):
            self.failure = 'capture buffer full'
            self.finished = True
        self.callback(samples)

    def assert_active(self):
        if not self.active:
            raise AssertionError('capture stopped')

    def request_stop(self):
        self.finished = True

    def abort(self):
        self.request_stop()
        try:
            self.stop()
        except RecordingError:
            pass

    def stop(self):
        self.active = False
        samples = self.buffer.read()
        if self.failure:
            raise RecordingError(self.failure, samples, len(samples)/16000)
        return samples, len(samples)/16000


class PersistentTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.addCleanup(patch.stopall)
        for module in ('persistent', 'pipeline', 'target', 'daemon'):
            patch(f'voicekey.{module}.notify').start()
        self.insert = patch('voicekey.session_target.emacs.insert').start()
        patch('voicekey.session_target.emacs.unpin').start()
        self.copy = patch('voicekey.pipeline.inject.copy').start()
        self.cfg = Config()
        self.cfg.persistent.follow_focus = False  # these tests exercise pinned sessions
        self.cfg.persistent.key = 'KEY_F11'
        self.cfg.persistent.pause_seconds = .2
        self.cfg.persistent.pre_roll_seconds = .05
        self.cfg.persistent.silence_seconds = 20
        self.cfg.persistent.max_utterance_seconds = 2
        self.cfg.pipeline.shutdown_seconds = .5
        self.backend = Mock(transcribe=Mock(return_value='Text.'))
        self.pipeline = Pipeline(self.cfg, backend=lambda: self.backend, polisher=lambda: None,
                                 journal=Journal(self.tmp.name+'/sessions'))
        self.pipeline.start()
        self.addCleanup(self.pipeline.close)
        self.vad = Mock(speech=lambda samples: bool(np.max(np.abs(samples)) > .01))
        self.binding = EmacsTarget(NotifyPreview('dictate'), Window(7, True), 'emacs',
                                   Mock(id='pin', valid=True, before=Mock(return_value=None)))
        self.recorder = ControlledRecorder()
        self.session = None

    def start(self, recorder=None):
        self.session = PersistentSession(self.cfg, self.pipeline, recorder or self.recorder,
            self.binding, self.vad, Slot('test-vad'), None, device='keyboard', chord=frozenset({ecodes.KEY_F11}))
        self.addCleanup(self.session.close)
        self.assertTrue(self.session.start())
        wait_for(lambda: self.session.ready.is_set() or self.session.done.is_set())
        return self.session

    def speak(self):
        self.recorder.push(np.ones(5120, dtype=np.float32)*.2)
        self.recorder.push(np.zeros(5120, dtype=np.float32))

    def finish(self):
        self.session.request_stop()
        self.assertTrue(self.session.done.wait(5))

    def test_failed_binding_aborts_early_audio_without_flush_or_recovery(self):
        from voicekey.target import ClipboardTarget
        def bind():
            self.assertTrue(self.recorder.active)
            self.recorder.push(np.ones(6400, dtype=np.float32) * .2)
            return ClipboardTarget(NotifyPreview('dictate'), Window(None, True), None)
        self.binding = bind
        with patch.object(self.recorder, 'abort', wraps=self.recorder.abort) as abort, \
                patch.object(PersistentSession, '_flush') as flush:
            self.start()
            self.assertTrue(self.session.done.wait(2))
        abort.assert_called_once()
        flush.assert_not_called()
        self.assertFalse(self.recorder.active)
        self.backend.transcribe.assert_not_called()
        self.assertEqual(self.session.reason, 'persistent mode needs an insertion destination')
        self.assertFalse(list(Path(self.tmp.name).rglob('*.wav')))
        self.assertFalse(self.pipeline.ledger.busy)

    def test_speech_during_blocked_binding_is_captured_from_first_sample(self):
        entered, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)
        binding = self.binding
        def bind():
            self.assertTrue(self.recorder.active)
            entered.set()
            release.wait(2)
            return binding
        self.session = PersistentSession(self.cfg, self.pipeline, self.recorder,
            bind, self.vad, Slot('test-vad'), None, device='keyboard', chord=frozenset())
        self.addCleanup(self.session.close)
        self.assertTrue(self.session.start())
        self.assertTrue(entered.wait(1))
        samples = np.linspace(.1, .9, 5120, dtype=np.float32)
        self.recorder.push(samples)
        # Even stopping before binding completes must retain the opening audio.
        self.session.request_stop()
        release.set()
        self.assertTrue(self.session.done.wait(3))
        np.testing.assert_array_equal(self.backend.transcribe.call_args.args[0], samples)

    def test_microphone_start_failure_releases_admission_without_binding(self):
        binding = Mock()
        self.session = PersistentSession(self.cfg, self.pipeline, self.recorder,
            binding, self.vad, Slot('test-vad'), None, device='keyboard', chord=frozenset())
        with patch.object(self.recorder, 'start', side_effect=OSError('microphone unavailable')):
            with self.assertRaisesRegex(OSError, 'microphone unavailable'):
                self.session.start()
        binding.assert_not_called()
        self.assertFalse(self.pipeline.ledger.busy)

    def test_failure_after_classified_silence_does_not_create_an_utterance(self):
        self.start()
        self.recorder.push(np.zeros(5120, dtype=np.float32))
        wait_for(lambda: self.session.segmenter.position == 5120)
        self.recorder.failure = 'microphone failed'
        self.finish()
        self.backend.transcribe.assert_not_called()
        self.assertFalse(list(Path(self.tmp.name).rglob('*.wav')))

    def test_invalid_pin_aborts_audio_captured_while_waiting_for_acknowledgement(self):
        entered, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)
        def before(wait):
            self.assertGreater(wait, 0)
            entered.set()
            release.wait(2)
            return None
        self.binding.pinning.before.side_effect = before
        self.binding.pinning.valid = False
        self.binding.pinning.reason = ''

        self.session = PersistentSession(self.cfg, self.pipeline, self.recorder,
            self.binding, self.vad, Slot('test-vad'), None, device='keyboard', chord=frozenset())
        self.addCleanup(self.session.close)
        self.assertTrue(self.session.start())
        self.assertTrue(entered.wait(1))
        self.assertTrue(self.recorder.active)
        self.recorder.push(np.ones(5120, dtype=np.float32) * .2)
        release.set()
        self.assertTrue(self.session.done.wait(2))
        self.assertFalse(self.session.ready.is_set())
        self.assertIn('Emacs did not acknowledge', self.session.reason)
        self.assertFalse(self.recorder.active)
        self.assertFalse(list(Path(self.tmp.name).rglob('*.wav')))
        self.backend.transcribe.assert_not_called()

    def test_microphone_failure_while_idle_is_reported_as_paused_with_actual_error(self):
        self.start()
        # Three actual recorder frames leave 192 samples beyond the VAD window.
        self.recorder.push(np.zeros(4800, dtype=np.float32))
        wait_for(lambda: self.session.segmenter.position == 4608)
        with patch('voicekey.persistent.notify') as notify:
            self.recorder.failure = 'pw-record: microphone disconnected'
            self.recorder.finished = True
            self.session.wake.set()
            self.assertTrue(self.session.done.wait(3))
            self.session.thread.join(1)
        self.assertTrue(self.session.paused)
        self.assertEqual(self.session.reason, self.recorder.failure)
        self.assertIn('paused', notify.call_args.args[0])
        self.assertIn(self.recorder.failure, notify.call_args.args[1])
        self.backend.transcribe.assert_not_called()
        self.assertFalse(list(Path(self.tmp.name).rglob('*.wav')))
        self.assertFalse((Path(self.tmp.name) / 'last-recovery.txt').exists())

    def test_wtype_focus_queries_are_spaced_at_least_one_second_apart(self):
        from voicekey.target import WtypeTarget
        self.binding = WtypeTarget(NotifyPreview('dictate'), Window(7, True), 'browser')
        calls = []
        def focused(**kwargs):
            calls.append((time.monotonic(), threading.current_thread().name))
            return 7
        with patch('voicekey.target.focus.window_id', side_effect=focused):
            self.start()
            wait_for(lambda: len(calls) >= 2)
            self.finish()
        self.assertTrue(all(name == 'persistent-capture' for _, name in calls))
        self.assertTrue(all(b[0] - a[0] >= 1 for a, b in zip(calls, calls[1:])))

    def test_microphone_failure_preserves_speech_in_the_partial_window_without_padding(self):
        self.start()
        samples = np.concatenate([np.zeros(4608, dtype=np.float32),
                                  np.full(192, .2, dtype=np.float32)])
        self.recorder.push(samples)
        wait_for(lambda: self.session.segmenter.position == 4608)
        self.recorder.failure = 'microphone failed'
        self.finish()
        np.testing.assert_array_equal(self.backend.transcribe.call_args.args[0], samples[-992:])
        self.assertTrue(self.session.paused)
        self.assertTrue(list(Path(self.tmp.name).rglob('*.wav')))

    def test_failure_classifying_the_partial_window_preserves_its_audio(self):
        self.start()
        self.recorder.push(np.zeros(4800, dtype=np.float32))
        wait_for(lambda: self.session.segmenter.position == 4608)
        self.vad.speech = Mock(side_effect=RuntimeError('detector failed'))
        self.recorder.failure = 'microphone failed'
        self.finish()
        self.vad.speech.assert_called_once()
        self.assertEqual(len(self.vad.speech.call_args.args[0]), WINDOW)
        self.assertEqual(len(self.backend.transcribe.call_args.args[0]), 992)
        self.assertTrue(list(Path(self.tmp.name).rglob('*.wav')))
        self.session.thread.join(1)
        from voicekey import persistent
        self.assertIn('microphone failed; final speech detection failed: detector failed', self.session.reason)
        self.assertIn(self.session.reason, persistent.notify.call_args.args[1])

    def test_segment_bookkeeping_failure_is_not_reported_as_detector_failure(self):
        self.start()
        self.session.segmenter.feed = Mock(side_effect=RuntimeError('bookkeeping failed'))
        self.vad.speech = Mock(wraps=self.vad.speech)
        with patch.object(self.session, '_flush', wraps=self.session._flush) as flush:
            self.recorder.push(np.full(5120, .2, dtype=np.float32))
            self.assertTrue(self.session.done.wait(3))
        self.assertEqual(self.session.reason, 'speech segmentation failed: bookkeeping failed')
        self.assertFalse(flush.call_args.kwargs['detector_failed'])
        # A healthy detector still classifies the remaining audio during flush.
        self.assertEqual(len(self.vad.speech.call_args.args[0]), 5120)
        self.assertEqual(len(self.backend.transcribe.call_args.args[0]), 5120)

    def test_detector_initialization_failure_preserves_even_a_subwindow_of_audio(self):
        entered, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)
        def reset():
            entered.set()
            release.wait(2)
            raise RuntimeError('detector initialization failed')
        self.vad.reset = reset
        self.vad.speech = Mock()
        self.session = PersistentSession(self.cfg, self.pipeline, self.recorder,
            self.binding, self.vad, Slot('test-vad'), None, device='keyboard', chord=frozenset())
        self.addCleanup(self.session.close)
        self.assertTrue(self.session.start())
        self.assertTrue(entered.wait(1))
        samples = np.full(192, .2, dtype=np.float32)
        self.recorder.push(samples)
        release.set()
        self.assertTrue(self.session.done.wait(3))
        self.vad.speech.assert_not_called()
        np.testing.assert_array_equal(self.backend.transcribe.call_args.args[0], samples)
        self.assertTrue(self.session.paused)

    def test_focus_poll_runs_on_capture_worker_and_tick_does_not_wait(self):
        entered, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)
        self.start()
        def available():
            self.assertEqual(threading.current_thread().name, 'persistent-capture')
            entered.set()
            release.wait(2)
            return True
        with patch.object(self.session.target, 'available', side_effect=available):
            self.assertTrue(entered.wait(1))
            self.session.tick()
            release.set()
            self.finish()

    def test_backlog_recovery_contains_all_sentences_in_sequence(self):
        from voicekey.emacs import EmacsRefused
        entered, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)
        def insert(*args, **kwargs):
            entered.set()
            release.wait(2)
            raise EmacsRefused('buffer killed')
        self.insert.side_effect = insert
        self.backend.transcribe.side_effect = ['First sentence.', 'Second sentence.', 'Third sentence.']
        self.start()
        for _ in range(3):
            self.speak()
        self.assertTrue(entered.wait(1))
        wait_for(lambda: self.backend.transcribe.call_count == 3)
        release.set()
        self.assertTrue(self.session.done.wait(3))
        text = (Path(self.tmp.name) / 'last-recovery.txt').read_text()
        self.assertEqual(text, 'First sentence.\n\nSecond sentence.\n\nThird sentence.\n')
        path = self.pipeline.journal.path(self.session.id, '.recovery.txt')
        self.assertEqual(path.read_text(), text)
        self.session.thread.join(1)
        from voicekey import persistent
        self.assertIn(str(path), persistent.notify.call_args.args[1])

    def test_slow_cancelled_decoder_is_replaced_lazily_with_full_suffix(self):
        entered, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)
        streams = []
        class Stream:
            def __init__(self):
                self.frames = []
                streams.append(self)
            def feed(self, samples):
                self.frames.append(samples.copy())
                if self is streams[0]:
                    entered.set()
                    release.wait(3)
                return 'live'
            def finish(self):
                return 'live'
        self.start()
        self.session.streaming = Mock(session=Stream)
        self.recorder.push(np.full(512, .1, dtype=np.float32))
        self.assertTrue(entered.wait(1))
        self.recorder.push(np.full(32768, .2, dtype=np.float32))
        # Admission increments the session counter before _cut replaces current.
        # Wait for the new utterance itself, not the reservation counter.
        wait_for(lambda: self.session.current.sequence == 1 and self.session.active)
        old = self.session.last_live
        self.assertTrue(old.stuck)
        self.assertIsNone(self.session.current.decoder)
        release.set()
        old.decoder.join(1)
        self.recorder.push(np.full(1024, .3, dtype=np.float32))
        wait_for(lambda: len(streams) == 2 and streams[1].frames)
        self.assertEqual(streams[1].frames[0][0], np.float32(.2))
        self.finish()

    def test_multiple_utterances_commit_while_capture_continues_and_idle_gate_is_free(self):
        self.start()
        self.assertFalse(self.pipeline.ledger.gated)
        self.speak()
        wait_for(lambda: self.insert.call_count == 1)
        self.assertTrue(self.recorder.active)
        wait_for(lambda: not self.pipeline.ledger.gated)
        self.speak()
        wait_for(lambda: self.insert.call_count == 2)
        self.finish()
        self.assertFalse(self.pipeline.ledger.busy)
        segments = [json.loads(line) for p in Path(self.tmp.name+'/sessions').glob('*.jsonl')
                    for line in p.read_text().splitlines() if json.loads(line)['event']=='segment']
        segments.sort(key=lambda x: x['sequence'])
        self.assertEqual(len(segments), 2)
        self.assertLessEqual(segments[0]['end_sample'], segments[1]['start_sample'])
        manifest = next(json.loads(line) for line in self.pipeline.journal.path(self.session.id,'.jsonl').read_text().splitlines()
                        if json.loads(line)['event']=='session-end')
        self.assertEqual(sum(x['end_sample']-x['start_sample'] for x in segments), manifest['assigned_samples'])
        self.assertEqual(manifest['assigned_samples']+manifest['discarded_silence_samples'], 20480)

    def test_fillers_drop_without_insertion_and_short_meaningful_text_is_polished(self):
        self.backend.transcribe.side_effect = ['Errrr, uhhhh, gah.', 'Is it gonna work?']
        polisher = Mock(polish=Mock(return_value='Is it going to work?'))
        self.pipeline.polisher = lambda: polisher
        self.start()
        self.speak()
        wait_for(lambda: any(u.raw for u in self.pipeline.ledger.history))
        self.insert.assert_not_called()
        polisher.polish.assert_not_called()
        first = next(u for u in self.pipeline.ledger.history if u.raw)
        self.assertEqual((first.outcome, first.final), ('dropped', ''))
        events = [json.loads(line) for line in self.pipeline.journal.path(first.id,'.jsonl').read_text().splitlines()]
        self.assertTrue(any(e.get('raw')=='Errrr, uhhhh, gah.' for e in events))
        self.assertFalse(any(e['event']=='delivery-attempt' for e in events))
        self.assertEqual(events[-1]['reason'], 'filler-only utterance')
        self.speak()
        wait_for(lambda: self.insert.call_count==1)
        self.finish()
        self.assertEqual(self.insert.call_args.args[0], 'Is it going to work?')
        self.assertEqual(polisher.polish.call_args.args[0], 'Is it gonna work?')
        self.assertEqual(polisher.polish.call_args.kwargs, {'app_id': 'emacs'})
        self.copy.assert_not_called()

    def test_empty_model_reply_never_discards_meaningful_short_persistent_text(self):
        self.backend.transcribe.return_value = 'Do not publish.'
        polisher = Mock(polish=Mock(return_value=''))
        self.pipeline.polisher = lambda: polisher
        self.start()
        self.speak()
        wait_for(lambda: self.insert.call_count==1)
        self.finish()
        polisher.polish.assert_called_once()
        self.assertEqual(self.insert.call_args.args[0], 'Do not publish.')

    def test_literal_fillers_can_be_kept_and_persistent_polish_threshold_is_separate(self):
        self.cfg.persistent.drop_filler_only = False
        self.cfg.persistent.polish_min_words = 3
        self.cfg.polish.min_words = 0
        self.backend.transcribe.return_value = 'Um'
        polisher = Mock()
        self.pipeline.polisher = lambda: polisher
        self.start()
        self.speak()
        wait_for(lambda: self.insert.call_count==1)
        self.finish()
        polisher.polish.assert_not_called()
        self.assertEqual(self.insert.call_args.args[0], 'Um')

    def test_silence_turns_off_without_ever_submitting_silence_for_transcription(self):
        self.cfg.persistent.silence_seconds = .5
        self.start()
        self.recorder.push(np.zeros(10000, dtype=np.float32))
        self.assertTrue(self.session.done.wait(5))
        self.assertEqual(self.session.reason, 'silence timeout')
        self.assertFalse(self.recorder.active)
        self.backend.transcribe.assert_not_called()

    def test_overload_preserves_current_speech_and_stops_instead_of_losing_the_suffix(self):
        self.pipeline.ledger.max_pending = 1
        self.start()
        self.speak()
        self.assertTrue(self.session.done.wait(5))
        self.assertTrue(self.session.paused)
        self.assertIn('limit', self.session.reason)
        self.assertEqual(len(self.backend.transcribe.call_args.args[0]), 10240)
        self.copy.assert_not_called()

    def test_stopping_during_speech_preserves_partial_utterance(self):
        self.start()
        self.recorder.push(np.ones(1711,dtype=np.float32)*.2)
        self.finish()
        self.assertEqual(len(self.backend.transcribe.call_args.args[0]), 1711)
        self.assertEqual(self.insert.call_count, 1)

    def test_drain_deadline_preserves_audio_and_rejects_late_results(self):
        entered, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)
        def transcribe(samples):
            entered.set()
            release.wait(4)
            return 'Late.'
        self.backend.transcribe.side_effect = transcribe
        self.cfg.pipeline.shutdown_seconds = .1
        self.start()
        self.speak()
        self.assertTrue(entered.wait(2))
        self.finish()
        self.assertTrue(list(Path(self.tmp.name+'/sessions').glob('*.wav')))
        release.set()
        self.pipeline._slots['transcribe'].join(1)
        self.insert.assert_not_called()
        self.assertFalse(self.pipeline.ledger.busy)

    def test_real_paced_wav_uses_one_capture_process_across_cuts(self):
        path = Path(self.tmp.name)/'continuous.wav'
        speech = np.ones(5120,dtype=np.int16)*10000
        silence = np.zeros(5120,dtype=np.int16)
        with wave.open(str(path),'wb') as wav:
            wav.setnchannels(1); wav.setsampwidth(2); wav.setframerate(16000)
            wav.writeframes(np.concatenate([speech,silence,speech,silence]).tobytes())
        recorder = Recorder([sys.executable, '-m', 'voicekey.replay', str(path)])
        self.start(recorder)
        process = recorder.proc
        self.assertTrue(self.session.done.wait(5))
        self.assertEqual(self.insert.call_count, 2)
        self.assertIsNotNone(process.returncode)
        self.assertFalse(recorder.active)

    def test_hard_cuts_cover_every_speech_sample_once_and_replay_suffix_to_new_decoder(self):
        streams = []
        class Stream:
            def __init__(self):
                self.frames = []
                streams.append(self)
            def feed(self, samples):
                self.frames.append(samples.copy())
                return 'live'
            def finish(self):
                return 'live'
        self.start()
        self.session.streaming = Mock(session=Stream)
        samples = np.linspace(.1, .9, 56000, dtype=np.float32)
        self.recorder.push(samples)
        wait_for(lambda: self.session.segmenter.position >= len(samples)-512)
        self.finish()
        recorded = [call.args[0] for call in self.backend.transcribe.call_args_list]
        self.assertEqual([len(x) for x in recorded], [32000, 24000])
        np.testing.assert_array_equal(np.concatenate(recorded), samples)
        self.assertGreaterEqual(len(streams), 2)
        self.assertEqual(streams[1].frames[0][0], samples[32000])

    def test_exact_hard_cut_followed_by_silence_does_not_create_empty_utterance(self):
        self.cfg.persistent.max_utterance_seconds = 2.048  # exactly 64 detector windows
        self.start()
        self.recorder.push(np.ones(32768,dtype=np.float32)*.2)
        self.recorder.push(np.zeros(5120,dtype=np.float32))
        wait_for(lambda: self.session.segmenter.position >= 37376)
        self.finish()
        self.assertEqual(self.backend.transcribe.call_count, 1)
        self.assertEqual(len(self.backend.transcribe.call_args.args[0]),32768)

    def test_native_vad_timeout_stops_and_preserves_unclassified_audio(self):
        entered, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)
        def speech(samples):
            entered.set()
            release.wait(4)
            return True
        self.vad.speech = speech
        self.start()
        self.recorder.push(np.ones(10240,dtype=np.float32)*.2)
        self.assertTrue(entered.wait(1))
        self.assertTrue(self.session.done.wait(4))
        self.assertTrue(self.session.vad_slot.busy)
        self.assertTrue(self.session.paused)
        self.assertTrue(list(Path(self.tmp.name+'/sessions').glob('*.wav')))
        release.set()
        self.session.vad_slot.join(1)

    def test_dead_emacs_target_stops_capture_and_preserves_text_without_clipboard(self):
        from voicekey.emacs import EmacsRefused
        self.insert.side_effect = EmacsRefused('buffer killed')
        self.start()
        self.speak()
        self.assertTrue(self.session.done.wait(5))
        self.assertTrue(self.session.paused)
        self.copy.assert_not_called()
        self.assertIn('Text.', (Path(self.tmp.name)/'last-recovery.txt').read_text())

    def test_generic_focus_loss_stops_capture_and_does_not_insert_elsewhere(self):
        ime = _offline_input_method()
        self.addCleanup(ime._close_pipe)
        ime._post = lambda fn: (fn() or True)
        ime._on_activate(None); ime._on_done(None)
        self.binding = ImeTarget(ime, 1, Window(7, True), 'browser')
        with patch('voicekey.target.focus.window_id', return_value=7):
            self.start()
            self.recorder.push(np.ones(5120,dtype=np.float32)*.2)
            wait_for(lambda: self.session.active)
            ime._on_deactivate(None); ime._on_done(None)
            self.session.tick()
            self.assertTrue(self.session.done.wait(5))
        self.assertTrue(self.session.paused)
        self.assertFalse(any(c[0]=='commit_string' for c in ime._im.calls))
        self.copy.assert_not_called()

    def test_cleanup_uses_only_previous_delivered_batch_and_records_context(self):
        from voicekey.polish import Polisher, Reply, S1MiniFormat
        import json
        backend = Mock(chat=Mock(side_effect=[Reply("I'd like to", True),
                                              Reply("I'd like to go to the store.", True)]))
        cleaner = Polisher(backend, S1MiniFormat('semi-formal'), 1)
        self.pipeline.polisher = lambda: cleaner
        self.backend.transcribe.side_effect = ["I'd like to", 'Go to the store.']
        self.start()
        self.speak()
        wait_for(lambda: bool(self.pipeline._polish_context.get(self.session.id, ('', ''))[1]))
        self.speak()
        wait_for(lambda: self.insert.call_count == 2)
        self.finish()
        self.assertEqual([call.args[0] for call in self.insert.call_args_list], ["I'd like to", 'go to the store.'])
        events = [json.loads(line) for path in Path(self.tmp.name+'/sessions').glob('*.jsonl')
                  for line in path.read_text().splitlines()]
        self.assertTrue(any(e.get('polish_context') == "I'd like to" for e in events))
        self.assertNotIn(self.session.id, self.pipeline._polish_context)

    def test_typing_or_disabled_context_omits_previous_batch(self):
        for disabled in (False, True):
            with self.subTest(disabled=disabled):
                cleaner = Mock(polish=Mock(return_value='Text.'))
                self.pipeline.polisher = lambda: cleaner
                self.cfg.persistent.polish_context = not disabled
                self.start(recorder=ControlledRecorder())
                self.recorder = self.session.recorder
                self.speak()
                wait_for(lambda: bool(self.pipeline._polish_context.get(self.session.id, ('', ''))[1]))
                if not disabled:
                    self.pipeline.spacing.user_typed()
                self.speak()
                wait_for(lambda: cleaner.polish.call_count == 2)
                self.finish()
                self.assertNotIn('context', cleaner.polish.call_args.kwargs)

    def test_typing_during_context_cleanup_falls_back_to_raw(self):
        cleaner = Mock(polish=Mock(return_value='Text.'))
        self.pipeline.polisher = lambda: cleaner
        self.start()
        self.speak()
        wait_for(lambda: bool(self.pipeline._polish_context.get(self.session.id, ('', ''))[1]))
        def clean(*args, **kwargs):
            self.assertEqual(kwargs.get('context'), 'Text.')
            self.pipeline.spacing.user_typed()
            return 'changed'
        cleaner.polish.side_effect = clean
        self.backend.transcribe.return_value = 'Raw words.'
        self.speak()
        wait_for(lambda: self.insert.call_count == 2)
        self.finish()
        self.assertEqual(self.insert.call_args.args[0], 'Raw words.')

    def test_pending_batch_prevents_using_older_delivered_context(self):
        cleaner = Mock(polish=Mock(return_value='Text.'))
        self.pipeline.polisher = lambda: cleaner
        self.start()
        self.speak()
        wait_for(lambda: bool(self.pipeline._polish_context.get(self.session.id, ('', ''))[1]))
        entered, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)
        def insert(*args, **kwargs):
            entered.set()
            release.wait(3)
        self.insert.side_effect = insert
        self.speak()
        self.assertTrue(entered.wait(1))
        self.speak()
        wait_for(lambda: cleaner.polish.call_count == 3)
        self.assertEqual(cleaner.polish.call_args_list[1].kwargs.get('context'), 'Text.')
        self.assertNotIn('context', cleaner.polish.call_args_list[2].kwargs)
        release.set()
        self.finish()

    def dictation_daemon(self):
        daemon = Daemon(self.cfg, recorder_factory=ControlledRecorder, journal=self.pipeline.journal)
        daemon.pipeline = self.pipeline
        daemon.gate = Gate(self.tmp.name+'/lock')
        daemon.gate.open()
        daemon.vad, daemon.backend = self.vad, self.backend
        self.addCleanup(daemon.close)
        patch('voicekey.daemon.target_mod.bind', return_value=self.binding).start()
        return daemon

    def test_default_hotkey_works_without_an_extra_persistent_key(self):
        self.cfg.persistent.key = ''
        daemon = self.dictation_daemon()
        with patch('voicekey.daemon.create_backend', return_value=self.backend), \
                patch('voicekey.daemon.create_streaming', return_value=None), \
                patch('voicekey.daemon.SpeechDetector', return_value=self.vad) as detector:
            self.cfg.dictation.ime = False
            daemon.load()
        detector.assert_called_once_with(self.cfg.persistent.vad_model)
        daemon._on_key('keyboard', ecodes.KEY_RIGHTMETA, 1)
        self.assertIsNotNone(daemon.persistent)
        daemon._on_device_lost('keyboard')
        self.assertTrue(daemon.persistent.done.wait(5))

    def test_default_hotkey_hold_delivers_before_release_and_flushes_tail(self):
        daemon = self.dictation_daemon()
        daemon._on_key('keyboard', ecodes.KEY_RIGHTMETA, 1)
        session = daemon.persistent
        wait_for(session.ready.is_set)
        # Feed one complete batch while the key remains held.
        session.recorder.push(np.ones(5120, dtype=np.float32)*.2)
        session.recorder.push(np.zeros(5120, dtype=np.float32))
        wait_for(lambda: self.insert.call_count == 1)
        self.assertFalse(session.stopping.is_set())
        session.recorder.push(np.ones(5120, dtype=np.float32)*.2)
        daemon._gesture = (session, time.monotonic() - self.cfg.tap_seconds - .1)
        daemon._on_key('keyboard', ecodes.KEY_RIGHTMETA, 0)
        self.assertTrue(session.done.wait(5))
        self.assertEqual(self.insert.call_count, 2)
        self.copy.assert_not_called()

    def test_default_hotkey_tap_latches_repeat_is_ignored_and_next_press_stops(self):
        daemon = self.dictation_daemon()
        daemon._on_key('keyboard', ecodes.KEY_RIGHTMETA, 1)
        session = daemon.persistent
        daemon._on_key('keyboard', ecodes.KEY_RIGHTMETA, 2)
        daemon._on_key('keyboard', ecodes.KEY_RIGHTMETA, 1)  # duplicate down
        self.assertFalse(session.stopping.is_set())
        self.cfg.tap_seconds = 10  # keep the tap test independent of scheduler delays
        daemon._gesture = (session, time.monotonic())
        daemon._on_key('keyboard', ecodes.KEY_RIGHTMETA, 0)
        self.assertFalse(session.stopping.is_set())
        daemon._on_key('keyboard', ecodes.KEY_RIGHTMETA, 1)
        self.assertTrue(session.stopping.is_set())
        daemon._on_key('keyboard', ecodes.KEY_RIGHTMETA, 0)
        self.assertTrue(session.done.wait(5))
        self.assertIsNone(daemon._gesture)

    def test_default_hotkey_release_requires_original_device_and_disconnect_stops(self):
        daemon = self.dictation_daemon()
        daemon._on_key('keyboard', ecodes.KEY_RIGHTMETA, 1)
        session = daemon.persistent
        daemon._gesture = (session, time.monotonic() - 1)
        daemon._on_key('other', ecodes.KEY_RIGHTMETA, 0)
        self.assertFalse(session.stopping.is_set())
        daemon._on_device_lost('keyboard')
        self.assertTrue(session.stopping.is_set())
        self.assertIsNone(daemon._gesture)
        self.assertTrue(session.done.wait(5))

    def test_f9_chord_release_and_additional_toggle_use_same_engine(self):
        self.cfg.dictate_key = 'KEY_RIGHTALT+KEY_F9'
        self.cfg.dictate_toggle_key = 'KEY_F12'
        daemon = self.dictation_daemon()
        daemon._on_key('keyboard', ecodes.KEY_RIGHTALT, 1)
        daemon._on_key('keyboard', ecodes.KEY_F9, 1)
        session = daemon.persistent
        daemon._gesture = (session, time.monotonic() - 1)
        daemon._on_key('keyboard', ecodes.KEY_RIGHTALT, 0)
        self.assertTrue(session.done.wait(5))
        daemon._on_key('keyboard', ecodes.KEY_F9, 0)
        daemon._on_key('keyboard', ecodes.KEY_F12, 1)
        session = daemon.persistent
        self.assertIsNotNone(session)
        daemon._on_key('keyboard', ecodes.KEY_F12, 0)
        self.assertFalse(session.stopping.is_set())
        daemon._on_key('keyboard', ecodes.KEY_F12, 1)
        self.assertTrue(session.done.wait(5))

    def test_f11_toggles_and_release_or_escape_does_not_stop(self):
        daemon = Daemon(self.cfg, recorder_factory=ControlledRecorder, journal=self.pipeline.journal)
        daemon.pipeline = self.pipeline
        daemon.gate = Gate(self.tmp.name+'/lock')
        daemon.gate.open()
        daemon.vad, daemon.backend = self.vad, self.backend
        self.addCleanup(daemon.close)
        with patch('voicekey.daemon.target_mod.bind', return_value=self.binding):
            daemon._on_key('keyboard',ecodes.KEY_F11,1)
            session = daemon.persistent
            self.assertIsNotNone(session)
            daemon._on_key('keyboard',ecodes.KEY_F11,0)
            daemon._on_key('keyboard',ecodes.KEY_ESC,1)
            with patch('voicekey.daemon.notify') as notify:
                daemon._on_key('keyboard',ecodes.KEY_F10,1)
                self.assertEqual(notify.call_args.args[0], 'voicekey: busy')
                daemon._on_key('keyboard',ecodes.KEY_F10,0)
            self.assertFalse(session.stopping.is_set())
            daemon._on_key('keyboard',ecodes.KEY_F11,1)
            self.assertTrue(session.stopping.is_set())
            self.assertTrue(session.done.wait(5))

    def test_keyboard_loss_stops_persistent_capture_and_preserves_current_speech(self):
        daemon = Daemon(self.cfg, recorder_factory=ControlledRecorder, journal=self.pipeline.journal)
        daemon.pipeline = self.pipeline
        daemon.gate = Gate(self.tmp.name+'/lock')
        daemon.gate.open()
        daemon.vad, daemon.backend = self.vad, self.backend
        self.addCleanup(daemon.close)
        with patch('voicekey.daemon.target_mod.bind', return_value=self.binding):
            daemon._on_key('keyboard',ecodes.KEY_F11,1)
            session = daemon.persistent
            wait_for(lambda: session.ready.is_set())
            session.recorder.push(np.ones(1711,dtype=np.float32)*.2)
            daemon._on_device_lost('keyboard')
            self.assertTrue(session.done.wait(5))
        self.assertTrue(session.paused)
        self.assertEqual(session.reason,'keyboard disconnected')
        self.assertEqual(len(self.backend.transcribe.call_args.args[0]),1711)
        self.assertFalse(daemon.gate.held)
