import glob
import os
import shutil
import subprocess
import unittest
from pathlib import Path
from unittest.mock import patch

from voicekey import emacs


def result(stdout='"ok"', returncode=0, stderr=''):
    return subprocess.CompletedProcess([], returncode, stdout, stderr)


class ProtocolTests(unittest.TestCase):
    @patch('voicekey.emacs.subprocess.run', return_value=result('"\\n"'))
    def test_pin_acknowledgement_parses_spacing_and_uses_fresh_id(self, run):
        first, second = emacs.pin(), emacs.pin()
        self.assertTrue(first.valid)
        self.assertEqual(first.before, '\n')
        self.assertNotEqual(first.id, second.id)
        self.assertIn('voicekey--pin', run.call_args.args[0][2])
        self.assertEqual(run.call_args.kwargs['timeout'], emacs.PIN_TIMEOUT)

    @patch('voicekey.emacs.subprocess.run', side_effect=subprocess.TimeoutExpired('emacsclient', 1))
    def test_timed_out_pin_is_invalid_and_cannot_authorize_insert(self, run):
        pinned = emacs.pin()
        self.assertFalse(pinned.valid)
        pending = emacs.PendingPin()
        pending.before(1)
        self.assertFalse(pending.valid)

    @patch('voicekey.emacs.subprocess.run', return_value=result())
    def test_insert_quotes_text_and_carries_expiry_operation_and_permit(self, run):
        emacs.insert('say "hi" \\ bye', 'pin', 0.3, operation_id='op', permit='/tmp/permit')
        form = run.call_args.args[0][2]
        self.assertIn('say \\"hi\\" \\\\ bye', form)
        self.assertIn('"pin" "op"', form)
        self.assertIn('"/tmp/permit"', form)
        self.assertEqual(run.call_args.kwargs['timeout'], 0.3)

    def test_only_explicit_refusal_is_safe_to_copy(self):
        with patch('voicekey.emacs.subprocess.run', return_value=result('"refused: read-only"')):
            with self.assertRaises(emacs.EmacsRefused):
                emacs.insert('x', 'pin')
        for reply in (result('"unknown: hook failed"'), result('*ERROR*: hook failed', 1), result('nil')):
            with patch('voicekey.emacs.subprocess.run', return_value=reply):
                with self.assertRaises(emacs.EmacsError) as error:
                    emacs.insert('x', 'pin')
                self.assertNotIsInstance(error.exception, emacs.EmacsRefused)

    @patch('voicekey.emacs.subprocess.run')
    def test_expired_before_submission_starts_no_process(self, run):
        with self.assertRaises(emacs.EmacsRefused):
            emacs.insert('x', 'pin', timeout=0)
        run.assert_not_called()


@unittest.skipUnless(shutil.which('emacs'), 'batch Emacs unavailable')
class EditorTests(unittest.TestCase):
    def test_actual_editor_transactions(self):
        root = Path(__file__).resolve().parents[1]
        command = ['emacs', '--batch', '-Q', '-L', str(root / 'voicekey')]
        paths = []
        for pattern in ('~/.emacs.d/elpa/evil-*', '~/.emacs.d/elpa/goto-chg-*',
                        '/usr/share/emacs/site-lisp/elpa/evil-*', '/usr/share/emacs/site-lisp/elpa/goto-chg-*'):
            paths.extend(glob.glob(os.path.expanduser(pattern)))
        if os.environ.get('EVIL_LOAD_PATH'):
            paths.extend(os.environ['EVIL_LOAD_PATH'].split(os.pathsep))
        for path in paths:
            if os.path.isdir(path):
                command += ['-L', path]
        command += ['-l', str(root / 'tests/emacs-tests.el'), '-f', 'ert-run-tests-batch-and-exit']
        run = subprocess.run(command, capture_output=True, text=True, timeout=20)
        self.assertEqual(run.returncode, 0, run.stdout + run.stderr)

@unittest.skipUnless(shutil.which('emacs') and shutil.which('emacsclient'), 'Emacs unavailable')
class CommandLoopSpike(unittest.TestCase):
    def test_server_evaluation_does_not_rebind_the_last_user_buffer(self):
        import ast
        import tempfile
        import time
        with tempfile.TemporaryDirectory(prefix='voicekey-emacs-test-') as directory:
            socket_path = directory + '/private'
            ready = Path(directory) / 'ready'
            script = (f"(progn (require 'server) (setq server-socket-dir {emacs._lisp_string(directory)} "
                      f"server-name \"private\") (server-start) "
                      f"(load {emacs._lisp_string(emacs.LIBRARY)} nil t) "
                      "(switch-to-buffer \"user-target\") (voicekey-tracking-mode 1) "
                      "(setq voicekey-test-commands 0) "
                      "(add-hook 'post-command-hook (lambda () (setq voicekey-test-commands (1+ voicekey-test-commands)))) "
                      f"(with-temp-file {emacs._lisp_string(str(ready))} (insert \"ready\")) "
                      "(while t (accept-process-output nil 0.02)))")
            process = subprocess.Popen(['emacs', '--batch', '-Q', '--eval', script],
                                       stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            try:
                deadline = time.monotonic() + 5
                while not ready.exists() and process.poll() is None and time.monotonic() < deadline:
                    time.sleep(0.01)
                self.assertTrue(ready.exists(), 'private Emacs server did not start')
                form = ('(progn (switch-to-buffer "agent-buffer") '
                        '(voicekey--pin "pin" (+ (float-time) 1)) '
                        '(format "%s:%s:%d" (buffer-name) '
                        '(buffer-name (cadr (assoc "pin" voicekey--pins))) voicekey-test-commands))')
                response = subprocess.run(['emacsclient', '-s', socket_path, '-e', form],
                                          capture_output=True, text=True, timeout=3)
                self.assertEqual(response.returncode, 0, response.stderr)
                self.assertEqual(ast.literal_eval(response.stdout.strip()), 'agent-buffer:user-target:0')
            finally:
                process.terminate()
                try:
                    process.communicate(timeout=2)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.communicate()
