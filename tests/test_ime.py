from __future__ import annotations

import threading
import time
import unittest
from unittest.mock import patch

from voicekey import ime as ime_mod
from voicekey.ime import ImeHung, InputMethod


class FakeProxy:
    def __init__(self):
        self.calls = []

    def set_preedit_string(self, text, begin, end):
        self.calls.append(("preedit", text, begin, end))

    def commit_string(self, text):
        self.calls.append(("commit_string", text))

    def commit(self, serial):
        self.calls.append(("commit", serial))

    def delete_surrounding_text(self, before, after):
        self.calls.append(("delete", before, after))


class FakeDisplay:
    def flush(self):
        pass

    def roundtrip(self):
        pass


class FakeManager:
    def __init__(self):
        self.created = 0

    def get_input_method(self, seat):
        self.created += 1
        proxy = FakeProxy()
        proxy.dispatcher = {}
        proxy.destroy = lambda: proxy.calls.append(("destroy",))
        return proxy


def _offline_input_method() -> InputMethod:
    """State machine and request logic only — no Wayland connection, no thread."""
    ime = InputMethod.__new__(InputMethod)
    ime._reset()
    ime._im = FakeProxy()
    ime._display = FakeDisplay()
    ime._manager = FakeManager()
    ime._seat = object()
    return ime


class ActivationTests(unittest.TestCase):
    def test_activation_is_applied_on_done_and_numbered(self):
        ime = _offline_input_method()
        self.assertIsNone(ime.activation())
        ime._on_activate(None)
        self.assertIsNone(ime.activation(), "not applied before done")
        ime._on_done(None)
        self.assertEqual(ime.activation(), 1)
        ime._on_deactivate(None)
        ime._on_done(None)
        self.assertIsNone(ime.activation())
        ime._on_activate(None)
        ime._on_done(None)
        self.assertEqual(ime.activation(), 2)

    def test_requests_carry_the_serial_of_done_events(self):
        ime = _offline_input_method()
        ime._on_activate(None)
        ime._on_done(None)
        ime._on_done(None)  # e.g. a surrounding-text update
        self.assertTrue(ime._apply(1, preedit="héllo"))
        self.assertEqual(ime._im.calls, [("preedit", "héllo", 6, 6), ("commit", 2)])

    def test_stale_generation_is_refused(self):
        ime = _offline_input_method()
        ime._on_activate(None)
        ime._on_done(None)
        ime._on_deactivate(None)
        ime._on_done(None)
        ime._on_activate(None)
        ime._on_done(None)
        self.assertFalse(ime._apply(1, commit="late text"))
        self.assertFalse(ime._apply(2, preedit="x") is False)
        self.assertEqual(ime._im.calls, [("preedit", "x", 1, 1), ("commit", 3)])

    def test_commit_replaces_the_preedit(self):
        ime = _offline_input_method()
        ime._on_activate(None)
        ime._on_done(None)
        self.assertTrue(ime._apply(1, commit="final text"))
        self.assertEqual(ime._im.calls, [("commit_string", "final text"), ("commit", 1)])

    def test_what_the_field_was_showing_at_deactivation_is_remembered(self):
        ime = _offline_input_method()
        ime._on_activate(None)
        ime._on_done(None)
        ime._apply(1, preedit="hello wor")
        self.assertEqual(ime.left_showing(), "", "still active: nothing was left behind")
        ime._on_deactivate(None)
        ime._on_done(None)
        self.assertEqual(ime.left_showing(), "hello wor")
        ime._apply(1, preedit="stale")  # refused: inactive, so it changes nothing
        self.assertEqual(ime.left_showing(), "hello wor")
        # A field that committed, or was cleared, before deactivation left nothing.
        ime._on_activate(None)
        ime._on_done(None)
        ime._apply(2, preedit="again")
        ime._apply(2, commit="again")
        ime._on_deactivate(None)
        ime._on_done(None)
        self.assertEqual(ime.left_showing(), "")

    def test_surrounding_text_is_split_at_the_cursor_and_replace_deletes_before_it(self):
        ime = _offline_input_method()
        ime._on_activate(None)
        ime._on_surrounding_text(None, "Dear all, héllo wor|after", len("Dear all, héllo wor".encode()), 0)
        ime._on_done(None)
        self.assertEqual(ime.surrounding_text(), ("Dear all, héllo wor", "|after"))
        self.assertEqual(ime.before_cursor(), "r")
        self.assertTrue(ime._apply(1, commit="hello world", delete_before=len("héllo wor".encode()), expected=ime.snapshot()))
        self.assertEqual(ime._im.calls, [("delete", 10, 0), ("commit_string", "hello world"), ("commit", 1)])
        self.assertEqual(ime.left_showing(), "")

    def test_clearing_sends_a_bare_commit_never_an_empty_preedit(self):
        ime = _offline_input_method()
        ime._on_activate(None)
        ime._on_done(None)
        self.assertTrue(ime._apply(1, preedit=""))
        self.assertEqual(ime._im.calls, [("commit", 1)])


