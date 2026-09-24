"""Real isolated Neovim RPC with fake desktop/process/audio evidence."""
import json
import os
from pathlib import Path
import subprocess
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock, patch

from voicekey import nvim
from voicekey.config import DictationConfig
from voicekey.focus import Focus
from voicekey.nvim_target import NeovimTarget
from voicekey.target import bind, EmacsTarget, ImeTarget, RefusedTarget, Outcome, Window
from tests.test_nvim import nvim_supported
from tests.test_pipeline import wait_for

ROOT = Path(__file__).resolve().parents[1]


class Editor:
    def __init__(self, case, runtime, number=1):
        self.socket = str(Path(runtime) / f'nvim-{number}.sock')
        env = dict(os.environ, XDG_RUNTIME_DIR=str(runtime), NVIM_LOG_FILE=str(Path(runtime)/'nvim.log'))
        self.process = subprocess.Popen(['nvim', '--headless', '-u', 'NONE', '-i', 'NONE',
            '--listen', self.socket, '--cmd', f'set rtp+={ROOT}/contrib/nvim',
            '-c', 'runtime plugin/voicekey.lua'], env=env,
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        case.addCleanup(self.close)
        wait_for(lambda: Path(self.socket).exists())
        self.lua('vim.cmd.doautocmd("FocusGained")')

    @property
    def record(self):
        return {'pid': self.process.pid, 'server': self.socket}

    def close(self):
        if self.process.poll() is None:
            self.process.kill()
        self.process.wait(timeout=5)

    def lua(self, code):
        if ';' in code or code.startswith('local '):
            code = '(function() ' + code + ' end)()'
        expression = 'luaeval(' + "'" + code.replace("'", "''") + "')"
        run = subprocess.run(['nvim', '--server', self.socket, '--remote-expr', expression],
                             capture_output=True, text=True, timeout=3)
        if run.returncode:
            raise AssertionError(run.stderr)
        return run.stdout.strip()

    def text(self):
        return json.loads(self.lua('vim.json.encode(vim.api.nvim_buf_get_lines(0, 0, -1, false))'))

    def target(self):
        return NeovimTarget(Window(7, True, 100), 'com.mitchellh.ghostty', self.record)


@unittest.skipUnless(nvim_supported(), 'Neovim 0.10+ required')
class TargetTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.addCleanup(patch.stopall)
        patch.dict(os.environ, XDG_RUNTIME_DIR=self.tmp.name).start()
        self.editor = Editor(self, self.tmp.name)
        self.tree = {100: (1, 'ghostty'), self.editor.process.pid: (100, 'nvim')}
        patch('voicekey.nvim.process_tree', side_effect=lambda: self.tree).start()
        self.focus = patch('voicekey.target.focus.focused', return_value=Focus(7, 'com.mitchellh.ghostty', 100)).start()
        self.ime = Mock(activation=Mock(return_value=1), rebind=Mock(return_value=True))
        self.ime.before_cursor.return_value = ''

    def bind(self):
        return bind(self.ime, DictationConfig(), True)

    def land(self, target, text='Hello', operation='operation'):
        return target.land(text, time.monotonic()+1, operation_id=operation)

    def test_resolver_editor_shell_emacs_and_other_app(self):
        target = self.bind()
        self.assertIsInstance(target, NeovimTarget)
        self.assertEqual(target.application_name, 'Neovim')
        self.assertEqual(self.land(target).outcome, Outcome.CONFIRMED)
        self.assertEqual(self.editor.text(), ['Hello'])
        self.ime.preedit.assert_not_called()
        self.ime.commit.assert_not_called()
        self.tree = {100: (1, 'ghostty')}
        self.assertIsInstance(self.bind(), ImeTarget)
        self.focus.return_value = Focus(8, 'browser', 101)
        self.assertIsInstance(self.bind(), ImeTarget)
        self.focus.return_value = Focus(9, 'emacs', 102)
        with patch('voicekey.target.emacs.PendingPin'):
            self.assertIsInstance(self.bind(), EmacsTarget)

    def test_unregistered_unfocused_dead_and_wrong_ancestry_refuse(self):
        path = Path(self.tmp.name)/'voicekey/nvim'/f'{self.editor.process.pid}.json'
        self.editor.lua('vim.cmd.doautocmd("FocusLost")')
        self.assertIsInstance(self.bind(), RefusedTarget)
        self.editor.lua('vim.cmd.doautocmd("FocusGained")')
        original = path.read_text()
        for change in ({'pid': self.editor.process.pid, 'server': '/nonexistent'},
                       {'pid': 8888, 'server': self.editor.socket}, None):
            if change is None:
                path.unlink()
            else:
                path.write_text(json.dumps(change))
            self.tree[8888] = (200, 'nvim')
            target = self.bind()
            self.assertIsInstance(target, RefusedTarget)
            self.assertEqual(self.land(target).outcome, Outcome.REFUSED)
        path.write_text(original)
        self.ime.commit.assert_not_called()

    def test_two_instances_require_unique_focused_claim(self):
        other = Editor(self, self.tmp.name, 2)
        self.tree[other.process.pid] = (100, 'nvim')
        self.assertIsInstance(self.bind(), RefusedTarget)
        self.editor.lua('vim.cmd.doautocmd("FocusLost")')
        target = self.bind()
        self.assertIsInstance(target, NeovimTarget)
        self.assertEqual(target.pinning.pid, other.process.pid)
        self.assertEqual(self.land(target).outcome, Outcome.CONFIRMED)
        self.assertEqual(other.text(), ['Hello'])
        self.assertEqual(self.editor.text(), [''])

    def test_pin_tracks_edits_and_inserts_in_order_in_background(self):
        self.editor.lua('vim.api.nvim_buf_set_lines(0,0,-1,false,{"one"}); vim.api.nvim_win_set_cursor(0,{1,2})')
        target = self.bind()
        self.editor.lua('vim.api.nvim_buf_set_text(0,0,0,0,0,{"zero "}); vim.cmd.doautocmd("FocusLost")')
        for i, text in enumerate(('two', 'three')):
            result = target.insert_pinned(text, time.monotonic()+1, str(i), '', None, threading.Event())
            self.assertEqual(result.outcome, Outcome.CONFIRMED)
        self.assertEqual(self.editor.text(), ['zero one two three'])
        target.unpin()
        self.assertEqual(self.editor.lua('#vim.api.nvim_buf_get_extmarks(0,vim.api.nvim_get_namespaces().voicekey,0,-1,{})'), '0')

    def test_preview_at_pin_and_clear_without_ime(self):
        target = self.bind()
        target.show('live words')
        mark = json.loads(self.editor.lua('vim.json.encode(vim.api.nvim_buf_get_extmarks(0,vim.api.nvim_get_namespaces().voicekey,0,-1,{details=true}))'))
        self.assertEqual(mark[0][3]['virt_text'][0][0], 'live words')
        self.assertEqual(mark[0][3]['virt_text_pos'], 'inline')
        target.clear()
        self.assertEqual(self.editor.text(), [''])
        self.ime.preedit.assert_not_called()

    def test_closed_unloaded_unmodifiable_buffers_refuse(self):
        for command in ('vim.bo.modifiable=false', 'vim.api.nvim_set_current_buf(vim.api.nvim_create_buf(true,false)); vim.api.nvim_buf_delete(b,{force=true})',
                        'vim.api.nvim_set_current_buf(vim.api.nvim_create_buf(true,false)); vim.api.nvim_buf_delete(b,{force=true,unload=true})'):
            self.editor.lua('vim.cmd("enew!"); vim.bo.modifiable=true')
            target = self.bind()
            self.editor.lua('local b=vim.api.nvim_get_current_buf(); '+command)
            self.assertEqual(self.land(target).outcome, Outcome.REFUSED)
            target.clear()

    def test_cancel_expiry_and_revoked_permit_never_insert(self):
        target = self.bind()
        target.cancel()
        self.assertEqual(self.land(target).outcome, Outcome.REFUSED)
        target = self.bind()
        target.permit = str(Path(self.tmp.name)/'missing.permit')
        self.assertEqual(self.land(target).outcome, Outcome.REFUSED)
        self.assertEqual(self.editor.text(), [''])
        with self.assertRaises(nvim.NvimRefused):
            nvim.call(self.editor.socket, 'pin', {'id':'late'}, timeout=0)

    def test_duplicate_insert_does_not_mutate_twice(self):
        target = self.bind()
        for _ in range(2):
            result = target.insert_pinned('once', time.monotonic()+1, 'same', '', None, threading.Event())
            self.assertEqual(result.outcome, Outcome.CONFIRMED)
        self.assertEqual(self.editor.text(), ['once'])

    def test_rpc_timeout_is_uncertain_and_never_types(self):
        target = self.bind()
        with patch('voicekey.nvim.subprocess.run', side_effect=subprocess.TimeoutExpired('nvim', .01)):
            self.assertEqual(self.land(target).outcome, Outcome.UNKNOWN)
        self.ime.commit.assert_not_called()
        target.clear()

    def test_exit_after_binding_does_not_fall_back(self):
        target = self.bind()
        self.editor.close()
        self.assertIn(self.land(target).outcome, (Outcome.REFUSED, Outcome.UNKNOWN))
        self.ime.commit.assert_not_called()

    def test_busy_editor_rejects_expired_request_after_client_timeout(self):
        target = self.bind()
        marker = str(Path(self.tmp.name)/'blocked')
        self.editor.lua('vim.defer_fn(function() vim.fn.writefile({"ready"}, '
                        + json.dumps(marker) + '); vim.uv.sleep(250) end, 10)')
        wait_for(lambda: Path(marker).exists())
        with self.assertRaises(nvim.NvimError):
            nvim.call(self.editor.socket, 'insert', {'id':target.pinning.id,
                'text':'Too late', 'operation':'late', 'keep_pin':True}, timeout=.03)
        time.sleep(.3)
        self.assertEqual(self.editor.text(), [''])
        target.clear()

    def test_json_transport_preserves_quotes_backslashes_and_unicode(self):
        target = self.bind()
        text = "Kant's café \\ path\nSecond paragraph."
        self.assertEqual(self.land(target, text).outcome, Outcome.CONFIRMED)
        self.assertEqual(self.editor.text(), text.split('\n'))

    def test_startup_does_not_claim_focus_without_a_terminal_event(self):
        self.editor.lua('vim.cmd.doautocmd("FocusLost"); vim.cmd.doautocmd("VimEnter")')
        self.assertFalse(nvim.call(self.editor.socket, 'status')['focused'])
        self.assertIsInstance(self.bind(), RefusedTarget)

    def test_late_acknowledgement_cannot_authorize_a_pin(self):
        clock = iter((1., 1., 2.))
        reply = Mock(returncode=0, stdout='{"status":"ok","buffer":"late","before":""}')
        with patch('voicekey.nvim.subprocess.run', return_value=reply), \
                patch('voicekey.nvim.time.monotonic', side_effect=lambda: next(clock, 2.)):
            with self.assertRaisesRegex(nvim.NvimError, 'after the deadline'):
                nvim.call(self.editor.socket, 'pin', {'id':'late'}, timeout=.1)


@unittest.skipUnless(nvim_supported(), 'Neovim 0.10+ required')
class PersistentTests(unittest.TestCase):
    def setUp(self):
        import tests.test_follow as fixtures
        fixtures.FollowTests.setUp(self)
        patch.dict(os.environ, XDG_RUNTIME_DIR=self.tmp.name).start()
        self.editor = Editor(self, self.tmp.name)
        self.destination = Focus(7, 'com.mitchellh.ghostty', 100)
        patch('voicekey.target.focus.focused', side_effect=lambda **kw: self.destination).start()
        patch('voicekey.nvim.process_tree', return_value={100:(1,'ghostty'),self.editor.process.pid:(100,'nvim')}).start()
        patch('voicekey.target.emacs.PendingPin', side_effect=lambda pid: Mock(id='emacs-pin', pid=pid,
            valid=True, before=Mock(return_value=''), describe=Mock(return_value='buffer notes'))).start()
        self.ime = Mock(activation=Mock(return_value=1), rebind=Mock(return_value=True), commit=self.commit)
        self.ime.before_cursor.return_value = ''
        self.bound = []
        self.bind = self.binding

    def binding(self):
        result = bind(self.ime, self.cfg.dictation, True)
        self.bound.append(result)
        return result

    def start(self):
        import tests.test_follow as fixtures
        fixtures.FollowTests.start(self)

    def speak(self):
        import numpy as np
        self.recorder.push(np.ones(5120,dtype=np.float32)*.2)
        self.recorder.push(np.zeros(5120,dtype=np.float32))

    def finish(self):
        self.session.request_stop()
        self.assertTrue(self.session.done.wait(5))

    def report(self, focused, sequence=1):
        self.session.editor_focus_changed(dict(self.editor.record, focused=focused, sequence=sequence))

    def test_repeated_utterances_preview_and_release_one_pin(self):
        self.cfg.persistent.destination_policy = 'pin'
        self.start()
        target = self.bound[0]
        target.show('provisional')
        self.backend.transcribe.side_effect = ['First.', 'Second.', 'Third.']
        for count in range(1, 4):
            self.speak()
            wait_for(lambda: len(self.editor.text()[0].split()) == count)
        self.finish()
        self.assertEqual(self.editor.text(), ['First. Second. Third.'])
        self.assertEqual(len(self.bound), 1)
        self.assertEqual(self.editor.lua('#vim.api.nvim_buf_get_extmarks(0,vim.api.nvim_get_namespaces().voicekey,0,-1,{})'), '0')
        self.commit.assert_not_called()

    def test_plugin_focus_lost_pauses_and_previous_speech_still_lands(self):
        from voicekey.control import ControlServer
        self.cfg.persistent.destination_policy = 'pause'
        self.start()
        # Actual Lua -> control socket -> daemon callback, without Niri or drain().
        from voicekey.daemon import Daemon
        daemon = Mock(persistent=self.session)
        server = ControlServer(editor_focus=lambda event: Daemon._editor_focus(daemon, event))
        server.start()
        self.addCleanup(server.close)
        import numpy as np
        self.recorder.push(np.ones(4096,dtype=np.float32)*.2)
        self.editor.lua('vim.cmd.doautocmd("FocusLost")')
        self.assertTrue(self.session.done.wait(4))
        self.assertTrue(self.session.paused)
        self.assertEqual(self.editor.text(), ['Words.'])
        self.assertEqual(self.destination.id, 7)

    def test_follow_neovim_emacs_app_neovim_through_one_resolver(self):
        self.start()
        for count, destination in enumerate((Focus(8,'emacs',200), Focus(9,'browser',300),
                                             Focus(7,'com.mitchellh.ghostty',100)), 1):
            self.speak()
            wait_for(lambda: self.backend.transcribe.call_count >= count)
            if count == 1:
                wait_for(lambda: self.editor.text() == ['Words.'])
            elif count == 2:
                wait_for(lambda: self.insert.call_count == 1)
            else:
                wait_for(lambda: self.commit.call_count == 1)
            self.destination = destination
            self.session.watcher.changed(destination)
            wait_for(lambda: self.session.target.target.window_id == destination.id)
        self.speak()
        wait_for(lambda: self.editor.text() == ['Words. Words.'])
        self.finish()
        self.assertEqual([t.kind for t in self.bound], ['neovim','emacs','input method','neovim'])
        self.unpin.assert_called_with('emacs-pin')
        self.type_text.assert_not_called()

    def test_follow_focus_lost_cuts_to_refusal_and_preserves_old_pin(self):
        import numpy as np
        self.start()
        self.recorder.push(np.ones(4096,dtype=np.float32)*.2)
        self.editor.lua('vim.cmd.doautocmd("FocusLost")')
        self.report(False)
        self.assertTrue(self.session.done.wait(4))
        self.assertTrue(self.session.paused)
        self.assertIsInstance(self.bound[-1], RefusedTarget)
        self.assertEqual(self.editor.text(), ['Words.'])
        self.type_text.assert_not_called()

    def test_exit_during_processing_keeps_audio_and_text(self):
        entered, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)
        def transcribe(samples):
            entered.set(); release.wait(3); return 'Retained.'
        self.backend.transcribe.side_effect = transcribe
        self.start()
        self.speak()
        self.assertTrue(entered.wait(2))
        self.editor.close()
        release.set()
        self.assertTrue(self.session.done.wait(4))
        self.assertIn('Retained.', (Path(self.tmp.name)/'last-recovery.txt').read_text())
        self.assertTrue(list((Path(self.tmp.name)/'sessions').glob('*.wav')))
        self.type_text.assert_not_called()

    def test_pin_policy_keeps_background_capture(self):
        self.cfg.persistent.destination_policy = 'pin'
        self.start()
        self.editor.lua('vim.cmd.doautocmd("FocusLost")')
        self.report(False)
        self.speak()
        wait_for(lambda: self.editor.text() == ['Words.'])
        self.assertFalse(self.session.stopping.is_set())
        self.finish()

    def test_duplicate_and_out_of_order_focus_reports_do_not_rebind(self):
        self.start()
        self.report(True, 5)
        self.report(False, 4)
        self.assertTrue(self.session.focus_events.empty())
        self.assertFalse(self.session.target.departed.is_set())

    def test_follow_between_instances_inside_one_terminal_window(self):
        import numpy as np
        self.start()
        other = Editor(self, self.tmp.name, 2)
        tree = {100:(1,'ghostty'), self.editor.process.pid:(100,'nvim'), other.process.pid:(100,'nvim')}
        self.recorder.push(np.ones(4096,dtype=np.float32)*.2)
        self.editor.lua('vim.cmd.doautocmd("FocusLost")')
        with patch('voicekey.nvim.process_tree', return_value=tree):
            self.report(False)
            wait_for(lambda: getattr(self.session.target.target, 'focus_identity', None)
                     == (other.process.pid, other.socket))
            wait_for(lambda: self.editor.text() == ['Words.'])
            self.speak()
            wait_for(lambda: other.text() == ['Words.'])
            self.finish()
        self.assertEqual([t.window_id for t in self.bound], [7,7])
        self.type_text.assert_not_called()

    def test_session_live_and_provisional_preview_use_virtual_text(self):
        import numpy as np
        entered, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)
        def polish(text, *args, **kwargs):
            entered.set(); release.wait(3); return 'Final.'
        self.polisher = Mock(polish=polish)
        self.start()
        self.recorder.push(np.ones(4096,dtype=np.float32)*.2)
        wait_for(lambda: self.session.has_speech)
        self.pipeline.ledger.live(self.session.current.id, 'Live words')
        self.session.target.render()
        def preview():
            return self.editor.lua('vim.json.encode(vim.api.nvim_buf_get_extmarks(0,vim.api.nvim_get_namespaces().voicekey,0,-1,{details=true}))')
        self.assertIn('Live words', preview())
        self.recorder.push(np.zeros(5120,dtype=np.float32))
        self.assertTrue(entered.wait(2))
        self.session.target.render()
        self.assertIn('Words.', preview())
        release.set()
        wait_for(lambda: self.editor.text() == ['Final.'])
        self.finish()
        self.assertEqual(json.loads(preview()), [])
        self.ime.preedit.assert_not_called()


