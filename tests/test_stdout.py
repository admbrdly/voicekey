import contextlib
import io
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import threading
import unittest
import wave
from unittest.mock import patch
import unittest.mock

import numpy as np

from voicekey import stdout
from tests.test_client_capture import CaptureHarness


class StdoutTests(CaptureHarness):
    def test_capture_uses_daemon_and_prints_recording_contract(self):
        self.controller()
        output, errors = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors):
            self.assertEqual(stdout.capture(seconds=.1, path=self.path), 0)
        self.assertEqual(output.getvalue(), 'Hello from the daemon.')
        self.assertIn('Recording;', errors.getvalue())
        self.assertIn('Transcribing;', errors.getvalue())
        self.bind.assert_not_called()
        self.copy.assert_not_called()

    def test_failed_stdout_never_copies(self):
        self.controller()
        with patch('sys.stdout.write', side_effect=BrokenPipeError), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(stdout.capture(seconds=.1, path=self.path), 1)
        self.copy.assert_not_called()

    def test_paced_wav_is_recorded_by_daemon(self):
        self.controller()
        wav = Path(self.tmp.name) / 'input.wav'
        with wave.open(str(wav), 'wb') as handle:
            handle.setparams((1, 2, 16000, 0, 'NONE', 'not compressed'))
            handle.writeframes(b'\0\0' * 8000)
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(stdout.capture(wav=str(wav), path=self.path), 0)
        self.assertEqual(len(self.daemon.backend.transcribe.call_args.args[0]), 8000)

    def cli(self, *args):
        env = {**os.environ, 'XDG_RUNTIME_DIR': self.tmp.name}
        process = subprocess.Popen([sys.executable, '-m', 'voicekey', '--capture-to-stdout', *args],
                                   env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        def close():
            if process.poll() is None:
                process.kill()
            process.communicate(timeout=3)
        self.addCleanup(close)
        return process

    def test_cli_sigint_finishes(self):
        self.controller()
        process = self.cli()
        self.assertIn('Recording;', process.stderr.readline())
        process.send_signal(signal.SIGINT)
        output, errors = process.communicate(timeout=5)
        self.assertEqual(process.returncode, 0, errors)
        self.assertEqual(output, 'Hello from the daemon.')

    def test_cli_sigterm_discards(self):
        self.controller()
        process = self.cli()
        self.assertIn('Recording;', process.stderr.readline())
        process.send_signal(signal.SIGTERM)
        output, errors = process.communicate(timeout=5)
        self.assertEqual(process.returncode, 130, errors)
        self.assertEqual(output, '')
        self.daemon.backend.transcribe.assert_not_called()

    def test_cli_global_stop_finishes_stdout_capture_without_signalling_client(self):
        entered, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)
        def transcribe(samples):
            entered.set()
            release.wait(3)
            return 'Hello from the daemon.'
        self.daemon.backend.transcribe.side_effect = transcribe
        self.controller()
        process = self.cli('--client-name', 'Neovim')
        self.assertIn('Recording;', process.stderr.readline())
        env = {**os.environ, 'XDG_RUNTIME_DIR': self.tmp.name}
        # Match the route script's status -> stop sequence, using another process.
        for command in ('status', 'stop'):
            result = subprocess.run([sys.executable, '-m', 'voicekey', '--control', command],
                                    env=env, capture_output=True, text=True, timeout=5)
            self.assertEqual(result.returncode, 0, result.stderr)
            reply = json.loads(result.stdout)
            if command == 'status':
                self.assertFalse(reply['error'])  # Status carries the daemon's error string.
                self.assertTrue(reply['listening'])
                self.assertEqual(reply['destination_name'], 'Neovim')
            else:
                self.assertIsNone(reply['error'])
        self.assertTrue(entered.wait(1))
        self.assertIn('Transcribing;', process.stderr.readline())
        self.assertIsNone(process.poll())
        release.set()
        output, errors = process.communicate(timeout=5)
        self.assertEqual(process.returncode, 0, errors)
        self.assertEqual(output, 'Hello from the daemon.')

    def test_client_label_is_omitted_for_older_version_one_daemons(self):
        with patch('voicekey.control.CAPABILITIES', []), \
                patch.object(self.daemon, 'command', wraps=self.daemon.command) as command, \
                contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            self.controller()
            self.assertEqual(stdout.capture(seconds=.1, path=self.path, client_name='Neovim'), 0)
            start = next(call for call in command.call_args_list if call.args[0] == 'capture-start')
            self.assertNotIn('client_name', start.kwargs['args'])

    def test_preview_lines_go_to_stderr_and_stdout_keeps_only_the_final_text(self):
        class Stream:
            def feed(self, frame):
                return 'Hello\nfrom "live"'
            def finish(self):
                return 'Hello\nfrom "live"'
        self.daemon.streaming = unittest.mock.Mock(session=Stream)
        original = self.daemon.command
        def command(name, **kwargs):
            result = original(name, **kwargs)
            if name == 'capture-start':
                self.daemon.client_capture.tick()
                self.daemon.client_capture.recorder.on_frame(np.zeros(512, dtype=np.float32))
            return result
        output, errors = io.StringIO(), io.StringIO()
        with patch.object(self.daemon, 'command', side_effect=command), \
                contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors):
            self.controller()
            self.assertEqual(stdout.capture(seconds=.1, path=self.path, preview=True), 0)
        self.assertEqual(output.getvalue(), 'Hello from the daemon.')
        previews = [line for line in errors.getvalue().splitlines() if line.startswith('Preview; ')]
        self.assertTrue(previews, errors.getvalue())
        self.assertEqual(json.loads(previews[0][len('Preview; '):]), 'Hello\nfrom "live"')

    def test_preview_is_not_requested_from_daemons_without_the_capability(self):
        with patch('voicekey.control.CAPABILITIES', ['capture-client-name']), \
                patch.object(self.daemon, 'command', wraps=self.daemon.command) as command, \
                contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            self.controller()
            self.assertEqual(stdout.capture(seconds=.1, path=self.path, preview=True), 0)
            start = next(call for call in command.call_args_list if call.args[0] == 'capture-start')
            self.assertNotIn('preview', start.kwargs['args'])

    def test_preview_flag_requires_capture_to_stdout(self):
        result = subprocess.run([sys.executable, '-m', 'voicekey', '--preview', '--last'],
                                capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 2)
        self.assertIn('--preview requires --capture-to-stdout', result.stderr)

    def test_cli_no_models_imported_and_no_config_required(self):
        self.controller()
        script = '''
import sys
class DenyModels:
    def find_spec(self, fullname, *args):
        if fullname in ('voicekey.backends', 'voicekey.daemon', 'voicekey.pipeline',
                        'voicekey.polish', 'voicekey.recorder', 'voicekey.config'):
            raise AssertionError('client imported ' + fullname)
sys.meta_path.insert(0, DenyModels())
from voicekey.__main__ import main
sys.argv = ['voicekey', '--capture-to-stdout', '--seconds', '.1']
raise SystemExit(main())
'''
        result = subprocess.run([sys.executable, '-c', script], capture_output=True, text=True, timeout=5,
                                env={**os.environ, 'XDG_RUNTIME_DIR': self.tmp.name})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, 'Hello from the daemon.')

    def test_daemon_absent_fails_without_fallback_and_restores_signals(self):
        handlers = {sig: signal.getsignal(sig) for sig in (signal.SIGINT, signal.SIGTERM)}
        with contextlib.redirect_stdout(io.StringIO()) as output, contextlib.redirect_stderr(io.StringIO()) as errors:
            self.assertEqual(stdout.capture(path=self.path.parent / 'absent.sock'), 1)
        self.assertEqual(output.getvalue(), '')
        self.assertIn('voicekey capture:', errors.getvalue())
        for sig, handler in handlers.items():
            self.assertEqual(signal.getsignal(sig), handler)

    def test_refusal_propagates_to_stderr(self):
        self.daemon.pipeline._accepting = False
        self.controller()
        with contextlib.redirect_stderr(io.StringIO()) as errors:
            self.assertEqual(stdout.capture(path=self.path), 1)
        self.assertIn('Capture unavailable', errors.getvalue())

    def test_old_daemon_requires_restart(self):
        # Simulate a pre-versioning server, without sending it any capture command.
        import socket
        other_path = str(Path(self.tmp.name) / 'old.sock')
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as listener:
            listener.bind(other_path)
            listener.listen()
            def server():
                client, _ = listener.accept()
                with client:
                    client.sendall(b'{"type":"status","state":"idle"}\n')
                    self.assertEqual(client.recv(1), b'')
            thread = threading.Thread(target=server)
            thread.start()
            with contextlib.redirect_stderr(io.StringIO()) as errors:
                self.assertEqual(stdout.capture(path=other_path), 1)
            thread.join(2)
            self.assertIn('restart voicekey.service', errors.getvalue())
