import tempfile
import unittest
from voicekey.history import latest, explain
from voicekey.recovery import Journal


class HistoryTests(unittest.TestCase):
    def test_capture_order_and_exclusions(self):
        with tempfile.TemporaryDirectory() as directory:
            journal = Journal(directory)
            for identity, when, action, final in [('a', 1, 'dictate', 'first'),
                                                   ('b', 2, 'dictate', 'second\n'),
                                                   ('c', 3, 'agent', 'private prompt'),
                                                   ('d', 4, 'dictate', '')]:
                journal.append(identity, 'captured', time=when)
                journal.append(identity, 'final', action=action, final=final, raw='raw')
            journal.append('a', 'outcome', outcome='unknown', reason='late completion')
            result = latest(journal)
            self.assertEqual(result['final'], 'second\n')
            self.assertIn('pending or interrupted', explain(result))
            # A partial line being appended must not make us invent a result.
            with journal.path('b', '.jsonl').open('a') as handle:
                handle.write('{')
            result = latest(journal)
            self.assertEqual(result['id'], 'b')
            self.assertTrue(result['damaged'])

    def test_empty_history(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(LookupError):
                latest(Journal(directory))

    def test_cli_history_needs_no_config_or_models(self):
        import contextlib
        import io
        from unittest.mock import patch
        from voicekey.__main__ import main

        with tempfile.TemporaryDirectory() as directory:
            journal = Journal(directory)
            journal.append('a', 'captured')
            journal.append('a', 'final', final='exact text\n')
            output = io.StringIO()
            with patch('voicekey.history.Journal', return_value=journal), \
                    patch('sys.argv', ['voicekey', '--last', '--config', '/missing']), \
                    patch('voicekey.config.load', side_effect=AssertionError('must not load config')), \
                    contextlib.redirect_stdout(output):
                self.assertEqual(main(), 0)
            self.assertEqual(output.getvalue(), 'exact text\n')
