import threading
import unittest

from voicekey.ledger import Ledger, Stage
from voicekey.spacing import Spacing, owed, spaced


class LedgerTests(unittest.TestCase):
    def test_admission_reserves_audio_and_count_until_terminal(self):
        ledger = Ledger(2, 100)
        first = ledger.admit(90)
        self.assertIsNotNone(first)
        self.assertIsNone(ledger.admit(20))
        ledger.transition(first, Stage.CAPTURING, Stage.FINALIZING, audio_seconds=1)
        second = ledger.admit(90)
        self.assertIsNotNone(second)
        self.assertIsNone(ledger.admit(1))
        ledger.complete(first, 'saved')
        self.assertIsNotNone(ledger.admit(1))

    def test_terminal_record_rejects_late_worker_and_preview_results(self):
        ledger = Ledger()
        identity = ledger.admit(1)
        ledger.live(identity, 'known')
        ledger.complete(identity, 'saved')
        self.assertFalse(ledger.live(identity, 'late'))
        self.assertFalse(ledger.transition(identity, Stage.CAPTURING, Stage.FINALIZING))
        self.assertFalse(ledger.complete(identity, 'confirmed'))
        self.assertEqual(ledger.history[0].live, 'known')

    def test_concurrent_reservations_produce_one_attempt(self):
        ledger = Ledger()
        identity = ledger.admit(1)
        for before, after in ((Stage.CAPTURING, Stage.FINALIZING), (Stage.FINALIZING, Stage.TRANSCRIBING),
                              (Stage.TRANSCRIBING, Stage.POLISHING), (Stage.POLISHING, Stage.READY)):
            ledger.transition(identity, before, after)
        barrier = threading.Barrier(3)
        results = []
        def reserve():
            barrier.wait()
            results.append(ledger.reserve(identity))
        threads = [threading.Thread(target=reserve) for _ in range(2)]
        for thread in threads:
            thread.start()
        barrier.wait()
        for thread in threads:
            thread.join()
        self.assertEqual(sum(value is not None for value in results), 1)
        self.assertEqual(ledger.get(identity).stage, Stage.DELIVERING)

    def test_history_is_bounded(self):
        ledger = Ledger()
        for _ in range(100):
            ledger.complete(ledger.admit(1), 'saved')
        self.assertEqual(len(ledger.history), 32)
        self.assertFalse(ledger.busy)


class SpacingTests(unittest.TestCase):
    def test_cursor_report_overrides_fallback(self):
        for char in ('', ' ', '\n', '(', '['):
            self.assertEqual(owed(char, ' '), '')
        self.assertEqual(owed('x'), ' ')
        self.assertEqual(owed(None, ' '), ' ')
        self.assertEqual(spaced(' ', ', next'), ', next')

    def test_typing_invalidates_continuation_even_during_delivery(self):
        spacing = Spacing()
        mark = spacing.mark()
        spacing.user_typed()
        spacing.inserted(7, 'hello', mark)
        self.assertEqual(spacing.prefix(7), '')
        spacing.inserted(7, 'hello', spacing.mark())
        self.assertEqual(spacing.prefix(7), ' ')
        self.assertEqual(spacing.prefix(8), '')
