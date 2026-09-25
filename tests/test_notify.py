from __future__ import annotations

import unittest
from contextlib import contextmanager, ExitStack
import queue
from unittest.mock import patch

from voicekey import notify as notifications
from voicekey.notify import _coalesce


@contextmanager
def queued_notifications(*modules):
    """Exercise the real policy without starting notify-send or its worker."""
    pending = queue.Queue()
    with patch.object(notifications, '_pending', pending), \
            patch.object(notifications, '_worker', object()), ExitStack() as stack:
        for module in modules:
            stack.enter_context(patch(f'voicekey.{module}.notify', side_effect=notifications.notify))
        yield pending


class PolicyTests(unittest.TestCase):
    def test_routine_status_and_preview_never_start_a_worker_or_enqueue(self):
        with patch.object(notifications, '_pending') as pending, \
                patch.object(notifications, '_worker', None), \
                patch.object(notifications.threading, 'Thread') as worker:
            for summary in ('Listening', 'Finishing', 'Off', 'Inserted', 'live text'):
                notifications.notify(summary, channel='persistent', ms=0)
            pending.put.assert_not_called()
            worker.assert_not_called()

    def test_attention_is_brief_and_noncritical(self):
        with queued_notifications() as pending:
            notifications.notify('voicekey: busy', 'Still processing', attention=True, ms=3000)
            channel, command = pending.get_nowait()
        self.assertEqual(channel, 'system')
        self.assertNotIn('critical', command)
        self.assertEqual(command[command.index('-t') + 1], '3000')
        self.assertEqual(command[-2:], ['voicekey: busy', 'Still processing'])

    def test_errors_and_recovery_notices_still_reach_desktop(self):
        with queued_notifications() as pending:
            notifications.notify('Delivery uncertain', 'Inspect saved text', error=True)
            channel, command = pending.get_nowait()
            self.assertIsNone(channel)
            self.assertEqual(command[command.index('-u') + 1], 'critical')
            notifications.notify('Interrupted dictation recovered', '/saved/text', attention=True, ms=0)
            _, command = pending.get_nowait()
            self.assertEqual(command[-1], '/saved/text')
            self.assertEqual(command[command.index('-t') + 1], '0')


class CoalesceTests(unittest.TestCase):
    def test_backlog_keeps_every_error_and_the_newest_preview_per_channel(self):
        batch = [
            ("dictate", ["preview 1"]),
            (None, ["error a"]),
            ("agent", ["agent 1"]),
            ("dictate", ["preview 2"]),
            (None, ["error b"]),
            ("dictate", ["preview 3"]),
        ]
        self.assertEqual(
            _coalesce(batch),
            [["error a"], ["error b"], ["agent 1"], ["preview 3"]],
        )


if __name__ == "__main__":
    unittest.main()
