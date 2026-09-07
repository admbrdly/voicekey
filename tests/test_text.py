import shlex
import sys
import tempfile
import time
import unittest
from pathlib import Path

from voicekey.text import hook, override


def python_hook(code):
    return shlex.quote(sys.executable) + ' -c ' + shlex.quote(code)


class OverridesTests(unittest.TestCase):
    def test_literal_longest_non_cascading_and_boundaries(self):
        rules = {'hyper': 'over', 'hyper whisper': 'hyprwhspr', 'hyprwhspr': 'wrong',
                 'niri': r'\niri', 'C++': 'cpp'}
        self.assertEqual(override('HYPER WHISPER hyper niri C++ hyperbole', rules),
                         r'hyprwhspr over \niri cpp hyperbole')

    def test_unicode_case_and_single_letters_have_word_boundaries(self):
        self.assertEqual(override('İ i inside ß Straße', {'i': 'I', 'ß': 'ss'}),
                         'I I inside ss Straße')


class HookTests(unittest.TestCase):
    def run_hook(self, command, value='original', seconds=1):
        return hook(value, command, time.monotonic() + seconds)

    def test_input_is_verbatim_not_shell_code(self):
        value = '$(false); `false` "quotes"\nGrüße\n'
        self.assertEqual(self.run_hook('cat', value), (value, 'applied'))

    def test_nonempty_output_replaces_and_preserves_newlines(self):
        self.assertEqual(self.run_hook("printf 'first\nsecond\n'"), ('first\nsecond\n', 'applied'))

    def test_empty_failure_invalid_and_oversize_preserve_input(self):
        commands = ['true', "printf '   '", "printf changed; exit 77", 'exit 1',
                    python_hook('import sys; sys.stdout.buffer.write(b"\\xff")'),
                    python_hook('print("x" * 100001)'),
                    python_hook('import sys; sys.stdout.buffer.write(b"\\0")')]
        for command in commands:
            with self.subTest(command=command):
                self.assertEqual(self.run_hook(command)[0], 'original')

    def test_deadline_does_not_start_command(self):
        self.assertIn('fallback', self.run_hook('cat', seconds=-1)[1])

    def test_timeout_kills_shell_children(self):
        with tempfile.TemporaryDirectory() as directory:
            marker = Path(directory) / 'late'
            command = python_hook(f'import time; from pathlib import Path; time.sleep(.3); Path({str(marker)!r}).touch()')
            started = time.monotonic()
            value, reason = self.run_hook(command, seconds=.05)
            self.assertEqual(value, 'original')
            self.assertIn('timed out', reason)
            self.assertLess(time.monotonic() - started, .25)
            time.sleep(.35)
            self.assertFalse(marker.exists())
