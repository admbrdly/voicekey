import json
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
        command = ['nvim', '--headless', '-u', 'NONE', '-i', 'NONE', '-l', str(root / 'tests/nvim-tests.lua')]
        run = subprocess.run(command, capture_output=True, text=True, timeout=60)
        self.assertEqual(run.returncode, 0, run.stdout + run.stderr)


@unittest.skipUnless(shutil.which('bash'), 'bash unavailable')
class BashTests(unittest.TestCase):
    """contrib/bash/voicekey.bash in a bash -i without a terminal."""

    def bash(self, script, session=False, env=None):
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        (tmp / 'voicekey').mkdir(mode=0o700)
        if session:
            (tmp / 'voicekey' / 'shell-session').write_text('7\n')
        calls = tmp / 'calls.log'
        python = tmp / 'python'
        # Status reports "finishing" once after stop, then "idle".
        python.write_text('#!/bin/sh\necho "$*" >> "$CALLS"\n'
                          'case "$*" in *status) if [ -e "$CALLS.seen" ]; then echo \'{"state": "idle"}\';'
                          ' else touch "$CALLS.seen"; echo \'{"state": "finishing"}\'; fi ;; esac\n')
        python.chmod(0o755)
        root = Path(__file__).resolve().parents[1]
        source = f'source {root / "contrib/bash/voicekey.bash"} 2>/dev/null\n'
        run = subprocess.run(['bash', '--norc', '--noprofile', '-i'], input=source + script,
                             capture_output=True, text=True, timeout=20,
                             env={**os.environ, 'XDG_RUNTIME_DIR': str(tmp), 'HOME': '/home/u',
                                  'VOICEKEY_PYTHON': str(python), 'CALLS': str(calls), **(env or {})})
        log = calls.read_text().splitlines() if calls.exists() else []
        return run.stdout, log, (tmp / 'voicekey' / 'shell-session').exists()

    def test_title_is_marked_only_at_the_prompt(self):
        out, _, _ = self.bash('cd /tmp; __voicekey_prompt_title; echo; history -s "less notes.md"; __voicekey_running; echo\n'
                              'PS1="$ "; __voicekey_prompt; __voicekey_prompt; printf "[%s]\\n" "$PS1"\n')
        self.assertIn('\x1b]2;❯ /tmp\x07', out)
        self.assertIn('\x1b]2;less notes.md\x07', out)
        self.assertIn('[\\[$(__voicekey_prompt_title)\\]$ ]', out, 'the PS1 prefix is added once')

    def test_ghostty_title_feature_is_turned_off(self):
        out, _, _ = self.bash('printf "[%s]\\n" "$GHOSTTY_SHELL_FEATURES"\n', env={'GHOSTTY_SHELL_FEATURES': 'cursor,title,sudo'})
        self.assertIn('[cursor,,sudo]', out)

    def test_enter_stops_prompt_dictation_and_waits_for_it(self):
        _, calls, left = self.bash('__voicekey_running\n', session=True)
        self.assertEqual(calls[0], '-m voicekey --control stop')
        self.assertEqual(calls[1:], ['-m voicekey --control status'] * 2, 'polls until no longer finishing')
        self.assertFalse(left)

    def test_enter_without_prompt_dictation_touches_nothing(self):
        _, calls, _ = self.bash('__voicekey_running\n')
        self.assertEqual(calls, [])


