from __future__ import annotations

import os
import stat
import tempfile
import threading
import unittest
from unittest.mock import patch

from voicekey import recovery


class RecoveryTests(unittest.TestCase):
    def test_recovery_file_is_private(self):
        with tempfile.TemporaryDirectory() as directory:
            state_dir = os.path.join(directory, "voicekey")
            path = os.path.join(state_dir, "last-recovery.txt")
            with patch.object(recovery, "STATE_DIR", state_dir):
                with patch.object(recovery, "LAST_RECOVERY", path):
                    self.assertEqual(recovery.save("secret"), path)
            self.assertEqual(stat.S_IMODE(os.stat(state_dir).st_mode), 0o700)
            self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o600)
            with open(path, encoding="utf-8") as handle:
                self.assertEqual(handle.read(), "secret\n")

    def test_concurrent_saves_leave_one_whole_transcript(self):
        # Two workers can fail at once; a reader must never see a torn file.
        with tempfile.TemporaryDirectory() as directory:
            state_dir = os.path.join(directory, "voicekey")
            path = os.path.join(state_dir, "last-recovery.txt")
            texts = ["A" * 20000, "B" * 20000]
            whole = {text + "\n" for text in texts}
            torn = []

            def read_repeatedly():
                for _ in range(300):
                    try:
                        with open(path, encoding="utf-8") as handle:
                            content = handle.read()
                    except FileNotFoundError:
                        continue
                    if content not in whole:
                        torn.append(len(content))

            with patch.object(recovery, "STATE_DIR", state_dir), \
                    patch.object(recovery, "LAST_RECOVERY", path):
                threads = [threading.Thread(target=lambda t=text: [recovery.save(t) for _ in range(50)])
                           for text in texts] + [threading.Thread(target=read_repeatedly)]
                for thread in threads:
                    thread.start()
                for thread in threads:
                    thread.join()
            self.assertEqual(torn, [])
            self.assertEqual(set(os.listdir(state_dir)), {"last-recovery.txt", "recovered"})
            self.assertEqual(len(os.listdir(os.path.join(state_dir, "recovered"))), 100)


if __name__ == "__main__":
    unittest.main()

