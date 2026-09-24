import os
from pathlib import Path
import threading
import time
import unittest
from unittest.mock import Mock, patch

from voicekey.config import DictationConfig
from voicekey.focus import Focus, focused
from voicekey.shell_prompt import Process, PromptWindow, process, candidates
from voicekey.target import bind, ImeTarget, RefusedTarget, Outcome
from voicekey.session_target import SessionTarget


class PromptTests(unittest.TestCase):
    def setUp(self):
        self.addCleanup(patch.stopall)
        self.destination = Focus(7, 'com.mitchellh.ghostty', 100, '/work')
        self.tree = {100:(1,'ghostty'), 200:(100,'bash'), 300:(100,'bash'), 301:(300,'nvim')}
        self.shell = Process('bash', 'S', 200, 123, 200, 12345)
        self.info = patch('voicekey.shell_prompt.process', side_effect=lambda pid: self.shell).start()
        patch('voicekey.shell_prompt.working_directory', return_value='/work').start()
        patch('voicekey.nvim.process_tree', return_value=self.tree).start()
        patch('voicekey.nvim.runtime_dir', return_value=Path('/nonexistent-voicekey-tests')).start()
        self.focus = patch('voicekey.focus.focused', side_effect=lambda **kw: self.destination).start()
        self.rpc = patch('voicekey.nvim.call', side_effect=AssertionError('no registered editor should be contacted')).start()
        self.ime = Mock(activation=Mock(return_value=1), rebind=Mock(return_value=True), commit=Mock(return_value=True))

    def bind(self):
        return bind(self.ime, DictationConfig(), True)

    def test_directory_title_allows_shell_with_neovim_in_other_ghostty_window(self):
        target = self.bind()
        self.assertIsInstance(target, ImeTarget)
        self.assertIsInstance(target.window, PromptWindow)
        result = target.land('words', time.monotonic()+1, operation_id='op')
        self.assertEqual(result.outcome, Outcome.SUBMITTED)
        self.ime.commit.assert_called_once()
        self.rpc.assert_not_called()

    def test_command_unknown_and_unmatched_directory_titles_keep_neovim_guard(self):
        for title in ('bash', 'nvim notes', '/usr/bin/nvim notes', '❯ ~/work', '/unmatched', ''):
            self.destination = Focus(7,'com.mitchellh.ghostty',100,title)
            self.assertIsInstance(self.bind(), RefusedTarget, title)
        self.ime.commit.assert_not_called()

    def test_home_directory_title_is_expanded(self):
        self.destination = Focus(7,'com.mitchellh.ghostty',100,'~/work')
        with patch.dict(os.environ, HOME='/user'), patch('voicekey.shell_prompt.working_directory', return_value='/user/work'):
            self.assertIsInstance(self.bind(), ImeTarget)

    def test_foreground_job_suspended_shell_and_missing_tty_refuse(self):
        for shell in (Process('bash','S',200,123,201,12345),
                      Process('bash','T',200,123,200,12345),
                      Process('bash','S',200,0,200,12345),
                      Process('nvim','S',200,123,200,12345), None):
            self.shell = shell
            self.assertIsInstance(self.bind(), RefusedTarget)
        self.ime.commit.assert_not_called()

    def test_missing_pid_other_terminals_and_wrong_ancestry_do_not_infer_prompt(self):
        for d in (Focus(7,'com.mitchellh.ghostty',None,'/work'),
                  Focus(7,'com.mitchellh.ghostty',400,'/work'),
                  Focus(7,'foot',100,'/work')):
            self.assertEqual(candidates(d), {})

    def test_child_sharing_foreground_group_refuses_but_background_job_does_not(self):
        self.tree[201] = (200,'nvim')
        self.assertIsInstance(self.bind(), RefusedTarget)
        self.info.side_effect = lambda pid: self.shell if pid == 200 else Process('nvim','T',201,123,200,555)
        self.assertIsInstance(self.bind(), ImeTarget)

    def test_title_change_during_binding_refuses(self):
        original = self.destination
        self.focus.side_effect = [original, Focus(7,original.app_id,100,'nvim notes')]
        self.assertIsInstance(self.bind(), RefusedTarget)

    def test_delivery_checks_title_again_even_with_same_ime_activation(self):
        target = self.bind()
        self.destination = Focus(7,self.destination.app_id,100,'nvim notes')
        self.assertEqual(target.land('words', time.monotonic()+1, operation_id='op').outcome, Outcome.REFUSED)
        self.ime.commit.assert_not_called()

    def test_stale_title_cannot_authorize_delivery_when_matching_shell_has_foreground_job(self):
        target = self.bind()
        self.shell = Process('bash','S',200,123,301,12345)
        self.assertEqual(target.land('words', time.monotonic()+1, operation_id='op').outcome, Outcome.REFUSED)
        self.ime.commit.assert_not_called()

    def test_pid_reuse_does_not_authorize_old_binding(self):
        target = self.bind()
        self.shell = Process('bash','S',200,123,200,99999)
        self.assertEqual(target.land('words', time.monotonic()+1, operation_id='op').outcome, Outcome.REFUSED)
        self.ime.commit.assert_not_called()

    def test_multiple_shells_in_same_directory_are_usable_but_surface_identity_is_not_proven(self):
        self.tree[400] = (100,'bash')
        self.info.side_effect = lambda pid: (Process('bash','S',400,456,400,67890) if pid == 400 else self.shell)
        self.assertEqual(candidates(self.destination), {200:12345,400:67890})
        self.assertIsInstance(self.bind(), ImeTarget)
        # Accepted limitation: another surface can leave a matching stale cwd
        # title while this other shell remains at its prompt. No surface ID links them.

    def test_persistent_field_check_and_delivery_refuse_after_title_changes(self):
        target = self.bind()
        session = SessionTarget('session', target, Mock(snapshots=Mock(return_value=[])))
        self.assertEqual(session.field_issue(), '')
        self.destination = Focus(7,self.destination.app_id,100,'nvim')
        target.window._checked = 0
        self.assertIn('shell prompt changed', session.field_issue())
        result = session.deliver('u','words',time.monotonic()+1,'op','',None,threading.Event())
        self.assertEqual(result.outcome, Outcome.REFUSED)
        self.ime.commit.assert_not_called()


