import subprocess
import threading
import time
import unittest
from unittest.mock import Mock, patch

from voicekey import emacs
from voicekey.focus import Focus
from voicekey.target import (ImePreview, ImeTarget, EmacsTarget, WtypeTarget, ClipboardTarget,
                             NotifyPreview, Window, Outcome, bind)
from voicekey.config import DictationConfig
from voicekey.ime import ImeHung
from tests.test_ime import _offline_input_method


class TargetTests(unittest.TestCase):
    def setUp(self):
        self.ime = _offline_input_method()
        self.addCleanup(self.ime._close_pipe)
        self.ime._post = lambda fn: (fn() or True)
        self.activate()
        self.focus = patch('voicekey.target.focus.window_id', return_value=7).start()
        patch('voicekey.target.notify').start()
        self.addCleanup(patch.stopall)

    def activate(self):
        self.ime._on_activate(None)
        self.ime._on_done(None)

    def deactivate(self):
        self.ime._on_deactivate(None)
        self.ime._on_done(None)

    def land(self, target, text='hello', **kwargs):
        return target.land(text, time.monotonic() + 1, operation_id='operation', **kwargs)

    def test_bound_app_permission_applies_only_to_input_method(self):
        cfg = DictationConfig(multiline_apps=['composer'])
        self.ime._on_content_type(None, 0x200, 0)
        self.ime._on_done(None)
        with patch('voicekey.target.focus.focused', return_value=Focus(7, 'composer')):
            target = bind(self.ime, cfg, True)
            self.assertEqual(self.land(target, 'first\nsecond').outcome, Outcome.SUBMITTED)
            self.assertIn(('commit_string', 'first\nsecond'), self.ime._im.calls)
            fallback = bind(None, cfg, True)
        with patch('voicekey.inject._run') as run:
            self.assertEqual(self.land(fallback, 'first\nsecond').outcome, Outcome.SUBMITTED)
        self.assertEqual(run.call_args.args[1], 'first second')

    def test_control_characters_are_definite_refusals_without_partial_typing(self):
        for target in (ImeTarget(self.ime, 1, Window(7, True), 'browser'),
                       WtypeTarget(NotifyPreview('dictate'), Window(7, True), 'terminal')):
            with self.subTest(target=target.kind), patch('voicekey.inject._run') as run:
                result = self.land(target, 'before\x1bafter')
                self.assertEqual(result.outcome, Outcome.REFUSED)
                self.assertIn('U+001B', result.reason)
                run.assert_not_called()
        self.assertFalse(any(c[0] == 'commit_string' for c in self.ime._im.calls))

    def test_changed_field_in_same_window_is_refused(self):
        target = ImeTarget(self.ime, 1, Window(7, True), 'browser')
        self.deactivate()
        self.activate()
        self.assertEqual(self.land(target).outcome, Outcome.REFUSED)
        self.assertEqual(self.ime._im.calls, [])

    def test_panel_binding_waits_for_window_and_delayed_field_without_rebinding(self):
        clock = [0.0]
        def sleep(seconds):
            clock[0] += seconds
        ime = Mock(rebind=Mock(return_value=True), activation=lambda: 7 if clock[0] >= .65 else None)
        def focused(**kwargs):
            return Focus(7, 'browser') if clock[0] >= .3 else Focus()
        with patch('voicekey.target.time', Mock(monotonic=lambda: clock[0], sleep=sleep)), \
                patch('voicekey.target.focus.focused', side_effect=focused):
            target = bind(ime, DictationConfig(), False, activation_wait=1)
        self.assertIsInstance(target, ImeTarget)
        self.assertEqual(target.window_id, 7)
        self.assertEqual(target.preview.generation, 7)
        ime.rebind.assert_called_once()
        self.assertLess(clock[0], 1)

    def test_panel_wait_does_not_retarget_if_window_changes_during_field_activation(self):
        ime = Mock(rebind=Mock(return_value=True), activation=Mock(return_value=7))
        with patch('voicekey.target.focus.focused', side_effect=[Focus(7, 'browser'), Focus(8, 'browser')]):
            target = bind(ime, DictationConfig(), False, activation_wait=1)
        self.assertIsInstance(target, ClipboardTarget)

    def test_commit_is_submitted_not_application_acknowledged(self):
        target = ImeTarget(self.ime, 1, Window(7, True), 'browser')
        self.assertEqual(self.land(target).outcome, Outcome.SUBMITTED)
        self.assertIn(('commit_string', 'hello'), self.ime._im.calls)

    def test_same_target_cannot_submit_an_operation_twice(self):
        target = ImeTarget(self.ime, 1, Window(7, True), 'browser')
        self.land(target)
        self.assertEqual(self.land(target).outcome, Outcome.UNKNOWN)
        self.assertEqual(sum(c[0] == 'commit_string' for c in self.ime._im.calls), 1)

    def test_spacing_uses_surrounding_text_at_execution(self):
        target = ImeTarget(self.ime, 1, Window(7, True), 'browser')
        self.ime._on_surrounding_text(None, 'old text', 8, 8)
        self.ime._on_done(None)
        self.assertEqual(target.before(), 't')
        self.ime._on_surrounding_text(None, '', 0, 0)
        self.ime._on_done(None)
        self.land(target, prefix=' ')
        self.assertIn(('commit_string', 'hello'), self.ime._im.calls)

    def test_newest_preview_survives_an_older_commit(self):
        older = ImeTarget(self.ime, 1, Window(7, True), 'browser')
        newer = ImeTarget(self.ime, 1, Window(7, True), 'browser')
        newer.show('new live')
        older.show('old raw')
        self.land(older)
        self.assertIn(('preedit', 'new live', 8, 8), self.ime._im.calls)
        self.assertEqual(self.ime._preview_text, 'new live')

    def test_clear_suppresses_late_preview(self):
        target = ImeTarget(self.ime, 1, Window(7, True), 'browser')
        target.clear()
        target.show('late')
        self.assertEqual(self.ime._preview_text, '')

    def test_cancelled_or_expired_target_never_submits(self):
        for cancel in (True, False):
            target = ImeTarget(self.ime, 1, Window(7, True), 'browser')
            if cancel:
                target.cancel()
            result = target.land('late', time.monotonic() - 1, operation_id='late')
            self.assertEqual(result.outcome, Outcome.REFUSED)
        self.assertEqual(self.ime._im.calls, [])

    def test_typing_timeout_is_unknown_and_spawn_failure_is_refused(self):
        for error, expected in ((subprocess.TimeoutExpired('wtype', 1), Outcome.UNKNOWN),
                                (FileNotFoundError('wtype'), Outcome.REFUSED)):
            target = WtypeTarget(NotifyPreview('dictate'), Window(7, True), 'terminal')
            with patch('voicekey.target.inject.type_text', side_effect=error):
                self.assertEqual(self.land(target).outcome, expected)

    def test_window_change_never_starts_typing(self):
        target = WtypeTarget(NotifyPreview('dictate'), Window(7, True), 'terminal')
        self.focus.return_value = 8
        with patch('voicekey.target.inject.type_text') as type_text:
            self.assertEqual(self.land(target).outcome, Outcome.REFUSED)
        type_text.assert_not_called()

    def test_emacs_requires_acknowledged_pin_and_reports_partial_errors(self):
        pinning = Mock(id='pin', valid=False,
                       reason='the focused window belongs to Emacs process 5, not to this server (process 6)')
        target = EmacsTarget(NotifyPreview('dictate'), Window(7, True), 'emacs', pinning)
        with patch('voicekey.target.emacs.insert') as insert:
            landing = self.land(target)
        self.assertEqual(landing.outcome, Outcome.REFUSED)
        self.assertEqual(landing.reason, pinning.reason)
        insert.assert_not_called()
        for error, outcome in ((emacs.EmacsRefused('read-only'), Outcome.REFUSED),
                                (emacs.EmacsError('hook failed'), Outcome.UNKNOWN),
                                (emacs.EmacsTimeout('late'), Outcome.UNKNOWN)):
            pinning.valid = True
            target = EmacsTarget(NotifyPreview('dictate'), Window(7, True), 'emacs', pinning)
            with patch('voicekey.target.emacs.insert', side_effect=error):
                self.assertEqual(self.land(target).outcome, outcome)

    def test_emacs_delivery_uses_buffer_without_compositor_focus(self):
        pinning = Mock(id='pin', valid=True)
        target = EmacsTarget(NotifyPreview('dictate'), Window(7, True), 'emacs', pinning)
        self.focus.side_effect = AssertionError('must not ask compositor')
        with patch('voicekey.target.emacs.insert') as insert:
            self.assertEqual(self.land(target).outcome, Outcome.CONFIRMED)
        self.assertEqual(insert.call_args.args, ('hello', 'pin'))

    def test_binding_checks_window_on_both_sides_of_activation(self):
        with patch('voicekey.target.focus.focused', side_effect=[Focus(7, 'a'), Focus(8, 'b')]):
            self.assertIsInstance(bind(self.ime, DictationConfig(), True), ClipboardTarget)

    def test_binding_emacs_pins_first_and_uses_the_shared_ime_preview(self):
        with patch('voicekey.target.focus.focused', return_value=Focus(7, 'emacs')), \
                patch('voicekey.target.emacs.PendingPin') as pin, \
                patch.object(self.ime, 'rebind') as rebind:
            def rebound(**kwargs):
                pin.assert_called_once()
                return True
            rebind.side_effect = rebound
            target = bind(self.ime, DictationConfig(), False)
        self.assertIsInstance(target, EmacsTarget)
        self.assertIsInstance(target.preview, ImePreview)
        target.show('live text')
        self.assertEqual(self.ime._preview_pending, ('live text', 1, target.preview.owner))
        rebind.assert_called_once()

    def test_binding_hands_the_focused_window_process_to_the_pin(self):
        with patch('voicekey.target.focus.focused', return_value=Focus(7, 'emacs', 4242)), \
                patch('voicekey.target.emacs.PendingPin') as pin:
            target = bind(None, DictationConfig(ime=False), False)
        self.assertIsInstance(target, EmacsTarget)
        pin.assert_called_once_with(4242)

    def test_targets_describe_their_binding_for_the_journal(self):
        pinning = Mock(id='pin', valid=True, describe=Mock(return_value="buffer 'notes.org' (org-mode)"))
        self.assertEqual(EmacsTarget(NotifyPreview('dictate'), Window(7, True), 'emacs', pinning).describe(),
                         "emacs buffer 'notes.org' (org-mode)")
        self.assertEqual(WtypeTarget(NotifyPreview('dictate'), Window(7, True), 'foot').describe(),
                         'wtype target in foot')
        self.assertEqual(ClipboardTarget(NotifyPreview('dictate'), Window(None, True), None).application_name, '')
        self.assertEqual(WtypeTarget(NotifyPreview('dictate'), Window(7, True), 'org.mozilla.firefox').application_name,
                         'Firefox')

    def test_emacs_falls_back_to_notifications_without_a_usable_ime(self):
        with patch('voicekey.target.focus.focused', return_value=Focus(7, 'emacs')), \
                patch('voicekey.target.emacs.PendingPin'):
            target = bind(None, DictationConfig(ime=False), False)
            self.assertIsInstance(target, EmacsTarget)
            self.assertIsInstance(target.preview, NotifyPreview)
            with patch.object(self.ime, 'rebind', return_value=False):
                target = bind(self.ime, DictationConfig(), False)
            self.assertIsInstance(target, EmacsTarget)
            self.assertIsInstance(target.preview, NotifyPreview)

    def test_emacs_binding_does_not_rebind_while_another_dictation_is_pending(self):
        with patch('voicekey.target.focus.focused', return_value=Focus(7, 'emacs')), \
                patch('voicekey.target.emacs.PendingPin'), patch.object(self.ime, 'rebind') as rebind:
            target = bind(self.ime, DictationConfig(), True)
        rebind.assert_not_called()
        self.assertIsInstance(target.preview, ImePreview)

    def test_changed_window_during_emacs_binding_only_disables_inline_preview(self):
        with patch('voicekey.target.focus.focused', side_effect=[Focus(7, 'emacs'), Focus(8, 'browser')]), \
                patch('voicekey.target.emacs.PendingPin'):
            target = bind(self.ime, DictationConfig(), True)
        self.assertIsInstance(target, EmacsTarget)
        self.assertEqual(target.window_id, 7)
        self.assertIsInstance(target.preview, NotifyPreview)

    def test_emacs_flushes_preview_clear_then_inserts_through_editor(self):
        preview = ImePreview(self.ime, 1)
        target = EmacsTarget(preview, Window(7, True), 'emacs', Mock(id='pin', valid=True))
        target.show('live')
        self.ime._apply(1, preedit='live', owner=preview.owner)
        self.ime._im.calls.clear()
        def insert(*args, **kwargs):
            self.assertEqual(self.ime._im.calls, [('commit', 1)])
            self.assertEqual(self.ime._shown, '')
            self.assertIsNone(self.ime._preview_pending)
        with patch('voicekey.target.emacs.insert', side_effect=insert) as inserted:
            self.assertEqual(self.land(target).outcome, Outcome.CONFIRMED)
        self.assertEqual(inserted.call_args.args, ('hello', 'pin'))
        target.show('late preview')
        self.assertIsNone(self.ime._preview_pending)

    def test_lost_emacs_activation_does_not_redirect_preview_or_prevent_pinned_insert(self):
        target = EmacsTarget(ImePreview(self.ime, 1), Window(7, True), 'emacs', Mock(id='pin', valid=True))
        self.deactivate()
        self.activate()
        self.focus.side_effect = AssertionError('pinned delivery does not ask for focus')
        with patch('voicekey.target.emacs.insert') as inserted:
            self.assertEqual(self.land(target).outcome, Outcome.CONFIRMED)
        inserted.assert_called_once()
        self.assertEqual(self.ime._im.calls, [])

    def test_an_older_emacs_delivery_does_not_clear_a_newer_preview(self):
        target = EmacsTarget(ImePreview(self.ime, 1), Window(7, True), 'emacs', Mock(id='pin', valid=True))
        newer = ImePreview(self.ime, 1)
        newer.show('next utterance')
        with patch('voicekey.target.emacs.insert'):
            self.assertEqual(self.land(target).outcome, Outcome.CONFIRMED)
        self.assertEqual(self.ime._preview_pending, ('next utterance', 1, newer.owner))
        self.assertEqual(self.ime._im.calls, [])

    def test_failed_preview_clear_never_starts_emacs_insertion(self):
        for result in (False, ImeHung('flush failed')):
            target = EmacsTarget(ImePreview(self.ime, 1), Window(7, True), 'emacs', Mock(id='pin', valid=True))
            with patch.object(self.ime, 'clear_preedit') as clear, patch('voicekey.target.emacs.insert') as insert:
                if isinstance(result, Exception):
                    clear.side_effect = result
                else:
                    clear.return_value = result
                self.assertEqual(self.land(target).outcome, Outcome.REFUSED)
            insert.assert_not_called()

    def test_cancellation_during_preview_clear_prevents_emacs_insertion(self):
        target = EmacsTarget(ImePreview(self.ime, 1), Window(7, True), 'emacs', Mock(id='pin', valid=True))
        def clear(*args, **kwargs):
            target.cancel()
            return True
        with patch.object(self.ime, 'clear_preedit', side_effect=clear), patch('voicekey.target.emacs.insert') as insert:
            self.assertEqual(self.land(target).outcome, Outcome.REFUSED)
        insert.assert_not_called()