class JournalTests(unittest.TestCase):
    def test_sigkill_backlog_is_recovered_once_in_order_without_retrying_delivery(self):
        import signal
        import subprocess
        import sys
        from pathlib import Path
        with tempfile.TemporaryDirectory() as directory:
            script = '''
import os, signal, sys
import numpy as np
from voicekey.recovery import Journal
journal = Journal(sys.argv[1])
journal.append('f', 'session-start')
for identity, sequence, text in (('c', 2, 'Third.'), ('a', 0, 'First.'),
                                  ('b', 1, 'Already inserted.'), ('d', 3, '')):
    journal.capture(identity, np.zeros(160, dtype=np.float32), '')
    journal.append(identity, 'segment', session_id='f', sequence=sequence)
    if text:
        journal.append(identity, 'final', final=text)
journal.append('b', 'outcome', outcome='confirmed')
journal.append('c', 'delivery-attempt', final='Third.')
with journal.path('f', '.jsonl').open('ab') as handle:
    handle.write(b'{"event":')
os.kill(os.getpid(), signal.SIGKILL)
'''
            run = subprocess.run([sys.executable, '-c', script, directory + '/sessions'],
                                 capture_output=True, text=True, timeout=5)
            self.assertEqual(run.returncode, -signal.SIGKILL, run.stderr)
            journal = recovery.Journal(directory + '/sessions')
            self.assertTrue(journal.path('c', '.permit').exists())
            with self.assertLogs('voicekey.recovery', level='WARNING'):
                recovered = journal.recover_interrupted()
            self.assertEqual(recovered, [str(journal.path('f', '.recovery.txt'))])
            text = Path(recovered[0]).read_text()
            self.assertLess(text.index('First.'), text.index('Third.'))
            self.assertNotIn('Already inserted.', text)
            self.assertIn('Delivery uncertain', text)
            self.assertIn(str(journal.path('d', '.wav')), text)
            self.assertFalse(journal.path('c', '.permit').exists())
            self.assertTrue(journal.path('c', '.wav').exists())
            original = {p.name: p.read_bytes() for p in journal.directory.iterdir()}
            with self.assertLogs('voicekey.recovery', level='WARNING'):
                self.assertEqual(journal.recover_interrupted(), [])
            self.assertEqual(original, {p.name: p.read_bytes() for p in journal.directory.iterdir()})

    def test_startup_skips_closed_and_non_session_records_and_recovers_latest_last(self):
        from pathlib import Path
        with tempfile.TemporaryDirectory() as directory:
            journal = recovery.Journal(directory + '/sessions')
            for session, utterance, text in (('f', 'a', 'Older.'), ('e', 'b', 'Newer.')):
                journal.append(session, 'session-start')
                journal.append(utterance, 'segment', session_id=session, sequence=0)
                journal.append(utterance, 'final', final=text)
            os.utime(journal.path('f', '.jsonl'), (1, 1))
            os.utime(journal.path('e', '.jsonl'), (2, 2))
            journal.append('c', 'session-start')
            journal.close_session('c')
            journal.append('d', 'final', final='Ordinary dictation.')
            untouched = {identity: journal.path(identity, '.jsonl').read_bytes() for identity in ('c', 'd')}
            self.assertEqual(journal.recover_interrupted(), [str(journal.path(s, '.recovery.txt')) for s in ('f', 'e')])
            self.assertEqual((Path(directory) / 'last-recovery.txt').read_text(), 'Newer.\n')
            for identity, content in untouched.items():
                self.assertEqual(journal.path(identity, '.jsonl').read_bytes(), content)

    def test_damaged_record_does_not_block_other_utterances_or_disable_storage(self):
        from pathlib import Path
        with tempfile.TemporaryDirectory() as directory:
            journal = recovery.Journal(directory + '/sessions')
            for identity, sequence, text in (('a', 0, 'First.'), ('b', 1, 'Second.')):
                journal.append(identity, 'segment', session_id='f', sequence=sequence)
                journal.append(identity, 'final', final=text)
                journal.append(identity, 'outcome', outcome='saved')
            damaged = journal.path('a', '.jsonl')
            with damaged.open('a') as handle:
                handle.write('{"event":\n')
            original = damaged.read_bytes()
            # A torn manifest line must also preserve its remaining valid index.
            with journal.path('f', '.jsonl').open('a') as handle:
                handle.write('[]\n')
            with self.assertLogs('voicekey.recovery', level='WARNING'):
                summary = Path(journal.close_session('f')).read_text()
            self.assertIn('Second.', summary)
            self.assertIn('Incomplete utterance journal', summary)
            self.assertIn(str(damaged), summary)
            self.assertIn('Incomplete session journal', summary)
            self.assertEqual(damaged.read_bytes(), original)
            journal.prepare()
            journal.append('c', 'final', final='New dictation still works.')

    def test_quota_keeps_unresolved_work_and_prunes_successful_history(self):
        from pathlib import Path
        import numpy as np
        with tempfile.TemporaryDirectory() as directory:
            journal = recovery.Journal(directory)
            journal.capture('a', np.zeros(1600, dtype=np.float32), 'live')
            journal.append('a', 'transcribed', raw='raw')
            journal.append('a', 'outcome', outcome='confirmed')
            self.assertFalse(journal.path('a', '.wav').exists())
            journal.capture('b', np.zeros(1600, dtype=np.float32), 'live')
            journal.limit = sum(p.stat().st_size for p in Path(directory).iterdir())
            journal.prepare(1)
            self.assertFalse(journal.path('a', '.jsonl').exists())
            self.assertTrue(journal.path('b', '.wav').exists())
            with self.assertRaises(OSError):
                journal.prepare(journal.limit)

    def test_audio_and_all_tiers_are_private_and_recovery_is_not_overwritten(self):
        import numpy as np
        with tempfile.TemporaryDirectory() as directory:
            journal = recovery.Journal(directory + '/sessions')
            for identity in ('a', 'b'):
                journal.capture(identity, np.zeros(1600, dtype=np.float32), identity)
                journal.append(identity, 'final', raw=identity, final=identity)
                journal.recover(identity, identity)
                journal.append(identity, 'outcome', outcome='copied')
            for path in journal.directory.iterdir():
                self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
            self.assertIn('raw: a', journal.path('a', '.txt').read_text())
            self.assertIn('raw: b', journal.path('b', '.txt').read_text())

    def test_session_recovery_orders_text_and_retains_uncertain_and_audio_only_entries(self):
        import numpy as np
        from pathlib import Path
        with tempfile.TemporaryDirectory() as directory:
            journal = recovery.Journal(directory + '/sessions')
            for identity, sequence, text, outcome in (
                    ('a', 2, 'Third.', 'unknown'), ('b', 0, 'First.', 'saved'),
                    ('c', 1, 'Already inserted.', 'confirmed'), ('d', 3, '', 'saved')):
                journal.capture(identity, np.zeros(16, dtype=np.float32), text)
                journal.append(identity, 'segment', session_id='f', sequence=sequence)
                journal.append(identity, 'outcome', outcome=outcome)
            path = Path(journal.close_session('f'))
            text = path.read_text()
            self.assertLess(text.index('First.'), text.index('Third.'))
            self.assertNotIn('Already inserted.', text)
            self.assertIn('Delivery uncertain', text)
            self.assertIn(str(journal.path('d', '.wav')), text)
            self.assertEqual(text, (Path(directory) / 'last-recovery.txt').read_text())
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
            self.assertFalse(journal.path('f', '.done').exists())
            journal.history_days = -1
            journal.prepare()
            self.assertTrue(path.exists())
