import threading
import unittest
from unittest.mock import Mock

import numpy as np

from voicekey.capture import Session, OVERLOAD_FRAMES


class CaptureTests(unittest.TestCase):
    def session(self):
        session = Session('dictate', 'hold', frozenset(), 'fake')
        session.target = Mock()
        self.addCleanup(session.cancel)
        return session

    def test_live_text_and_finish_run_on_decoder(self):
        session = self.session()
        thread_names = []
        def finish():
            thread_names.append(threading.current_thread().name)
            return 'finished text'
        stream = Mock(feed=Mock(return_value='live'), finish=finish)
        session.attach(lambda: stream)
        session.feed(np.zeros(1600))
        session.finish()
        self.assertEqual(session.text, 'finished text')
        self.assertEqual(thread_names, ['live-decode'])
        self.assertEqual([call.args[0] for call in session.target.show.call_args_list], ['live', 'finished text'])

    def test_hung_native_finish_is_bounded_and_late_text_suppressed(self):
        entered, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)
        session = self.session()
        def finish():
            entered.set()
            release.wait(2)
            return 'late'
        session.attach(lambda: Mock(finish=finish))
        session.finish(timeout=0.03)
        self.assertTrue(entered.is_set())
        self.assertTrue(session.stuck)
        release.set()
        session.decoder.join(1)
        session.target.show.assert_not_called()

    def test_overflow_does_not_wait_for_decoder_or_show_on_audio_thread(self):
        entered, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)
        session = self.session()
        def feed(frame):
            entered.set()
            release.wait(2)
            return 'late'
        session.attach(lambda: Mock(feed=feed))
        session.feed(np.zeros(1))
        self.assertTrue(entered.wait(1))
        for _ in range(OVERLOAD_FRAMES + 2):
            session.feed(np.zeros(1))
        self.assertFalse(session.live)
        session.target.show.assert_not_called()
        release.set()
        session.decoder.join(1)
        session.target.show.assert_not_called()

    def test_factory_failure_isolated_from_capture(self):
        session = self.session()
        session.attach(Mock(side_effect=RuntimeError('native setup')))
        session.decoder.join(1)
        session.feed(np.zeros(1600))
        self.assertFalse(session.live)
        self.assertFalse(session.stuck)