class TakeoverTests(unittest.TestCase):
    def test_unavailable_turns_in_field_text_off(self):
        ime = _offline_input_method()
        ime._on_activate(None)
        ime._on_done(None)
        ime._on_unavailable(None)
        self.assertIsNone(ime.activation())
        self.assertFalse(ime._apply(1, preedit="ignored"))
        self.assertEqual(ime._im.calls, [])

    def test_bind_replaces_the_object_and_resets_activation(self):
        ime = _offline_input_method()
        old = ime._im
        old.destroy = lambda: old.calls.append(("destroy",))
        ime._on_activate(None)
        ime._on_done(None)
        self.assertTrue(ime._bind())
        self.assertEqual(old.calls, [("destroy",)])
        self.assertEqual(ime._manager.created, 1)
        self.assertEqual(set(ime._im.dispatcher), set(
            ("activate", "deactivate", "surrounding_text", "text_change_cause",
             "content_type", "done", "unavailable")
        ))
        self.assertIsNone(ime.activation(), "activation arrives with the next done")
        ime._on_activate(None)
        ime._on_done(None)
        self.assertEqual(ime.activation(), 2)


class FailureTests(unittest.TestCase):
    def test_timed_out_call_is_cancelled_not_run_later(self):
        ime = _offline_input_method()  # no loop thread: every call times out
        ran = []
        with patch.object(ime_mod, "CALL_TIMEOUT", 0.01):
            self.assertFalse(ime._call(lambda: ran.append(1) or True))
        queued = ime._commands.get_nowait()
        queued()  # the loop thread catching up later must be a no-op
        self.assertEqual(ran, [])

    def test_a_call_already_running_at_timeout_is_awaited(self):
        ime = _offline_input_method()
        started = threading.Event()
        release = threading.Event()

        def operation():
            started.set()
            release.wait(5)
            return True

        def loop_thread():  # runs whatever gets posted, like the real loop
            ime._commands.get(timeout=2)()

        def release_later():
            started.wait(2)
            time.sleep(0.15)
            release.set()

        threading.Thread(target=loop_thread, daemon=True).start()
        threading.Thread(target=release_later, daemon=True).start()
        with patch.object(ime_mod, "CALL_TIMEOUT", 0.02):
            began = time.monotonic()
            self.assertTrue(ime._call(operation), "a started call reports its real result")
        self.assertGreaterEqual(time.monotonic() - began, 0.15)

    def test_a_started_call_that_hangs_writes_the_connection_off(self):
        ime = _offline_input_method()
        release = threading.Event()

        def operation():
            release.wait(5)
            return True

        threading.Thread(target=lambda: ime._commands.get(timeout=2)(), daemon=True).start()
        with patch.object(ime_mod, "CALL_TIMEOUT", 0.02), patch.object(ime_mod, "STARTED_TIMEOUT", 0.05), \
                patch.object(ime, "_sever") as sever:
            with self.assertRaises(ImeHung):
                ime._call(operation)
        sever.assert_called_once()
        self.assertTrue(ime._dead)
        self.assertIsNone(ime.activation())
        release.set()

    def test_dead_connection_turns_everything_off(self):
        ime = _offline_input_method()
        ime._on_activate(None)
        ime._on_done(None)
        ime._dead = True
        self.assertIsNone(ime.activation())
        self.assertFalse(ime._call(lambda: True))
        ime.preedit("x", 1)
        self.assertTrue(ime._commands.empty())


