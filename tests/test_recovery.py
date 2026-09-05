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
