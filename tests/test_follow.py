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
from voicekey.target import EmacsTarget, NotifyPreview, Window, ImeTarget, WtypeTarget
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
        self.commit = Mock(return_value=True)
        self.cfg = Config()
        self.cfg.persistent.destination_policy = "follow"
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
        self.session = None

    def binding(self):
        d = self.destination
        if d.app_id == 'emacs':
            return EmacsTarget(NotifyPreview('dictate'), Window(d.id, True), d.app_id,
                               Mock(id=f'pin-{d.id}', pid=d.pid, valid=True, before=Mock(return_value=None)))
        ime = Mock(activation=Mock(return_value=1), before_cursor=Mock(return_value=None),
                   commit=self.commit)
        return ImeTarget(ime, 1, Window(d.id, True), d.app_id)

    def start(self):
        self.session = PersistentSession(self.cfg, self.pipeline, self.recorder, lambda: self.bind(),
            self.vad, Slot('test-vad'), None, device='keyboard', chord=frozenset(),
            watch_factory=ManualWatch if self.cfg.persistent.destination_policy != 'pin' else None)
        self.addCleanup(self.session.close)
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
        self.assertEqual(self.commit.call_count, 1)
        self.assertEqual(self.commit.call_args.args[0], 'Words.')
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
        self.commit.assert_not_called()
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
        self.assertEqual(self.commit.call_count, 1)
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
        self.assertEqual(self.commit.call_count, 1)
        self.assertEqual(self.session.target.target.window_id, 3)

    def test_no_focused_window_pauses_without_automatic_resume(self):
        self.start()
        self.audio(np.ones(1024, dtype=np.float32)*.2)
        self.destination = Focus()
        self.session.watcher.changed(self.destination)
        self.assertTrue(self.session.done.wait(3))
        self.assertEqual(self.session.reason, 'No text field detected')
        self.session.watcher.changed(Focus(3, 'browser', 300))
        self.assertFalse(self.recorder.active)
        self.commit.assert_not_called()
        self.type_text.assert_not_called()

    def test_brief_no_window_transition_in_follow_mode_keeps_listening(self):
        self.start()
        self.switch(Focus())
        self.switch(Focus(3, 'browser', 300))
        self.audio(np.ones(1024, dtype=np.float32)*.2)
        self.finish()
        self.assertFalse(self.session.paused)
        self.commit.assert_called_once()
        self.type_text.assert_not_called()

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
        wait_for(lambda: self.commit.call_count == 1)
        self.switch(Focus(2, 'browser', 200))
        self.audio(np.ones(5120, dtype=np.float32)*.5)
        self.finish()
        calls = self.polisher.polish.call_args_list
        self.assertEqual([c.kwargs['app_id'] for c in calls], ['terminal', 'browser'])
        self.assertNotIn('context', calls[1].kwargs)

    def test_default_pause_stops_capture_but_delivers_pending_text_to_emacs_pin(self):
        self.cfg.persistent.destination_policy = 'pause'
        self.destination = Focus(1, 'emacs', 100)
        entered, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)
        def transcribe(samples):
            entered.set()
            release.wait(2)
            return 'Pending words.'
        self.backend.transcribe.side_effect = transcribe
        self.start()
        self.audio(np.ones(5120, dtype=np.float32)*.2)
        self.audio(np.zeros(5120, dtype=np.float32))
        self.assertTrue(entered.wait(1))
        for identity in (2, 1):
            self.destination = Focus(identity, 'emacs', 100)
            self.session.watcher.changed(self.destination)
        self.assertTrue(self.recorder.finished)
        release.set()
        self.assertTrue(self.session.done.wait(3))
        self.assertEqual(self.session.reason, 'Window changed')
        self.assertTrue(self.session.paused)
        self.insert.assert_called_once()
        self.assertEqual(self.insert.call_args.args[:2], ('Pending words.', 'pin-1'))
        self.commit.assert_not_called()
        self.assertFalse((Path(self.tmp.name) / 'last-recovery.txt').exists())

    def test_pin_keeps_emacs_background_delivery(self):
        self.cfg.persistent.destination_policy = 'pin'
        self.destination = Focus(1, 'emacs', 100)
        self.start()
        self.destination = Focus(2, 'browser', 200)
        self.audio(np.ones(5120, dtype=np.float32)*.2)
        self.audio(np.zeros(5120, dtype=np.float32))
        wait_for(lambda: self.insert.call_count == 1)
        self.assertFalse(self.session.stopping.is_set())
        self.assertEqual(self.insert.call_args.args[1], 'pin-1')
        self.finish()

    def test_follow_into_unverified_browser_pauses_and_never_types(self):
        self.start()
        self.audio(np.ones(1024, dtype=np.float32)*.2)
        self.bind = lambda: WtypeTarget(NotifyPreview('dictate'), Window(2, True), 'browser')
        self.destination = Focus(2, 'browser', 200)
        self.session.watcher.changed(self.destination)
        self.assertTrue(self.session.done.wait(3))
        self.assertEqual(self.session.reason, 'No text field detected')
        self.type_text.assert_not_called()
        self.commit.assert_not_called()
        self.assertIn('Words.', (Path(self.tmp.name) / 'last-recovery.txt').read_text())

    def test_field_loss_inside_same_window_pauses_even_with_focus_watcher(self):
        self.start()
        self.audio(np.ones(1024, dtype=np.float32)*.2)
        self.session.target.target.ime.activation.return_value = None
        self.assertTrue(self.session.done.wait(3))
        self.assertEqual(self.session.reason, 'Text field is no longer active')
        self.type_text.assert_not_called()
        self.commit.assert_not_called()

    def test_pause_without_event_stream_also_checks_emacs_window(self):
        self.cfg.persistent.destination_policy = 'pause'
        self.destination = Focus(1, 'emacs', 100)
        self.start()
        self.session.watcher = None
        self.destination = Focus(2, 'browser', 200)
        self.assertTrue(self.session.done.wait(3))
        self.assertEqual(self.session.reason, 'Window changed')
        self.insert.assert_not_called()


