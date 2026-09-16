"""Only temporary Python fixtures are executed; never an installed agent."""
import contextlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock, patch

from voicekey import agent
from voicekey.config import AgentConfig, Config, ConfigError, _validate


class CommandAgentTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def fixture(self, code, **kwargs):
        script = self.root / 'fixture.py'
        script.write_text(code)
        return AgentConfig(target='command', command=[sys.executable, str(script)],
                           working_directory=str(self.root), **kwargs)

    def test_exact_stdin_literal_arguments_cwd_and_no_output_logging(self):
        cfg = self.fixture('import sys, pathlib, json, os\n'
                           'text = sys.stdin.buffer.read()\n'
                           'pathlib.Path("input").write_bytes(text)\n'
                           'pathlib.Path("args").write_text(json.dumps(sys.argv[1:]))\n'
                           'print(text); print(text, file=sys.stderr)\n')
        literal = '$(touch unexpected); {text} `whoami`'
        cfg.command.append(literal)
        prompt = 'Private café\n!run /new {!secret} $(touch bad)\x00\n'
        with self.assertLogs('voicekey.agent', level='INFO') as logs:
            self.assertEqual(agent.send_prompt(cfg, prompt), 'Command')
        self.assertEqual((self.root / 'input').read_bytes(), prompt.encode())
        self.assertEqual(json.loads((self.root / 'args').read_text()), [literal])
        self.assertNotIn(prompt, '\n'.join(logs.output))
        self.assertFalse((self.root / 'unexpected').exists())

    def test_failure_does_not_expose_echoed_transcript_and_can_retry(self):
        cfg = self.fixture('import sys\ns = sys.stdin.read()\nprint(s, file=sys.stderr)\nsys.exit(7)\n')
        with self.assertRaisesRegex(agent.AgentError, '^agent command exited with status 7$'):
            agent.send_prompt(cfg, 'private transcript')
        cfg = self.fixture('import sys\nsys.stdin.read()\n')
        agent.send_prompt(cfg, 'next prompt')

    def test_deadlines_interrupt_blocked_stdin_and_reap_process(self):
        cfg = self.fixture('import time\ntime.sleep(30)\n', command_timeout=.15)
        real_popen = subprocess.Popen
        for budget in (None, .08):
            children = []
            def spawn(*args, **kwargs):
                child = real_popen(*args, **kwargs)
                children.append(child)
                return child
            with self.subTest(budget=budget), patch.object(agent.subprocess, 'Popen', side_effect=spawn):
                started = time.monotonic()
                deadline = None if budget is None else started + budget
                with self.assertRaisesRegex(agent.AgentError, 'timed out'):
                    agent.send_prompt(cfg, 'x' * 1000000, deadline=deadline)
                self.assertLess(time.monotonic() - started, 2)
                self.assertEqual(len(children), 1)
                self.assertIsNotNone(children[0].returncode)
                with self.assertRaises(ChildProcessError):
                    os.waitpid(children[0].pid, os.WNOHANG)

    def test_large_input_survives_slow_reader(self):
        cfg = self.fixture('import sys, time, pathlib\ntime.sleep(.12)\n'
                           'pathlib.Path("input").write_bytes(sys.stdin.buffer.read())\n')
        prompt = 'café\n' * 100000
        agent.send_prompt(cfg, prompt)
        self.assertEqual((self.root / 'input').read_bytes(), prompt.encode())

    def test_cancellation_kills_child_and_descendant(self):
        cfg = self.fixture('import subprocess, sys, pathlib, time, os\n'
                           'child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])\n'
                           'pathlib.Path("pids").write_text(f"{os.getpid()} {child.pid}")\n'
                           'time.sleep(30)\n')
        cancelled = threading.Event()
        errors = []
        def run():
            try:
                agent.send_prompt(cfg, 'secret', cancelled=cancelled)
            except agent.AgentError as exc:
                errors.append(str(exc))
        thread = threading.Thread(target=run)
        thread.start()
        try:
            expires = time.monotonic() + 3
            while not (self.root / 'pids').exists() and time.monotonic() < expires:
                time.sleep(.01)
            self.assertTrue((self.root / 'pids').exists())
        finally:
            cancelled.set()
            thread.join(3)
        self.assertFalse(thread.is_alive())
        self.assertEqual(errors, ['agent command cancelled'])
        for pid in (self.root / 'pids').read_text().split():
            stat = Path('/proc') / pid / 'stat'
            expires = time.monotonic() + 2
            while stat.exists() and stat.read_text().split()[2] != 'Z' and time.monotonic() < expires:
                time.sleep(.01)
            self.assertTrue(not stat.exists() or stat.read_text().split()[2] == 'Z')

    def test_pre_cancelled_or_expired_never_spawns(self):
        cfg = self.fixture('raise RuntimeError("must not execute")')
        cancelled = threading.Event()
        cancelled.set()
        for options in ({'cancelled': cancelled}, {'deadline': time.monotonic() - 1}):
            with patch.object(agent.subprocess, 'Popen') as spawn:
                with self.assertRaises(agent.AgentError):
                    agent.send_prompt(cfg, 'secret', **options)
                spawn.assert_not_called()

    def test_check_and_missing_paths_never_execute(self):
        cfg = self.fixture('raise RuntimeError("must not execute")')
        with patch.object(agent.subprocess, 'Popen') as spawn:
            self.assertIsNone(agent.check_target(cfg))
            cfg.working_directory = str(self.root / 'absent')
            self.assertIn('working directory', agent.check_target(cfg))
            with self.assertRaises(agent.AgentError):
                agent.send_prompt(cfg, 'secret')
            cfg.command = [str(self.root / 'missing')]
            self.assertIn('executable', agent.check_target(cfg))
            spawn.assert_not_called()

    def test_config_validation(self):
        for value in ([], 'echo', [1], [''], ['echo', '\x00'], [True]):
            cfg = Config(agent=AgentConfig(target='command', command=value))
            with self.subTest(value=value), self.assertRaises(ConfigError):
                _validate(cfg)
        cfg = Config(agent=AgentConfig(target='command', command=['~/bin/fixture', '', '{text}']))
        _validate(cfg)
        self.assertTrue(os.path.isabs(cfg.agent.command[0]))
        self.assertEqual(cfg.agent.command[1:], ['', '{text}'])
        cfg.agent.transport = 'ssh-over-tailscale'
        with self.assertRaisesRegex(ConfigError, 'requires.*local'):
            _validate(cfg)

    def test_cli_check_has_no_hermes_dependencies_and_does_not_launch_command(self):
        from voicekey.__main__ import check
        cfg = Config(agent=self.fixture('raise RuntimeError("must not execute")'))
        cfg.streaming.model_dir = ''
        cfg.dictation.ime = False
        device = Mock(name='keyboard')
        with contextlib.ExitStack() as stack:
            for name in ('voicekey.daemon.fix_environment', 'voicekey.backends.create_backend',
                         'voicekey.segment.SpeechDetector'):
                stack.enter_context(patch(name))
            stack.enter_context(patch('voicekey.focus.compositor', return_value=None))
            stack.enter_context(patch('voicekey.listener.all_event_devices', return_value=['fake']))
            stack.enter_context(patch('voicekey.listener._supports_any_key', return_value=True))
            stack.enter_context(patch('evdev.InputDevice', return_value=device))
            which = stack.enter_context(patch('shutil.which', side_effect=lambda c: None if c in {'hermes', 'tmux', 'ghostty', 'systemd-run'} else c))
            spawn = stack.enter_context(patch.object(agent.subprocess, 'Popen'))
            stack.enter_context(contextlib.redirect_stdout(io.StringIO()))
            stack.enter_context(contextlib.redirect_stderr(io.StringIO()))
            self.assertEqual(check(cfg), 0)
            cfg.agent.working_directory = str(self.root / 'absent')
            self.assertEqual(check(cfg), 3)
            spawn.assert_not_called()
            self.assertFalse({'hermes', 'tmux', 'ghostty', 'systemd-run'} & {c.args[0] for c in which.call_args_list})