@unittest.skipUnless(shutil.which('jq'), 'jq unavailable')
class ClaudeHookTests(unittest.TestCase):
    """contrib/claude-code/voicekey-claude-hook with a stand-in daemon."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.state = self.tmp / 'voicekey'
        self.state.mkdir(mode=0o700)
        self.calls = self.tmp / 'calls.log'
        python = self.tmp / 'python'
        # Status reports "finishing" once after stop, then "idle".
        python.write_text('#!/bin/sh\necho "$*" >> "$CALLS"\n'
                          'case "$*" in *status) if [ -e "$CALLS.seen" ]; then echo \'{"state": "idle"}\';'
                          ' else touch "$CALLS.seen"; echo \'{"state": "finishing"}\'; fi ;; esac\n')
        python.chmod(0o755)
        self.python = python

    def hook(self, event, tool=None, dictating=False):
        if dictating:
            (self.state / 'tui-session').write_text('7\n')
        self.calls.unlink(missing_ok=True)
        Path(f'{self.calls}.seen').unlink(missing_ok=True)
        payload = {'hook_event_name': event, 'session_id': 'abc-123'}
        if tool:
            payload['tool_name'] = tool
        root = Path(__file__).resolve().parents[1]
        run = subprocess.run([str(root / 'contrib/claude-code/voicekey-claude-hook')], input=json.dumps(payload),
                             capture_output=True, text=True, timeout=20,
                             env={**os.environ, 'XDG_RUNTIME_DIR': str(self.tmp),
                                  'VOICEKEY_PYTHON': str(self.python), 'CALLS': str(self.calls)})
        self.assertEqual((run.returncode, run.stdout), (0, ''), 'never influences Claude Code')
        return self.calls.read_text().splitlines() if self.calls.exists() else []

    def dialog_open(self):
        return (self.state / 'claude-dialog' / 'abc-123').exists()

    def test_submit_stops_terminal_dictation_and_waits(self):
        calls = self.hook('UserPromptSubmit', dictating=True)
        self.assertEqual(calls, ['-m voicekey --control stop'] + ['-m voicekey --control status'] * 2)
        self.assertFalse((self.state / 'tui-session').exists())

    def test_hooks_without_terminal_dictation_leave_the_daemon_alone(self):
        for event, tool in (('UserPromptSubmit', None), ('PermissionRequest', 'Bash'), ('PreToolUse', 'Bash')):
            with self.subTest(event=event):
                self.assertEqual(self.hook(event, tool), [])

    def test_dialogs_stop_dictation_and_stay_marked_until_resolved(self):
        for event, tool in (('PermissionRequest', 'Bash'), ('PreToolUse', 'AskUserQuestion'),
                            ('PreToolUse', 'ExitPlanMode')):
            with self.subTest(event=event, tool=tool):
                calls = self.hook(event, tool, dictating=True)
                self.assertEqual(calls[0], '-m voicekey --control stop')
                self.assertTrue(self.dialog_open())
                self.hook('PostToolUse', tool)
                self.assertFalse(self.dialog_open())

    def test_dialog_marks_clear_on_every_resolution(self):
        for event, tool in (('PreToolUse', 'Read'), ('PostToolUseFailure', 'Bash'), ('PermissionDenied', 'Bash'),
                            ('Stop', None), ('SessionEnd', None), ('UserPromptSubmit', None)):
            with self.subTest(event=event):
                self.hook('PermissionRequest', 'Bash')
                self.assertTrue(self.dialog_open())
                self.hook(event, tool)
                self.assertFalse(self.dialog_open())


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
        self.stub('niri', 'printf \'{"id": %s, "app_id": "%s", "title": "%s"}\\n\' '
                  '"${FOCUSED_ID:-7}" "$FOCUSED_APP" "$FOCUSED_TITLE"')
        self.stub('notify-send', 'echo "notify $*" >> "$CALLS"')
        self.stub('python', 'echo "daemon $*" >> "$CALLS"\n'
                  '[ "$*" = "-m voicekey --control status" ] && printf \'{"listening": %s}\\n\' "$LISTENING"\n'
                  '[ -n "$DAEMON_DOWN" ] && exit 1\n'
                  'exit 0')

    def stub(self, name, body):
        path = self.bin / name
        path.write_text('#!/bin/sh\n' + body + '\n')
        path.chmod(0o755)

    def route(self, app, listening=False, title='~/notes', window=7, down=False):
        env = {'DAEMON_DOWN': '1' if down else '', 'FOCUSED_TITLE': title, 'FOCUSED_ID': str(window), **os.environ, 'PATH': f'{self.bin}:{os.environ["PATH"]}', 'XDG_RUNTIME_DIR': str(self.runtime),
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
        process = subprocess.Popen(['nvim', '--headless', '-u', 'NONE', '-i', 'NONE', '--listen', str(socket),
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

    def test_shell_prompt_starts_the_daemon_and_marks_the_session(self):
        code, calls = self.route('com.mitchellh.ghostty', title='❯ ~/notes')
        self.assertEqual(code, 0)
        self.assertEqual(calls[-1], 'daemon -m voicekey --control start')
        self.assertEqual((self.runtime / 'voicekey' / 'shell-session').read_text(), '7\n')

    def test_stopping_clears_the_shell_session(self):
        (self.runtime / 'voicekey' / 'shell-session').write_text('7\n')
        code, calls = self.route('org.mozilla.firefox', listening=True)
        self.assertEqual(calls[-1], 'daemon -m voicekey --control stop')
        self.assertFalse((self.runtime / 'voicekey' / 'shell-session').exists())

    def test_idle_claude_code_and_codex_start_the_daemon(self):
        for title in ('✳ Mail agent', '◐ Mail agent', '◒ Voicekey review', 'codex | voicekey-nvim'):
            with self.subTest(title=title):
                code, calls = self.route('com.mitchellh.ghostty', title=title)
                self.assertEqual(code, 0)
                self.assertEqual(calls[-1], 'daemon -m voicekey --control start')
                self.assertTrue((self.runtime / 'voicekey' / 'tui-session').exists())
                self.assertFalse((self.runtime / 'voicekey' / 'shell-session').exists())

    def test_working_claude_code_refuses(self):
        for title in ('✶ Mail agent', 'codexfoo', 'codex ⠋ voicekey-nvim', 'codex', 'codex --model o5', 'codex | a | b', 'adam'):
            with self.subTest(title=title):
                code, calls = self.route('com.mitchellh.ghostty', title=title)
                self.assertEqual(code, 1)
                self.assertNotIn('daemon -m voicekey --control start', calls)

    def test_claude_code_with_an_open_dialog_refuses(self):
        dialogs = self.runtime / 'voicekey' / 'claude-dialog'
        dialogs.mkdir()
        (dialogs / 'other-session').touch()
        code, calls = self.route('com.mitchellh.ghostty', title='◑ Mail agent')
        self.assertEqual(code, 1)
        self.assertNotIn('daemon -m voicekey --control start', calls)
        (dialogs / 'other-session').unlink()
        code, calls = self.route('com.mitchellh.ghostty', title='◑ Mail agent')
        self.assertEqual(code, 0)

    def test_terminal_program_without_prompt_mark_refuses(self):
        code, calls = self.route('com.mitchellh.ghostty', title='htop')
        self.assertEqual(code, 1)
        self.assertNotIn('daemon -m voicekey --control start', calls)

    def test_unknown_focus_refuses(self):
        code, calls = self.route('')
        self.assertEqual(code, 1)
        self.assertNotIn('daemon -m voicekey --control start', calls)
