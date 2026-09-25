import os
import re
import shutil
import subprocess
import tempfile
import time
import unittest
from pathlib import Path


def nvim_supported():
    """contrib/nvim needs Neovim 0.10 (vim.system, inline virtual text)."""
    if not shutil.which('nvim'):
        return False
    version = subprocess.run(['nvim', '--version'], capture_output=True, text=True).stdout
    match = re.match(r'NVIM v(\d+)\.(\d+)', version)
    return bool(match) and (int(match[1]), int(match[2])) >= (0, 10)


@unittest.skipUnless(nvim_supported(), 'Neovim 0.10 or newer unavailable')
class NeovimTests(unittest.TestCase):
    def test_buffer_insertion(self):
        root = Path(__file__).resolve().parents[1]
        command = ['nvim', '--headless', '-n', '-u', 'NONE', '-i', 'NONE', '-l', str(root / 'tests/nvim-tests.lua')]
        run = subprocess.run(command, capture_output=True, text=True, timeout=60)
        self.assertEqual(run.returncode, 0, run.stdout + run.stderr)


@unittest.skipUnless(nvim_supported() and shutil.which('jq'), 'Neovim 0.10+ or jq unavailable')
class RouteTests(unittest.TestCase):
    """contrib/nvim/voicekey-route with stand-ins for niri, notify-send and the daemon."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.bin = self.tmp / 'bin'
        self.bin.mkdir()
        self.runtime = self.tmp / 'runtime'
        (self.runtime / 'voicekey').mkdir(parents=True, mode=0o700)
        self.log = self.tmp / 'calls.log'
        self.stub('niri', 'printf \'{"app_id": "%s"}\\n\' "$FOCUSED_APP"')
        self.stub('notify-send', 'echo "notify $*" >> "$CALLS"')
        self.stub('python', 'echo "daemon $*" >> "$CALLS"\n'
                  '[ "$*" = "-m voicekey --control status" ] && printf \'{"listening": %s}\\n\' "$LISTENING"\n'
                  '[ -n "$DAEMON_DOWN" ] && exit 1\n'
                  '[ -n "$DAEMON_BUSY" ] && [ "$*" = "-m voicekey --control start" ] && '
                  '{ echo \'{"type": "reply", "error": "Finish the current dictation before starting another"}\'; exit 1; }\n'
                  'exit 0')

    def stub(self, name, body):
        path = self.bin / name
        path.write_text('#!/bin/sh\n' + body + '\n')
        path.chmod(0o755)

    def route(self, app, listening=False, down=False, busy=False):
        env = {'DAEMON_DOWN': '1' if down else '', 'DAEMON_BUSY': '1' if busy else '', **os.environ, 'PATH': f'{self.bin}:{os.environ["PATH"]}', 'XDG_RUNTIME_DIR': str(self.runtime),
               'VOICEKEY_PYTHON': str(self.bin / 'python'), 'FOCUSED_APP': app, 'CALLS': str(self.log),
               'LISTENING': 'true' if listening else 'false'}
        root = Path(__file__).resolve().parents[1]
        run = subprocess.run([str(root / 'contrib/nvim/voicekey-route')], env=env,
                             capture_output=True, text=True, timeout=20)
        calls = self.log.read_text().splitlines() if self.log.exists() else []
        self.log.unlink(missing_ok=True)
        return run.returncode, calls

    def editor(self):
        """A Neovim server whose :VoiceKey only records that it ran."""
        socket = self.tmp / 'nvim.sock'
        process = subprocess.Popen(['nvim', '--headless', '-n', '-u', 'NONE', '-i', 'NONE', '--listen', str(socket),
                                    '-c', 'command VoiceKey let g:dictated = get(g:, "dictated", 0) + 1'],
                                   stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.addCleanup(process.wait, 5)
        self.addCleanup(process.kill)
        for _ in range(200):
            if socket.exists():
                break
            time.sleep(0.02)
        return socket

    def dictated(self, socket):
        return subprocess.run(['nvim', '--server', str(socket), '--remote-expr', 'get(g:, "dictated", 0)'],
                              capture_output=True, text=True, timeout=10).stdout.strip()

    def test_terminal_with_focused_neovim_dictates_into_neovim(self):
        socket = self.editor()
        (self.runtime / 'voicekey' / 'nvim-focus').write_text(f'{socket}\n')
        code, calls = self.route('com.mitchellh.ghostty')
        self.assertEqual(code, 0)
        self.assertEqual(self.dictated(socket), '1')
        self.assertEqual(calls, ['daemon -m voicekey --control status'], 'the daemon is never started')

    def test_terminal_without_focused_neovim_refuses(self):
        for focus in (None, str(self.tmp / 'gone.sock')):
            with self.subTest(focus=focus):
                if focus:
                    (self.runtime / 'voicekey' / 'nvim-focus').write_text(focus + '\n')
                code, calls = self.route('com.mitchellh.ghostty')
                self.assertEqual(code, 1)
                self.assertNotIn('daemon -m voicekey --control start', calls)
                self.assertTrue(any(call.startswith('notify ') for call in calls), calls)

    def test_other_applications_start_the_daemon(self):
        code, calls = self.route('org.mozilla.firefox')
        self.assertEqual(code, 0)
        self.assertEqual(calls[-1], 'daemon -m voicekey --control start')

    def test_listening_daemon_is_stopped_even_from_a_terminal(self):
        code, calls = self.route('com.mitchellh.ghostty', listening=True)
        self.assertEqual(code, 0)
        self.assertEqual(calls[-1], 'daemon -m voicekey --control stop')

    def test_daemon_not_running_is_reported(self):
        code, calls = self.route('org.mozilla.firefox', down=True)
        self.assertEqual(code, 1)
        self.assertTrue(any(call.startswith('notify ') and 'voicekey.service' in call for call in calls), calls)

    def test_daemon_refusal_reason_is_reported(self):
        code, calls = self.route('org.mozilla.firefox', busy=True)
        self.assertEqual(code, 1)
        self.assertTrue(any('Finish the current dictation' in call for call in calls), calls)
        self.assertFalse(any('voicekey.service' in call for call in calls), calls)

    def test_unknown_focus_refuses(self):
        code, calls = self.route('')
        self.assertEqual(code, 1)
        self.assertNotIn('daemon -m voicekey --control start', calls)