@unittest.skipUnless(nvim_supported(), 'Neovim 0.10+ required')
class KeyTests(unittest.TestCase):
    def setUp(self):
        from evdev import ecodes
        from voicekey.config import Config
        from voicekey.daemon import Daemon
        from voicekey.gate import Gate
        from voicekey.recovery import Journal
        from tests.test_pipeline import FakeRecorder
        TargetTests.setUp(self)
        self.cfg = Config()
        self.cfg.pipeline.shutdown_seconds = 1
        self.daemon = Daemon(self.cfg, recorder_factory=FakeRecorder,
                             journal=Journal(self.tmp.name+'/sessions'))
        self.daemon.gate = Gate(self.tmp.name+'/lock')
        self.daemon.gate.open()
        self.daemon.backend = Mock(transcribe=Mock(return_value='Dictated.'))
        self.daemon.ime = self.ime
        self.daemon.start_workers()
        self.addCleanup(self.daemon.close)
        for module in ('daemon','pipeline','persistent'):
            patch(f'voicekey.{module}.notify').start()
        self.copy = patch('voicekey.pipeline.inject.copy').start()
        # Existing single-shot configuration; defaults use persistent tap/hold.
        self.daemon.actions[frozenset({ecodes.KEY_RIGHTMETA})] = ('dictate','hold')
        self.keycode = ecodes.KEY_RIGHTMETA

    def key(self, value):
        self.daemon._on_key('test', self.keycode, value)

    def done(self):
        wait_for(lambda: not self.daemon.pipeline.ledger.busy)

    def test_hold_key_binds_neovim_and_stop_while_processing_does_not_redirect(self):
        entered, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)
        self.daemon.backend.transcribe.side_effect = lambda samples: (entered.set(), release.wait(3), 'Dictated.')[-1]
        self.key(1)
        self.assertIsInstance(self.daemon.session.target, NeovimTarget)
        self.assertEqual(self.daemon.status()['destination_name'], 'Neovim')
        self.key(0)
        self.assertTrue(entered.wait(2))
        self.daemon.command('stop')
        self.focus.return_value = Focus(8,'browser',101)
        self.editor.lua('vim.cmd.doautocmd("FocusLost")')
        release.set()
        self.done()
        self.assertEqual(self.editor.text(), ['Dictated.'])
        self.ime.commit.assert_not_called()

    def test_short_tap_and_cancel_insert_nothing(self):
        self.daemon.recorder.duration = .01
        self.key(1); self.key(0); self.done()
        self.assertEqual(self.editor.text(), [''])
        self.key(1)
        self.daemon.session.target.cancel()
        self.key(0); self.done()
        self.assertEqual(self.editor.text(), [''])
        self.copy.assert_not_called()

    def test_unregistered_editor_refuses_with_journal_recovery(self):
        (Path(self.tmp.name)/'voicekey/nvim'/f'{self.editor.process.pid}.json').unlink()
        with patch('voicekey.daemon.notify') as notify:
            self.key(1)
            self.assertIsInstance(self.daemon.session.target, RefusedTarget)
            attention = [c for c in notify.call_args_list if c.kwargs.get('attention')]
            self.assertEqual(len(attention), 1)
            self.assertIn('destination refused', attention[0].args[0])
            self.daemon.backend.transcribe.assert_not_called()
        self.key(0); self.done()
        self.assertEqual(self.editor.text(), [''])
        self.assertTrue(list((Path(self.tmp.name)/'sessions').glob('*.wav')))
        self.assertIn('Dictated.', (Path(self.tmp.name)/'last-recovery.txt').read_text())
        self.ime.commit.assert_not_called()
        self.copy.assert_called_once_with('Dictated.')

    def test_rpc_timeout_keeps_journal_and_audio(self):
        real_call = nvim.call
        def call(server, method, *args, **kwargs):
            if method == 'insert':
                raise nvim.NvimError('RPC timeout')
            return real_call(server, method, *args, **kwargs)
        self.key(1)
        with patch('voicekey.nvim.call', side_effect=call):
            self.key(0); self.done()
        self.assertTrue(list((Path(self.tmp.name)/'sessions').glob('*.wav')))
        records = '\n'.join(p.read_text() for p in (Path(self.tmp.name)/'sessions').glob('*.jsonl'))
        self.assertIn('unknown', records)
        self.assertIn('Dictated.', records)
        self.copy.assert_not_called()

    def test_default_tap_hold_and_f23_route_persistent_capture_to_neovim(self):
        import numpy as np
        from evdev import ecodes
        from tests.test_persistent import ControlledRecorder
        self.daemon.recorder_factory = ControlledRecorder
        self.daemon.vad = Mock(speech=lambda samples: bool(np.max(np.abs(samples)) > .01))
        self.cfg.persistent.destination_policy = 'pin'
        with patch('voicekey.daemon.focus.compositor', return_value=None):
            for key, hold in ((ecodes.KEY_RIGHTMETA, False), (ecodes.KEY_F23, True)):
                self.keycode = key
                self.daemon.actions[frozenset({key})] = ('persistent','tap/hold')
                self.key(1)
                session = self.daemon.persistent
                wait_for(session.ready.is_set)
                self.assertIsInstance(session.target.target, NeovimTarget)
                self.assertEqual(self.daemon.status()['destination_name'], 'Neovim')
                if hold:
                    self.daemon._gesture = (session, time.monotonic()-self.cfg.tap_seconds-1)
                else:
                    self.key(0)
                    self.assertFalse(session.stopping.is_set())
                session.recorder.push(np.ones(4096,dtype=np.float32)*.2)
                self.key(0 if hold else 1)
                self.assertTrue(session.done.wait(4))
                self.key(0)
                self.daemon._on_tick()
        self.assertEqual(self.editor.text(), ['Dictated. Dictated.'])
        self.ime.commit.assert_not_called()

    def test_single_shot_key_resolves_emacs_plain_terminal_and_other_app(self):
        self.tree = {100: (1, 'ghostty')}
        pin = Mock(id='isolated-test-pin', valid=True, before=Mock(return_value=''))
        with patch('voicekey.target.emacs.PendingPin', return_value=pin), \
                patch('voicekey.emacs.insert') as insert:
            for destination, expected in ((Focus(8,'emacs',200), EmacsTarget),
                                          (Focus(7,'com.mitchellh.ghostty',100), ImeTarget),
                                          (Focus(9,'browser',300), ImeTarget)):
                self.focus.return_value = destination
                self.key(1)
                self.assertIsInstance(self.daemon.session.target, expected)
                self.key(0)
                self.done()
            insert.assert_called_once()
        self.assertEqual(self.ime.commit.call_count, 2)

    def test_failed_pin_warns_before_speech_and_copies_final_text(self):
        real_call = nvim.call
        def call(server, method, *args, **kwargs):
            if method == 'pin':
                raise nvim.NvimRefused('buffer is not modifiable')
            return real_call(server, method, *args, **kwargs)
        with patch('voicekey.nvim.call', side_effect=call), patch('voicekey.daemon.notify') as notify:
            self.key(1)
            self.assertTrue(any(c.kwargs.get('attention') and 'not modifiable' in c.args[1]
                                for c in notify.call_args_list))
            self.daemon.backend.transcribe.assert_not_called()
        self.key(0); self.done()
        self.copy.assert_called_once_with('Dictated.')
        self.assertEqual(self.editor.text(), [''])

    def test_definite_insert_refusal_copies_but_clipboard_failure_keeps_recovery(self):
        self.key(1)
        self.editor.lua('vim.api.nvim_set_option_value("modifiable",false,{buf=0})')
        self.copy.side_effect = OSError('clipboard unavailable')
        self.key(0); self.done()
        self.copy.assert_called_once_with('Dictated.')
        self.assertTrue(list((Path(self.tmp.name)/'sessions').glob('*.wav')))
        self.assertIn('Dictated.', (Path(self.tmp.name)/'last-recovery.txt').read_text())
        self.assertEqual(self.editor.text(), [''])
