"""Drafts use a fake editor and microphone; no access to the active desktop."""
import tempfile
import threading
import time
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import numpy as np
from evdev import ecodes

from voicekey import emacs, nvim
from voicekey.config import Config
from voicekey.daemon import Daemon
from voicekey.focus import Focus
from voicekey.gate import Gate
from voicekey.history import latest
from voicekey.recovery import Journal
from voicekey.target import PinnedEditorTarget, NotifyPreview, Window, Landing, Outcome, WtypeTarget, ImeTarget, RefusedTarget
from tests.test_persistent import ControlledRecorder
from tests.test_ime import _offline_input_method
from tests.test_pipeline import wait_for


class Editor(PinnedEditorTarget):
    kind = 'editor'

    def __init__(self):
        super().__init__(NotifyPreview('dictate'), Window(7, True), 'editor')
        self.pinning = SimpleNamespace(valid=True, before=lambda wait: '', reason='')
        self.previews, self.inserted = [], []
        self.unpinned = False

    def show_draft(self, text, *, timeout=.25):
        self.previews.append(text)

    def insert_pinned(self, text, deadline, operation, prefix, permit, cancelled):
        if cancelled.is_set() or not Path(permit).exists():
            return Landing(reason='cancelled')
        self.inserted.append(text)
        return Landing(Outcome.CONFIRMED)

    def unpin(self):
        self.unpinned = True


class DraftTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.addCleanup(patch.stopall)
        for name in ('daemon', 'persistent', 'pipeline', 'target'):
            patch(f'voicekey.{name}.notify').start()
        patch('voicekey.daemon.focus.compositor', return_value='none').start()
        patch('voicekey.persistent.focus.window_id', return_value=7).start()
        self.editor = Editor()
        patch('voicekey.daemon.target_mod.bind', return_value=self.editor).start()
        cfg = Config()
        cfg.persistent.draft = True
        cfg.persistent.pause_seconds = .2
        cfg.persistent.pre_roll_seconds = .05
        cfg.persistent.max_utterance_seconds = 2
        cfg.pipeline.shutdown_seconds = 2
        self.d = Daemon(cfg, recorder_factory=ControlledRecorder, journal=Journal(self.tmp.name + '/sessions'))
        self.d.gate = Gate(self.tmp.name + '/gate')
        self.d.backend = Mock(transcribe=Mock(return_value='Some words.'))
        self.d.vad = Mock(speech=lambda samples: bool(np.max(np.abs(samples)) > .01))
        self.d.start_workers()
        self.addCleanup(self.d.close)

    def start(self):
        self.d.command('start')
        self.s = self.d.persistent
        wait_for(lambda: self.s.ready.is_set() or self.s.done.is_set())
        return self.s

    def speak(self, text='Some words.'):
        self.d.backend.transcribe.return_value = text
        sequence = self.s.target._through
        self.s.recorder.push(np.ones(5120, dtype=np.float32) * .2)
        self.s.recorder.push(np.zeros(5120, dtype=np.float32))
        wait_for(lambda: self.s.target._through > sequence or self.s.done.is_set())

    def key(self, code):
        self.d._on_key('test', code, 1)
        self.d._on_key('test', code, 0)
        thread = self.s._hotkey_thread
        if thread is not None:
            thread.join(1)

    def finished(self):
        self.assertTrue(self.s.done.wait(5))
        self.s.thread.join(1)
        self.d._on_tick()
        self.assertIsNone(self.d.persistent)

    def test_cleans_each_chunk_with_draft_context_and_inserts_only_on_acceptance(self):
        self.d.polisher = Mock(polish=Mock(side_effect=['The claim.', 'That we discussed.']))
        self.start()
        self.speak('the claim')
        self.speak('that we discussed')
        call = self.d.polisher.polish.call_args
        self.assertEqual(call.args[0], 'that we discussed')
        self.assertEqual(call.kwargs['context'], 'The claim.')
        self.assertEqual(self.s.target.text, 'The claim. That we discussed.')
        self.s.target.render()
        self.assertEqual(self.editor.previews[-1], 'The claim. That we discussed.')
        self.assertEqual(self.editor.inserted, [])
        self.key(ecodes.KEY_RIGHTMETA)
        self.finished()
        self.assertEqual(self.editor.inserted, ['The claim. That we discussed.'])
        self.assertTrue(self.editor.unpinned)
        self.assertEqual(latest(self.d.pipeline.journal)['final'], self.editor.inserted[0])
        self.assertEqual(list(self.d.pipeline.journal.directory.glob('*.wav')), [])
        self.assertEqual(list(self.d.pipeline.journal.directory.glob('*.recovery.txt')), [])

    def test_escape_discards_the_entire_draft_and_allows_a_new_session(self):
        self.start()
        self.speak('First sentence.')
        self.speak('Second sentence.')
        self.key(ecodes.KEY_ESC)
        self.finished()
        self.assertEqual(self.editor.inserted, [])
        self.assertTrue(self.editor.unpinned)
        self.assertEqual(list(self.d.pipeline.journal.directory.glob('*.wav')), [])
        # An accidental Escape is recoverable from history and the recovery file.
        self.assertEqual(latest(self.d.pipeline.journal)['final'], 'First sentence. Second sentence.')
        backup = self.d.pipeline.journal.directory.parent / 'last-recovery.txt'
        self.assertEqual(backup.read_text(), 'First sentence. Second sentence.\n')
        self.assertIn('--copy-last', self.s.reason)
        self.start()
        self.speak('Next session.')
        self.d.command('stop')
        self.finished()
        self.assertEqual(self.editor.inserted, ['Next session.'])

    def test_accepted_draft_does_not_overwrite_the_recovery_backup(self):
        backup = self.d.pipeline.journal.directory.parent / 'last-recovery.txt'
        self.start()
        self.speak()
        self.d.command('stop')
        self.finished()
        self.assertFalse(backup.exists())

    def test_cancel_while_model_is_running_prevents_late_insertion(self):
        entered, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)
        def polish(text, *args, **kwargs):
            entered.set()
            release.wait(3)
            return 'Late revision.'
        self.d.polisher = Mock(polish=polish)
        self.start()
        self.s.recorder.push(np.ones(5120, dtype=np.float32) * .2)
        self.s.recorder.push(np.zeros(5120, dtype=np.float32))
        self.assertTrue(entered.wait(2))
        self.key(ecodes.KEY_ESC)
        wait_for(lambda: self.editor.unpinned)
        self.assertEqual(self.editor.inserted, [])
        release.set()
        self.finished()
        self.assertEqual(self.editor.inserted, [])

    def test_automatic_stop_waits_for_explicit_acceptance_at_original_editor(self):
        self.start()
        self.speak()
        self.s.focus_changed(Focus(8, 'other'))
        wait_for(self.s.draft_waiting.is_set)
        self.assertFalse(self.s.recorder.active)
        self.assertEqual(self.d.status()['state'], 'draft')
        self.assertFalse(self.d.status()['listening'])
        with self.assertRaisesRegex(ValueError, 'before changing draft mode'):
            self.d.command('draft-off')
        self.assertEqual(self.editor.inserted, [])
        self.key(ecodes.KEY_RIGHTMETA)
        self.finished()
        self.assertEqual(self.editor.inserted, ['Some words.'])

    def test_free_memory_preserves_draft_which_can_be_accepted_without_models(self):
        self.start()
        self.speak()
        self.d.command('free-memory')
        wait_for(self.s.draft_waiting.is_set)
        self.d._on_tick()
        wait_for(lambda: self.d.model_state == 'unloaded')
        self.assertIsNone(self.s.vad)
        self.assertIsNone(self.s.streaming)
        self.assertEqual(self.editor.inserted, [])
        self.d.command('stop')
        self.finished()
        self.assertEqual(self.editor.inserted, ['Some words.'])

    def test_shutdown_preserves_whole_draft_without_duplicate_revisions(self):
        self.start()
        self.speak('First.')
        self.speak('Second.')
        identity = self.s.id
        self.d.close()
        self.assertEqual(self.editor.inserted, [])
        recovered = self.d.pipeline.journal.path(identity, '.recovery.txt').read_text()
        self.assertEqual(recovered, 'First. Second.\n')

    def ordinary_speech(self):
        self.s.recorder.push(np.ones(5120, dtype=np.float32) * .2)
        self.s.recorder.push(np.zeros(5120, dtype=np.float32))

    def blocked_start(self, target, *, key=False):
        entered, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)
        def bind(*args, **kwargs):
            entered.set()
            if not release.wait(4):
                raise TimeoutError('test binding was not released')
            return target
        patch('voicekey.daemon.target_mod.bind', side_effect=bind).start()
        if key:
            self.d._on_key('test', ecodes.KEY_RIGHTMETA, 1)
        else:
            self.d.command('start')
        self.s = self.d.persistent
        self.assertTrue(entered.wait(1))
        self.assertFalse(self.d.status()['draft_mode'])
        return release

    def browser(self):
        ime = _offline_input_method()
        self.addCleanup(ime._close_pipe)
        ime._post = lambda fn: (fn() or True)
        ime._on_activate(None)
        ime._on_done(None)
        return ImeTarget(ime, 1, Window(7, True), 'browser')

    def test_browser_fallback_commits_chunks_and_keeps_preference_and_preview(self):
        target = self.browser()
        with patch('voicekey.daemon.target_mod.bind', return_value=target):
            self.start()
        self.assertFalse(self.s.draft)
        self.assertTrue(self.d.status()['draft_enabled'])
        self.assertFalse(self.d.status()['draft_mode'])
        self.assertEqual(self.s.targets, [self.s.target])
        self.assertIs(self.s.current.target.session, self.s.target)
        self.assertFalse(target.preview.closed)
        self.ordinary_speech()
        wait_for(lambda: any(c[0] == 'commit_string' for c in target.ime._im.calls))
        self.d.command('stop')
        self.finished()
        self.assertIn(('commit_string', 'Some words.'), target.ime._im.calls)
        self.start()
        self.assertTrue(self.s.draft)
        self.d.command('cancel')
        self.finished()

    def test_terminal_fallback_keeps_normal_refusal_without_typing_permission(self):
        target = WtypeTarget(NotifyPreview('dictate'), Window(7, True), 'terminal')
        with patch('voicekey.daemon.target_mod.bind', return_value=target), \
                patch('voicekey.inject.type_text') as typing:
            self.start()
            self.finished()
        self.assertFalse(self.s.draft)
        self.assertIn('No text field detected', self.s.reason)
        self.assertTrue(self.s.typing_fallback)
        typing.assert_not_called()

    def test_explicit_typing_uses_ordinary_mode_and_pause_policy(self):
        self.d.cfg.persistent.destination_policy = 'follow'
        target = WtypeTarget(NotifyPreview('dictate'), Window(7, True), 'terminal')
        with patch('voicekey.daemon.target_mod.bind', return_value=target), \
                patch('voicekey.inject.type_text') as typing:
            self.d.command('start-typing')
            self.s = self.d.persistent
            wait_for(self.s.ready.is_set)
            self.assertFalse(self.s.draft)
            self.assertEqual(self.s.policy, 'pause')
            self.ordinary_speech()
            wait_for(lambda: typing.called)
            self.d.command('stop')
            self.finished()
            self.assertEqual(typing.call_args.args[0], 'Some words.')

    def test_capability_refusal_preserves_editor_binding_and_ordinary_preview(self):
        self.editor.show_draft = Mock(side_effect=emacs.EmacsRefused('drafts require an editable text buffer'))
        self.editor.preview = self.browser().preview
        self.d.cfg.persistent.destination_policy = 'pin'
        watch = Mock()
        with patch('voicekey.daemon.NiriFocusWatch', watch), \
                patch('voicekey.daemon.focus.compositor', return_value='niri'):
            self.start()
        self.assertFalse(self.s.draft)
        self.assertEqual(self.s.policy, 'pin')
        watch.assert_not_called()
        self.assertFalse(self.editor.preview.closed)
        self.ordinary_speech()
        wait_for(lambda: bool(self.editor.inserted))
        self.d.command('stop')
        self.finished()
        self.assertEqual(self.editor.inserted, ['Some words.'])

    def test_fallback_read_only_editor_still_refuses_insertion(self):
        self.editor.show_draft = Mock(side_effect=emacs.EmacsRefused('drafts require an editable text buffer'))
        self.editor.insert_pinned = Mock(return_value=Landing(reason='buffer is read-only'))
        self.start()
        self.ordinary_speech()
        wait_for(lambda: self.editor.insert_pinned.called)
        self.d.command('stop')
        self.finished()
        self.assertTrue(self.d.pipeline.journal.path(self.s.id, '.recovery.txt').exists())
        self.assertEqual(self.editor.inserted, [])

    def test_browser_fallback_restores_follow_policy_and_watcher(self):
        self.d.cfg.persistent.destination_policy = 'follow'
        watch = Mock()
        with patch('voicekey.daemon.target_mod.bind', return_value=self.browser()), \
                patch('voicekey.daemon.focus.compositor', return_value='niri'), \
                patch('voicekey.daemon.NiriFocusWatch', watch):
            self.start()
        self.assertEqual(self.s.policy, 'follow')
        watch.assert_called_once()
        self.assertIs(self.s.watcher, watch.return_value)
        self.d.command('stop')
        self.finished()

    def test_unknown_original_window_uses_ordinary_editor_delivery(self):
        self.editor.window.id = None
        self.start()
        self.assertFalse(self.s.draft)
        self.assertIn('Window tracking unavailable', self.s.tracking_notice)
        self.assertEqual(self.editor.previews, [])
        self.ordinary_speech()
        wait_for(lambda: bool(self.editor.inserted))
        self.d.command('stop')
        self.finished()
        self.assertEqual(self.editor.inserted, ['Some words.'])

    def test_hold_release_during_binding_stops_fallback_and_delivers_opening_speech(self):
        target = self.browser()
        release = self.blocked_start(target, key=True)
        self.ordinary_speech()
        self.d._gesture = (self.s, time.monotonic() - self.d.cfg.tap_seconds - 1)
        self.d._on_key('test', ecodes.KEY_RIGHTMETA, 0)
        release.set()
        self.finished()
        self.assertEqual(self.s.reason, 'key released')
        self.assertIn(('commit_string', 'Some words.'), target.ime._im.calls)

    def test_tap_release_during_binding_leaves_fallback_listening(self):
        release = self.blocked_start(self.browser(), key=True)
        self.d.cfg.tap_seconds = 10
        self.d._on_key('test', ecodes.KEY_RIGHTMETA, 0)
        release.set()
        wait_for(self.s.ready.is_set)
        self.assertFalse(self.s.stopping.is_set())
        self.assertEqual(self.s.stop_instruction, 'press a dictation key to stop')
        self.d.command('stop')
        self.finished()

    def test_hold_release_during_binding_does_not_accept_supported_draft(self):
        release = self.blocked_start(self.editor, key=True)
        self.d._gesture = (self.s, time.monotonic() - self.d.cfg.tap_seconds - 1)
        self.d._on_key('test', ecodes.KEY_RIGHTMETA, 0)
        release.set()
        wait_for(self.s.ready.is_set)
        self.assertTrue(self.s.draft)
        self.assertFalse(self.s.stopping.is_set())
        self.d.command('cancel')
        self.finished()

    def test_stop_during_binding_restores_ordinary_drain_deadline_and_speech(self):
        target = self.browser()
        release = self.blocked_start(target)
        self.ordinary_speech()
        self.d.command('stop')
        self.assertTrue(self.s.recorder.finished)
        deadline = self.s.deadline
        release.set()
        self.finished()
        self.assertEqual(self.s._drain_deadline, deadline)
        self.assertIn(('commit_string', 'Some words.'), target.ime._im.calls)

    def test_cancel_during_binding_never_inserts_fallback_speech(self):
        target = self.browser()
        release = self.blocked_start(target)
        self.ordinary_speech()
        self.d.command('cancel')
        release.set()
        self.finished()
        self.assertFalse(any(c[0] == 'commit_string' for c in target.ime._im.calls))
        self.assertFalse(self.s.recorder.active)

    def test_invalid_editor_pin_keeps_opening_speech_without_terminal_typing(self):
        self.editor.pinning.valid = False
        self.editor.pinning.reason = 'pin acknowledgement timed out'
        release = self.blocked_start(self.editor)
        self.ordinary_speech()
        with patch('voicekey.inject.type_text') as typing:
            release.set()
            self.finished()
            typing.assert_not_called()
        self.assertTrue(self.s.draft)
        self.assertEqual(self.editor.inserted, [])
        self.assertTrue(self.d.pipeline.journal.path(self.s.id, '.recovery.txt').exists())

    def test_refused_terminal_editor_binding_never_becomes_typing(self):
        target = RefusedTarget(Window(7, True), 'terminal', 'Neovim binding uncertain')
        release = self.blocked_start(target)
        self.ordinary_speech()
        with patch('voicekey.inject.type_text') as typing:
            release.set()
            self.finished()
            typing.assert_not_called()
        self.assertIn('Neovim binding uncertain', self.s.reason)
        self.assertFalse(self.s.typing_fallback)

    def test_preview_initialization_timeout_preserves_speech_without_downgrade(self):
        self.editor.show_draft = Mock(side_effect=emacs.EmacsTimeout('preview timed out'))
        release = self.blocked_start(self.editor)
        self.ordinary_speech()
        release.set()
        self.finished()
        self.assertTrue(self.s.draft)
        self.assertEqual(self.editor.inserted, [])
        self.assertTrue(self.d.pipeline.journal.path(self.s.id, '.recovery.txt').exists())

    def test_long_key_press_still_toggles_in_draft_mode(self):
        self.d._on_key('test', ecodes.KEY_RIGHTMETA, 1)
        self.s = self.d.persistent
        wait_for(self.s.ready.is_set)
        self.assertIsNotNone(self.d._gesture)
        self.d._on_key('test', ecodes.KEY_RIGHTMETA, 0)
        self.assertFalse(self.s.stopping.is_set())
        self.d.command('cancel')
        self.finished()

    def test_controller_ticks_during_binding_do_not_render_an_unbound_draft(self):
        entered, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)
        def bind(*args, **kwargs):
            entered.set()
            release.wait(2)
            return self.editor
        with patch('voicekey.daemon.target_mod.bind', side_effect=bind):
            self.d.command('start')
            self.s = self.d.persistent
            self.assertTrue(entered.wait(1))
            self.d._on_tick()
            self.assertEqual(self.editor.previews, [])
            release.set()
            wait_for(self.s.ready.is_set)
        self.assertFalse(self.s.stopping.is_set())
        self.d.command('cancel')
        self.finished()

    def test_hooks_and_word_overrides_apply_once_to_the_accepted_draft(self):
        self.d.cfg.text.word_overrides = {'words': 'sentences'}
        self.d.cfg.dictation.post_transcription_hook = "sed 's/^/Edited: /'"
        self.start()
        self.speak()
        self.assertEqual(self.s.target.text, 'Some words.')
        self.d.command('stop')
        self.finished()
        self.assertEqual(self.editor.inserted, ['Edited: Some sentences.'])

    def test_draft_limit_preserves_overflow_without_inserting_a_partial_draft(self):
        self.start()
        self.speak('First.')
        with patch('voicekey.draft.MAX_DRAFT_BYTES', 10):
            self.s.recorder.push(np.ones(5120, dtype=np.float32) * .2)
            self.s.recorder.push(np.zeros(5120, dtype=np.float32))
            self.finished()
        self.assertEqual(self.editor.inserted, [])
        self.assertTrue(self.d.pipeline.journal.path(self.s.id, '.recovery.txt').exists())

    def test_custom_cancel_chord_does_not_cancel_on_escape_alone(self):
        self.d.actions.pop(frozenset({ecodes.KEY_ESC}))
        self.d.actions[frozenset({ecodes.KEY_LEFTCTRL, ecodes.KEY_ESC})] = ('draft-cancel', 'hold')
        self.start()
        self.speak()
        self.key(ecodes.KEY_ESC)
        self.assertFalse(self.s.stopping.is_set())
        self.d._on_key('test', ecodes.KEY_LEFTCTRL, 1)
        self.key(ecodes.KEY_ESC)
        self.d._on_key('test', ecodes.KEY_LEFTCTRL, 0)
        self.finished()
        self.assertEqual(self.editor.inserted, [])

    def test_earlier_text_is_never_revised(self):
        self.d.polisher = Mock(polish=Mock(side_effect=lambda text, *args, **kwargs: text.lower()))
        self.start()
        self.speak('First three words.')
        self.speak('New passage.')
        self.speak('Continues here.')
        self.assertEqual([c.args[0] for c in self.d.polisher.polish.call_args_list],
                         ['First three words.', 'New passage.', 'Continues here.'])
        self.assertEqual(self.d.polisher.polish.call_args.kwargs['context'],
                         'first three words. new passage.')
        self.assertEqual(self.s.target.text, 'first three words. new passage. continues here.')
        self.d.command('stop')
        self.finished()

    def test_model_failure_appends_the_new_chunk_raw(self):
        self.d.polisher = Mock(polish=Mock(side_effect=['First.', RuntimeError('unavailable')]))
        self.start()
        self.speak('First raw.')
        self.speak('Next raw.')
        self.assertEqual(self.s.target.text, 'First. Next raw.')
        self.d.command('stop')
        self.finished()
        self.assertEqual(self.editor.inserted, ['First. Next raw.'])

    def test_cleanup_budget_after_acceptance_appends_later_chunks_raw(self):
        self.d.polisher = Mock(polish=Mock(side_effect=lambda text, *args, **kwargs: text.upper()))
        self.start()
        self.speak('One.')
        self.s.target.hurry(0)
        self.speak('Two.')
        self.assertEqual(self.d.polisher.polish.call_count, 1)
        self.assertEqual(self.s.target.text, 'ONE. Two.')
        self.d.command('stop')
        self.finished()
        self.assertEqual(self.editor.inserted, ['ONE. Two.'])

    def test_stage_exception_fails_the_draft_and_recovers_the_missing_chunk(self):
        self.start()
        self.speak('First.')
        with patch.object(self.s.target, 'prepare', side_effect=RuntimeError('unexpected')), \
                patch.object(self.d.pipeline, 'notify'):
            self.d.backend.transcribe.return_value = 'Lost chunk.'
            self.s.recorder.push(np.ones(5120, dtype=np.float32) * .2)
            self.s.recorder.push(np.zeros(5120, dtype=np.float32))
            wait_for(self.s.target.failed.is_set)
        self.assertEqual(self.s.target.text, 'First.')
        self.d.command('stop')
        self.finished()
        self.assertEqual(self.editor.inserted, [])
        recovered = self.d.pipeline.journal.path(self.s.id, '.recovery.txt').read_text()
        self.assertIn('First.', recovered)
        self.assertIn('Lost chunk.', recovered)

    def test_failed_draft_never_claims_later_chunks(self):
        self.start()
        self.speak('First.')
        self.s.target.failed.set()
        through = self.s.target._through
        job = SimpleNamespace(id='x', raw='um', failure='', target=self.s.target)
        with patch.object(self.s.target.ledger, 'get', return_value=SimpleNamespace(sequence=through + 1)):
            self.s.target.prepare(job, self.d.pipeline)
        self.assertEqual(self.s.target._through, through)
        self.d.command('cancel')
        self.finished()

    def test_discard_revokes_an_editor_request_already_queued_by_acceptance(self):
        entered, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)
        def insert(text, deadline, operation, prefix, permit, cancelled):
            entered.set()
            release.wait(3)
            # The editor, not Python, performs the final check before mutation.
            if not Path(permit).exists():
                return Landing(reason='insertion cancelled')
            self.editor.inserted.append(text)
            return Landing(Outcome.CONFIRMED)
        self.editor.insert_pinned = insert
        self.start()
        self.speak()
        self.d.command('stop')
        self.assertTrue(entered.wait(3))
        self.d.command('cancel')
        release.set()
        self.finished()
        self.assertEqual(self.editor.inserted, [])

    def test_discard_just_before_permit_creation_cannot_insert(self):
        self.start()
        self.speak()
        target, journal = self.s.target, self.d.pipeline.journal
        def insert(text, deadline, operation, prefix, permit, cancelled):
            # Model a request already handed to the editor: only the permit counts.
            if not Path(permit).exists():
                return Landing(reason='insertion cancelled')
            self.editor.inserted.append(text)
            return Landing(Outcome.CONFIRMED)
        self.editor.insert_pinned = insert
        append = journal.append
        def racing_append(identity, event, **data):
            result = append(identity, event, **data)
            if event == 'delivery-attempt':
                target.cancelled.set()  # discard lands after the pre-permit check
            return result
        with patch.object(journal, 'append', side_effect=racing_append):
            self.d.command('stop')
            self.finished()
        self.assertEqual(self.editor.inserted, [])
        self.assertFalse(journal.path(self.s.id, '.permit').exists())

    def test_backlogged_chunk_past_its_cleanup_deadline_is_not_sent_to_the_model(self):
        self.d.polisher = Mock(polish=Mock(side_effect=lambda text, *args, **kwargs: text.upper()))
        self.start()
        real = self.s.target.prepare
        def backlogged(job, pipeline):
            # The chunk waited in the queue past its ordinary cleanup deadline.
            return real(replace(job, polish_deadline=time.monotonic() - 1), pipeline)
        with patch.object(self.s.target, 'prepare', side_effect=backlogged):
            self.speak('Late chunk.')
        self.d.polisher.polish.assert_not_called()
        self.assertEqual(self.s.target.text, 'Late chunk.')
        self.d.command('cancel')
        self.finished()

    def test_recordings_dir_keeps_draft_chunks(self):
        self.d.cfg.recordings_dir = self.tmp.name + '/corpus'
        self.d.polisher = Mock(polish=Mock(return_value='Cleaned words.'))
        self.start()
        self.speak()
        self.d.command('stop')
        self.finished()
        self.assertEqual(len(list(Path(self.d.cfg.recordings_dir).glob('*.wav'))), 1)
        note, = Path(self.d.cfg.recordings_dir).glob('*.txt')
        self.assertIn('Cleaned words.', note.read_text())

    def test_preview_never_drops_a_chunk_published_between_reads(self):
        self.start()
        self.speak('First.')
        target = self.s.target
        sequence = target._through + 1
        original = target.ledger.snapshots
        def snapshots():
            # The worker publishes the chunk and retires its ledger entry just
            # before this read; the preview must still include it.
            with target._draft_lock:
                target._text, target._through = 'First. Second.', sequence
            return original()
        with patch.object(target.ledger, 'snapshots', side_effect=snapshots):
            target.render()
        self.assertEqual(self.editor.previews[-1], 'First. Second.')
        self.d.command('cancel')
        self.finished()

    def test_uncertain_acceptance_is_never_retried_and_is_labelled_in_recovery(self):
        self.editor.insert_pinned = Mock(return_value=Landing(Outcome.UNKNOWN, 'editor timed out'))
        self.start()
        self.speak()
        self.d.command('stop')
        self.finished()
        self.editor.insert_pinned.assert_called_once()
        recovered = self.d.pipeline.journal.path(self.s.id, '.recovery.txt').read_text()
        self.assertIn('Delivery uncertain', recovered)
        self.assertIn('Some words.', recovered)

    def test_transient_preview_error_does_not_poison_later_acceptance(self):
        self.start()
        self.speak('First.')
        self.speak('Second.')
        self.s.target._last = None
        with patch.object(self.editor, 'show_draft', side_effect=TimeoutError('emacsclient timed out')):
            self.s.target.render()
        self.assertIn('timed out', self.s.target._preview_issue)
        self.assertEqual(self.s.target.field_issue(), '')
        self.speak('Third.')
        self.s.target.render()
        self.assertEqual(self.s.target._preview_issue, '')
        self.d.command('stop')
        self.finished()
        self.assertEqual(self.editor.inserted, ['First. Second. Third.'])

    def test_preview_timeout_at_acceptance_does_not_block_buffer_insertion(self):
        self.start()
        self.speak()
        self.s.target._last = None
        with patch.object(self.editor, 'show_draft', side_effect=TimeoutError('preview busy')):
            self.d.command('stop')
            self.finished()
        self.assertEqual(self.editor.inserted, ['Some words.'])

    def test_acceptance_drains_cleanup_past_shutdown_budget(self):
        self.d.cfg.pipeline.shutdown_seconds = .05
        entered, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)
        def cleanup(text, *args, **kwargs):
            entered.set()
            release.wait(2)
            return 'Prepared draft.'
        self.d.polisher = Mock(polish=cleanup)
        self.start()
        self.s.recorder.push(np.ones(5120, dtype=np.float32) * .2)
        self.s.recorder.push(np.zeros(5120, dtype=np.float32))
        self.assertTrue(entered.wait(1))
        self.d.command('stop')
        self.assertFalse(self.s.done.wait(.15), 'acceptance expired while cleanup was still healthy')
        release.set()
        self.finished()
        self.assertEqual(self.editor.inserted, ['Prepared draft.'])

    def test_shutdown_still_bounds_a_draft_that_is_draining(self):
        self.d.cfg.pipeline.shutdown_seconds = .05
        entered, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)
        def cleanup(text, *args, **kwargs):
            entered.set()
            release.wait(3)
            return text
        self.d.polisher = Mock(polish=cleanup)
        self.start()
        self.s.recorder.push(np.ones(5120, dtype=np.float32) * .2)
        self.s.recorder.push(np.zeros(5120, dtype=np.float32))
        self.assertTrue(entered.wait(1))
        self.d.command('stop')
        started = time.monotonic()
        self.d.close()
        self.assertLess(time.monotonic() - started, 1)
        self.assertTrue(self.s.done.is_set())
        self.assertEqual(self.editor.inserted, [])

    def test_definite_refusal_waits_for_explicit_retry_and_runs_hook_once(self):
        self.start()
        self.speak()
        insert = self.editor.insert_pinned
        def refuse_once(*args, **kwargs):
            if attempts.call_count == 1:
                return Landing(reason='buffer is read-only')
            return insert(*args, **kwargs)
        with patch.object(self.editor, 'insert_pinned', side_effect=refuse_once) as attempts, \
                patch.object(self.d.pipeline, '_prepare_text', wraps=self.d.pipeline._prepare_text) as prepare:
            self.d.command('stop')
            wait_for(lambda: attempts.called and self.s.draft_waiting.is_set())
            self.assertFalse(self.s.done.is_set())
            self.assertFalse(self.editor.unpinned)
            self.assertEqual(self.editor.inserted, [])
            self.assertFalse(self.d.pipeline.journal.path(self.s.id, '.permit').exists())
            self.d.command('stop')
            self.finished()
            self.assertEqual(attempts.call_count, 2)
            prepare.assert_called_once()
        self.assertEqual(self.editor.inserted, ['Some words.'])

    def test_escape_and_dictation_key_elsewhere_only_remind_about_pending_draft(self):
        self.start()
        self.speak()
        self.s.focus_changed(Focus(8, 'other'))
        wait_for(self.s.draft_waiting.is_set)
        with patch('voicekey.draft.focus.window_id', return_value=8), \
                patch('voicekey.persistent.notify') as notify:
            for code in (ecodes.KEY_ESC, ecodes.KEY_RIGHTMETA):
                self.key(code)
                self.assertFalse(self.s.target.cancelled.is_set())
                self.assertFalse(self.s.done.is_set())
                self.assertEqual(self.editor.inserted, [])
            self.assertEqual([c.args[0] for c in notify.call_args_list], ['Draft still pending'] * 2)
            self.d.command('stop')  # the widget's explicit action is permitted anywhere
            self.finished()
        self.assertEqual(self.editor.inserted, ['Some words.'])

    def test_unknown_compositor_focus_retains_draft_and_explains_widget_controls(self):
        self.start()
        self.speak()
        with patch('voicekey.draft.focus.window_id', return_value=None), \
                patch('voicekey.persistent.notify') as notify:
            for code in (ecodes.KEY_ESC, ecodes.KEY_RIGHTMETA):
                self.key(code)
                self.assertFalse(self.s.target.cancelled.is_set())
                self.assertFalse(self.s.stopping.is_set())
            self.assertEqual([c.args[0] for c in notify.call_args_list],
                             ['Draft focus could not be verified'] * 2)
            self.assertIn('widget', notify.call_args.args[1])
        self.d.command('cancel')
        self.finished()

    def test_editor_focus_timeout_is_unknown_and_queries_stay_off_key_thread(self):
        self.start()
        self.speak()
        self.editor.focus_identity = (123, '/test/nvim')
        controller = threading.get_ident()
        def query(*args, **kwargs):
            self.assertNotEqual(threading.get_ident(), controller)
            raise nvim.NvimError('RPC timeout')
        with patch('voicekey.draft.nvim.call', side_effect=query), \
                patch('voicekey.persistent.notify') as notify:
            self.key(ecodes.KEY_ESC)
            self.assertFalse(self.s.target.cancelled.is_set())
            self.assertEqual(notify.call_args.args[0], 'Draft focus could not be verified')
        self.d.command('cancel')
        self.finished()

    def test_accept_key_queued_during_binding_finishes_ordinary_fallback(self):
        target = self.browser()
        release = self.blocked_start(target, key=True)
        self.ordinary_speech()
        self.d.cfg.tap_seconds = 10
        self.d._on_key('test', ecodes.KEY_RIGHTMETA, 0)
        self.d._on_key('test', ecodes.KEY_RIGHTMETA, 1)
        release.set()
        self.finished()
        self.assertIn(('commit_string', 'Some words.'), target.ime._im.calls)

    def test_cancel_key_queued_during_binding_discards_supported_draft(self):
        release = self.blocked_start(self.editor)
        self.ordinary_speech()
        self.d._on_key('test', ecodes.KEY_ESC, 1)
        release.set()
        self.finished()
        self.assertEqual(self.editor.inserted, [])

    def test_cancel_key_during_binding_blocks_ordinary_delivery_before_focus_worker_runs(self):
        target = self.browser()
        release = self.blocked_start(target)
        self.ordinary_speech()
        # Hold the focus worker back after it has received the key. The mode
        # transition must block delivery without relying on thread scheduling.
        with patch('voicekey.persistent.threading.Thread') as thread:
            self.d._on_key('test', ecodes.KEY_ESC, 1)
            self.assertTrue(thread.called)
        self.d.command('stop')
        release.set()
        self.finished()
        self.assertFalse(any(c[0] == 'commit_string' for c in target.ime._im.calls))

    def test_returning_to_original_window_allows_escape_and_reports_recovery(self):
        self.start()
        self.speak()
        self.s.focus_changed(Focus(8, 'other'))
        wait_for(self.s.draft_waiting.is_set)
        with patch('voicekey.persistent.notify') as notify:
            self.key(ecodes.KEY_ESC)  # the focus adapter now reports the original window
            self.finished()
            self.assertTrue(any('--copy-last' in str(c.args) and c.kwargs.get('attention')
                                for c in notify.call_args_list))
        self.assertIn('--copy-last', self.s.reason)
        self.assertEqual(self.editor.inserted, [])

    def test_neovim_in_same_terminal_must_also_be_the_focused_instance(self):
        self.start()
        self.speak()
        self.editor.focus_identity = (123, '/test/nvim')
        with patch('voicekey.draft.nvim.call', return_value={'pid': 123, 'focused': False}):
            self.key(ecodes.KEY_ESC)
            self.assertFalse(self.s.target.cancelled.is_set())
        self.d.command('cancel')
        self.finished()