if __name__ == "__main__":
    unittest.main()

class RevisionTests(unittest.TestCase):
    def setUp(self):
        self.ime = _offline_input_method()
        self.addCleanup(self.ime._close_pipe)
        self.ime._on_activate(None)
        self.ime._on_done(None)

    def test_surrounding_changes_are_applied_only_on_done(self):
        self.ime._on_surrounding_text(None, 'pending', 7, 7)
        self.assertIsNone(self.ime.surrounding_text())
        self.ime._on_done(None)
        self.assertEqual(self.ime.surrounding_text(), ('pending', ''))

    def test_cursor_change_invalidates_checked_replacement(self):
        self.ime._on_surrounding_text(None, 'preview', 7, 7)
        self.ime._on_done(None)
        expected = self.ime.snapshot()
        self.ime._on_surrounding_text(None, 'valuable text', 13, 13)
        self.ime._on_done(None)
        self.assertFalse(self.ime._apply(1, commit='final', delete_before=7, expected=expected))
        self.assertEqual(self.ime._im.calls, [])

    def test_preview_history_is_kept_per_activation_across_focus_hops(self):
        self.ime._apply(1, preedit='first live')
        self.ime._on_deactivate(None)
        self.ime._on_done(None)
        self.ime._on_activate(None)
        self.ime._on_done(None)
        self.ime._on_deactivate(None)
        self.ime._on_done(None)
        self.assertEqual(self.ime.shown_for(1), 'first live')

    def test_failed_flush_is_never_success(self):
        self.ime._display.flush = lambda: -1
        with self.assertRaises(Exception):
            self.ime._apply(1, commit='text', deadline=time.monotonic() - 1e-6 + 0.01)

    def test_a_started_exception_is_uncertain_not_a_refusal(self):
        self.ime._post = lambda function: (function() or True)
        with patch.object(self.ime, '_sever'):
            with self.assertRaises(ImeHung):
                self.ime._call(lambda: (_ for _ in ()).throw(RuntimeError('partial send')))

    def test_preview_posts_are_coalesced_and_do_not_fill_command_queue(self):
        self.ime.claim_preview('owner')
        for index in range(20000):
            self.ime.preedit(str(index), 1, 'owner')
        self.assertTrue(self.ime._commands.empty())
        self.assertEqual(self.ime._preview_pending[0], '19999')

    def test_queued_preview_clear_rechecks_owner_before_applying(self):
        self.ime.claim_preview('old')
        result = []
        caller = threading.Thread(target=lambda: result.append(self.ime.clear_preedit(1, 'old', timeout=1)))
        caller.start()
        command = self.ime._commands.get(timeout=1)
        self.ime.claim_preview('new')
        self.ime.preedit('new live', 1, 'new')
        command()
        caller.join(1)
        self.assertFalse(caller.is_alive())
        self.assertEqual(result, [True])
        self.assertEqual(self.ime._preview_pending, ('new live', 1, 'new'))
        self.assertEqual(self.ime._im.calls, [])

    def test_expired_preview_clear_cannot_run_later(self):
        self.ime.claim_preview('old')
        self.assertFalse(self.ime.clear_preedit(1, 'old', timeout=0.01))
        self.ime._commands.get_nowait()()
        self.assertEqual(self.ime._im.calls, [])