class DestinationMonitorTests(unittest.TestCase):
    """Drive observations explicitly to test event order without thread timing."""
    def setUp(self):
        patch('voicekey.persistent.notify').start()
        self.addCleanup(patch.stopall)
        self.cfg = Config()
        self.cfg.persistent.destination_policy = 'follow'
        self.ime = Mock(activation=Mock(return_value=1))
        self.binding = ImeTarget(self.ime, 1, Window(7, True), 'browser')
        self.session = PersistentSession(self.cfg, Mock(), ControlledRecorder(), self.binding,
            Mock(), Mock(), None, device='test', chord=frozenset())
        self.addCleanup(self.session.close)
        self.session.target.target = self.binding
        self.session.focused = Focus(7, 'browser')
        self.session.watcher = ManualWatch(self.session.focus_changed, Mock())

    def test_transient_field_loss_is_cleared_and_sustained_loss_requires_two_polls(self):
        self.ime.activation.return_value = None
        self.session._check_destination()
        self.assertFalse(self.session.stopping.is_set())
        self.ime.activation.return_value = 1
        self.session._check_destination()
        self.ime.activation.return_value = None
        self.session._check_destination()
        self.assertFalse(self.session.stopping.is_set())
        self.session._check_destination()
        self.assertTrue(self.session.stopping.is_set())
        self.assertEqual(self.session.reason, 'Text field is no longer active')

    def test_ime_deactivation_before_focus_event_does_not_stop_following(self):
        self.ime.activation.return_value = None
        self.session._check_destination()
        self.session.focus_changed(Focus(8, 'browser'))
        self.session._check_destination()
        self.assertFalse(self.session.stopping.is_set())
        self.assertIsNone(self.session._destination_issue)

    def test_failed_delivery_waits_for_focus_event_instead_of_stopping_new_destination(self):
        self.session.target.failed.set()
        self.session._check_destination()
        self.assertFalse(self.session.stopping.is_set())
        self.session.focus_changed(Focus(8, 'browser'))
        self.session._check_destination()
        self.assertFalse(self.session.stopping.is_set())

    def test_event_arriving_during_second_field_check_invalidates_the_observation(self):
        target = self.session.target
        target.field_issue = Mock(return_value='Text field is no longer active')
        self.session._check_destination()
        def issue():
            self.session.focus_changed(Focus(8, 'browser'))
            return 'Text field is no longer active'
        target.field_issue.side_effect = issue
        self.session._check_destination()
        self.assertFalse(self.session.stopping.is_set())
        self.assertIsNone(self.session._destination_issue)

    def test_stable_delivery_failure_still_stops_capture(self):
        self.session.target.failed.set()
        self.session._check_destination()
        self.session._check_destination()
        self.assertEqual(self.session.reason, 'Delivery unavailable')

    def test_polling_is_at_most_once_per_second_and_one_unknown_reply_is_tolerated(self):
        self.session.policy = 'pause'
        self.session.watcher = None
        clock = [0.0]
        with patch('voicekey.persistent.time', Mock(monotonic=lambda: clock[0])), \
                patch('voicekey.persistent.focus.window_id', side_effect=[None, 7, 8]) as focused:
            self.session._check_destination()
            for tick in (.2, .4, .6, .8):
                clock[0] = tick
                self.session._check_destination()
            self.assertEqual(focused.call_count, 1)
            self.assertFalse(self.session.stopping.is_set())
            clock[0] = 1
            self.session._check_destination()
            self.assertFalse(self.session.stopping.is_set())
            clock[0] = 2
            self.session._check_destination()
        self.assertEqual(focused.call_count, 3)
        self.assertEqual(self.session.reason, 'Window changed')

    def test_repeated_unknown_focus_stops_with_a_tracking_error(self):
        self.session.policy = 'pause'
        self.session.watcher = None
        clock = [0.0]
        with patch('voicekey.persistent.time', Mock(monotonic=lambda: clock[0])), \
                patch('voicekey.persistent.focus.window_id', return_value=None):
            self.session._check_destination()
            self.assertFalse(self.session.stopping.is_set())
            clock[0] = 1
            self.session._check_destination()
        self.assertEqual(self.session.reason, 'Window tracking unavailable')

    def test_acknowledged_emacs_buffer_without_window_identity_is_not_rejected(self):
        self.session.policy = 'pause'
        self.session.watcher = None
        self.session.target.target = EmacsTarget(NotifyPreview('dictate'), Window(None, False), 'emacs',
            Mock(id='pin', valid=True))
        with patch('voicekey.persistent.focus.window_id') as focused:
            self.session._check_destination()
            self.session._check_destination()
        focused.assert_not_called()
        self.assertFalse(self.session.stopping.is_set())


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