class EvidenceTests(unittest.TestCase):
    def test_proc_stat_parser_uses_kernel_group_tty_and_start_time(self):
        current = process(os.getpid())
        self.assertIsNotNone(current)
        self.assertEqual(current.group, os.getpgrp())
        self.assertGreater(current.started, 0)
        self.assertIsNone(process(0))

    def test_titles_are_collected_without_changing_window_identity(self):
        for compositor, payload in (
            ('niri', '{"id":7,"app_id":"ghostty","pid":100,"title":"directory"}'),
            ('sway', '{"id":7,"type":"con","focused":true,"app_id":"ghostty","pid":100,"name":"directory"}'),
            ('hyprland', '{"address":7,"class":"ghostty","pid":100,"title":"directory"}')):
            with patch('voicekey.focus.compositor', return_value=compositor), \
                    patch('voicekey.focus.subprocess.run', return_value=Mock(returncode=0,stdout=payload)):
                result = focused()
                self.assertEqual(result, Focus(7,'ghostty',100))
                self.assertEqual(result.title, 'directory')
                self.assertEqual(result, Focus(7,'ghostty',100,'other title'))

    def test_private_bash_pty_validates_idle_shell_and_rejects_running_job(self):
        import select
        import shutil
        import subprocess
        import tempfile
        if not shutil.which('setsid'):
            self.skipTest('setsid unavailable')
        master, slave = os.openpty()
        self.addCleanup(os.close, master)
        with tempfile.TemporaryDirectory() as directory:
            shell = subprocess.Popen(['setsid','--ctty','bash','--norc','--noprofile','-i'],
                stdin=slave,stdout=slave,stderr=slave,cwd=directory,
                env=dict(os.environ,HOME=directory))
            os.close(slave)
            try:
                destination = Focus(7,'com.mitchellh.ghostty',os.getpid(),directory)
                deadline = time.monotonic()+3
                while not candidates(destination) and time.monotonic() < deadline:
                    if select.select([master],[],[],.05)[0]:
                        os.read(master,65536)
                self.assertIn(shell.pid,candidates(destination))
                os.write(master,b'sleep 1\n')
                deadline = time.monotonic()+2
                while candidates(destination) and time.monotonic() < deadline:
                    time.sleep(.01)
                self.assertFalse(candidates(destination), 'stale title must not validate a shell with a foreground job')
                os.write(master,b'\x03exit\n')
            finally:
                try:
                    shell.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    shell.kill()
                    shell.wait(timeout=2)