class DraftCapabilityTests(unittest.TestCase):
    def test_only_specific_capability_refusals_authorize_fallback(self):
        from voicekey.draft import DraftTarget, DraftUnsupported
        for error in (emacs.EmacsRefused, nvim.NvimRefused):
            for reason in ('drafts require an editable text buffer',
                           'draft preview expired', 'the pinned buffer is gone',
                           'selection or operator pending'):
                with self.subTest(error=error, reason=reason):
                    editor = Editor()
                    editor.show_draft = Mock(side_effect=error(reason))
                    draft = DraftTarget('session', editor, Mock())
                    unsupported = reason in ('drafts require an editable text buffer', 'selection or operator pending')
                    expected = DraftUnsupported if unsupported else error
                    with self.assertRaises(expected):
                        draft.initialize()
                    self.assertFalse(editor.preview.closed)


class DraftRecoveryTests(unittest.TestCase):
    def test_crash_after_acknowledged_refusal_is_not_reported_as_uncertain(self):
        with tempfile.TemporaryDirectory() as directory:
            journal = Journal(directory + '/sessions')
            journal.prepare()
            journal.append('f', 'session-start', draft=True)
            journal.append('f', 'draft-update', draft=True, final='Whole draft.', through=0)
            journal.append('f', 'delivery-attempt', attempt='a', final='Whole draft.')
            journal.append('f', 'delivery-result', attempt='a', outcome='refused')
            path, = journal.recover_interrupted()
            self.assertEqual(Path(path).read_text(), 'Whole draft.\n')

    def test_crash_after_confirmed_insertion_does_not_offer_the_draft_for_reinsertion(self):
        with tempfile.TemporaryDirectory() as directory:
            journal = Journal(directory + '/sessions')
            journal.prepare()
            journal.append('f', 'session-start', draft=True)
            journal.append('f', 'draft-update', draft=True, final='Whole draft.', through=0)
            journal.append('f', 'delivery-attempt', attempt='a', final='Whole draft.')
            journal.append('f', 'delivery-result', attempt='a', outcome='confirmed')
            self.assertEqual(journal.recover_interrupted(), [])

    def test_crash_recovers_latest_revision_and_unprocessed_suffix_exactly_once(self):
        with tempfile.TemporaryDirectory() as directory:
            journal = Journal(directory + '/sessions')
            journal.prepare()
            journal.append('f', 'session-start', draft=True)
            for seq, text in enumerate(('First raw.', 'Second raw.', 'Unprocessed tail.')):
                identity = str(seq)
                journal.capture(identity, np.zeros(160, dtype=np.float32), '')
                journal.append(identity, 'segment', session_id='f', sequence=seq)
                journal.append(identity, 'transcribed', raw=text)
            journal.append('f', 'draft-update', draft=True, final='First revision.', through=0)
            journal.append('f', 'draft-update', draft=True, final='Combined revision.', through=1)
            paths = journal.recover_interrupted()
            self.assertEqual(len(paths), 1)
            self.assertEqual(Path(paths[0]).read_text(), 'Combined revision.\n\nUnprocessed tail.\n')
            self.assertEqual(journal.recover_interrupted(), [])

    def test_crash_after_acceptance_attempt_marks_the_whole_draft_uncertain(self):
        with tempfile.TemporaryDirectory() as directory:
            journal = Journal(directory + '/sessions')
            journal.prepare()
            journal.append('f', 'session-start', draft=True)
            journal.append('f', 'draft-update', draft=True, final='Whole draft.', through=2)
            journal.append('f', 'delivery-attempt', final='Whole draft.')
            path, = journal.recover_interrupted()
            self.assertEqual(Path(path).read_text(),
                             '[Delivery uncertain; check the destination before pasting.]\nWhole draft.\n')
            self.assertFalse(journal.path('f', '.permit').exists())
