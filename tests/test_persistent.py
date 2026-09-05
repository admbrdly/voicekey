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
        return self.session

    def speak(self):
        self.recorder.push(np.ones(5120, dtype=np.float32)*.2)
        self.recorder.push(np.zeros(5120, dtype=np.float32))

    def finish(self):
        self.session.request_stop()
        self.assertTrue(self.session.done.wait(5))

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
            session.recorder.push(np.ones(1711,dtype=np.float32)*.2)
            daemon._on_device_lost('keyboard')
            self.assertTrue(session.done.wait(5))
        self.assertTrue(session.paused)
        self.assertEqual(session.reason,'keyboard disconnected')
        self.assertEqual(len(self.backend.transcribe.call_args.args[0]),1711)
        self.assertFalse(daemon.gate.held)
