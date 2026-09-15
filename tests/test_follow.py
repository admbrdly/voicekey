import json
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import numpy as np

from voicekey.config import Config
from voicekey.focus import Focus
from voicekey.follow import NiriFocusWatch
from voicekey.persistent import PersistentSession
from voicekey.pipeline import Pipeline
from voicekey.recorder import SAMPLE_RATE
from voicekey.recovery import Journal
from voicekey.target import EmacsTarget, NotifyPreview, Window, WtypeTarget
from voicekey.work import Slot
from tests.test_persistent import ControlledRecorder
from tests.test_pipeline import wait_for


class ManualWatch:
    def __init__(self, changed, failed):
        self.changed, self.failed = changed, failed

    def close(self):
        pass


class FollowTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.addCleanup(patch.stopall)
        for module in ('persistent', 'pipeline', 'target'):
            patch(f'voicekey.{module}.notify').start()
        self.insert = patch('voicekey.session_target.emacs.insert').start()
        self.unpin = patch('voicekey.session_target.emacs.unpin').start()
        self.type_text = patch('voicekey.session_target.inject.type_text').start()
        self.cfg = Config()
        self.cfg.persistent.pause_seconds = .2
        self.cfg.persistent.pre_roll_seconds = .05
        self.cfg.persistent.silence_seconds = 20
        self.cfg.persistent.max_utterance_seconds = 2
        self.cfg.pipeline.shutdown_seconds = 2
        self.recorder = ControlledRecorder()
        self.backend = Mock(transcribe=Mock(return_value='Words.'))
        self.polisher = None
        self.pipeline = Pipeline(self.cfg, backend=lambda: self.backend, polisher=lambda: self.polisher,
                                 journal=Journal(self.tmp.name + '/sessions'))
        self.pipeline.start()
        self.addCleanup(self.pipeline.close)
        self.destination = Focus(1, 'terminal', 100)
        patch('voicekey.target.focus.window_id', side_effect=lambda **kw: self.destination.id).start()
        self.vad = Mock(speech=lambda samples: bool(np.max(np.abs(samples)) > .01))
        self.bind = self.binding
        self.session = PersistentSession(self.cfg, self.pipeline, self.recorder, lambda: self.bind(),
            self.vad, Slot('test-vad'), None, device='keyboard', chord=frozenset(), watch_factory=ManualWatch)
        self.addCleanup(self.session.close)

    def binding(self):
        d = self.destination
        if d.app_id == 'emacs':
            return EmacsTarget(NotifyPreview('dictate'), Window(d.id, True), d.app_id,
                               Mock(id=f'pin-{d.id}', pid=d.pid, valid=True, before=Mock(return_value=None)))
        return WtypeTarget(NotifyPreview('dictate'), Window(d.id, True), d.app_id)

    def start(self):
        self.assertTrue(self.session.start())
        wait_for(self.session.ready.is_set)

    def audio(self, samples):
        self.recorder.push(samples)

    def switch(self, destination):
        self.destination = destination
        self.session.watcher.changed(destination)
        wait_for(lambda: self.session.target.target.window_id == destination.id)

    def finish(self):
        self.session.request_stop()
        self.assertTrue(self.session.done.wait(5))

    def test_mid_speech_focus_change_preserves_exact_audio_and_rebinds(self):
        self.start()
        a = np.linspace(.1, .2, 1637, dtype=np.float32)  # partial VAD window
        b = np.linspace(.5, .6, 2071, dtype=np.float32)
        self.audio(a)
        self.switch(Focus(2, 'browser', 200))
        self.audio(b)
        self.finish()
        parts = [c.args[0] for c in self.backend.transcribe.call_args_list]
        self.assertEqual(len(parts), 2)
        np.testing.assert_array_equal(parts[0], a)
        np.testing.assert_array_equal(parts[1], b)
        self.assertEqual(self.type_text.call_count, 1)
        self.assertEqual(self.type_text.call_args.args[0], 'Words.')
        changes = [json.loads(line) for line in (Path(self.tmp.name)/'sessions'/f'{self.session.id}.jsonl').read_text().splitlines()]
        event = next(e for e in changes if e['event'] == 'destination-changed')
        self.assertEqual(event['sample'], len(a))
        self.assertTrue(list((Path(self.tmp.name)/'sessions').glob('*.txt')))

    def test_pending_terminal_text_never_follows_focus_or_returns_to_reactivated_window(self):
        entered, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)
        def transcribe(samples):
            entered.set()
            release.wait(2)
            return 'Old terminal.'
        self.backend.transcribe.side_effect = transcribe
        self.start()
        self.audio(np.ones(5120, dtype=np.float32)*.2)
        self.audio(np.zeros(5120, dtype=np.float32))
        self.assertTrue(entered.wait(1))
        self.switch(Focus(2, 'terminal', 100))
        self.switch(Focus(1, 'terminal', 100))
        release.set()
        self.finish()
        self.type_text.assert_not_called()
        saved = '\n'.join(p.read_text() for p in (Path(self.tmp.name)/'sessions').glob('*.txt'))
        self.assertIn('Old terminal.', saved)

    def test_emacs_pending_text_keeps_old_pin_new_speech_uses_new_app(self):
        self.destination = Focus(1, 'emacs', 100)
        self.start()
        self.audio(np.ones(2048, dtype=np.float32)*.2)
        self.switch(Focus(2, 'terminal', 200))
        self.audio(np.ones(2048, dtype=np.float32)*.5)
        self.finish()
        self.assertEqual(self.insert.call_count, 1)
        self.assertEqual(self.insert.call_args.args[1], 'pin-1')
        self.assertEqual(self.type_text.call_count, 1)
        self.unpin.assert_any_call('pin-1')

    def test_focus_change_during_bind_never_relabels_audio_as_later_window(self):
        self.start()
        original = self.bind
        def bind():
            if self.destination.id == 2:
                candidate = original()
                self.destination = Focus(3, 'browser', 300)
                self.session.watcher.changed(self.destination)
                return candidate
            return original()
        self.bind = bind
        self.audio(np.ones(1024, dtype=np.float32)*.2)
        self.destination = Focus(2, 'terminal', 200)
        self.session.watcher.changed(self.destination)
        wait_for(lambda: self.session.target.target.window_id == 3)
        self.audio(np.ones(1024, dtype=np.float32)*.5)
        self.finish()
        self.assertEqual(self.type_text.call_count, 1)
        self.assertEqual(self.session.target.target.window_id, 3)

    def test_no_focused_window_is_recovery_only_and_next_window_can_resume(self):
        self.start()
        self.switch(Focus())
        self.audio(np.ones(1024, dtype=np.float32)*.2)
        self.switch(Focus(3, 'browser', 300))
        self.audio(np.ones(1024, dtype=np.float32)*.5)
        self.finish()
        self.assertEqual(self.type_text.call_count, 1)
        self.assertEqual(self.backend.transcribe.call_count, 2)

    def test_tracker_failure_stops_capture_and_preserves_audio(self):
        self.start()
        self.audio(np.ones(1024, dtype=np.float32)*.2)
        self.session.watcher.failed('disconnected')
        self.assertTrue(self.session.done.wait(5))
        self.assertTrue(self.session.paused)
        self.assertIn('focus tracking lost', self.session.reason)
        self.assertEqual(self.backend.transcribe.call_count, 1)

    def test_silent_switch_creates_no_transcript_and_reclaims_old_targets(self):
        self.start()
        for identity in range(2, 12):
            self.switch(Focus(identity, 'terminal', 100))
        self.finish()
        self.backend.transcribe.assert_not_called()
        self.assertLessEqual(len(self.session.targets), 2)

    def test_app_style_and_polish_context_follow_destination(self):
        self.polisher = Mock(polish=Mock(side_effect=lambda text, *a, **kw: text))
        self.start()
        self.audio(np.ones(5120, dtype=np.float32)*.2)
        self.audio(np.zeros(5120, dtype=np.float32))
        wait_for(lambda: self.type_text.call_count == 1)
        self.switch(Focus(2, 'browser', 200))
        self.audio(np.ones(5120, dtype=np.float32)*.5)
        self.finish()
        calls = self.polisher.polish.call_args_list
        self.assertEqual([c.kwargs['app_id'] for c in calls], ['terminal', 'browser'])
        self.assertNotIn('context', calls[1].kwargs)


class NiriEventsTests(unittest.TestCase):
    def test_initial_state_focus_creation_and_closure(self):
        watch = NiriFocusWatch.__new__(NiriFocusWatch)
        watch.windows, watch.current, watch.initialized = {}, Focus(), False
        watch.changed = Mock()
        watch.event({'WindowsChanged': {'windows': [
            {'id': 1, 'app_id': 'terminal', 'pid': 2, 'is_focused': True},
            {'id': 2, 'app_id': 'emacs', 'pid': 3, 'is_focused': False}]}})
        watch.event({'WindowFocusChanged': {'id': 2}})
        watch.event({'WindowOpenedOrChanged': {'window':
            {'id': 3, 'app_id': 'browser', 'pid': 4, 'is_focused': True}}})
        watch.event({'WindowClosed': {'id': 3}})
        self.assertEqual([c.args[0].id for c in watch.changed.call_args_list], [1, 2, 3, None])
        watch.event({'WindowFocusTimestampChanged': {'id': 1}})
        self.assertEqual(watch.changed.call_count, 4)
