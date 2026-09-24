import json
import socket
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from evdev import ecodes

from voicekey.control import ControlServer
from voicekey.config import Config
from voicekey.daemon import Daemon
from voicekey.gate import Gate
from voicekey.history import latest
from voicekey.recovery import Journal
from tests.test_pipeline import FakeRecorder, wait_for


class CaptureHarness(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / 'voicekey' / 'control.sock'
        self.server = ControlServer(self.path)
        self.server.start()
        self.addCleanup(self.server.close)
        cfg = Config()
        cfg.pipeline.shutdown_seconds = .3
        self.daemon = Daemon(cfg, recorder_factory=FakeRecorder, journal=Journal(self.tmp.name + '/sessions'))
        self.daemon.control = self.server
        self.daemon.gate = Gate(self.tmp.name + '/gate')
        self.daemon.backend = Mock(transcribe=Mock(return_value='Hello from the daemon.'))
        self.daemon.vad = Mock()
        self.addCleanup(self.daemon.close)
        self.addCleanup(patch.stopall)
        patch('voicekey.pipeline.notify').start()
        patch('voicekey.daemon.notify').start()
        self.bind = patch('voicekey.daemon.target_mod.bind').start()
        self.copy = patch('voicekey.pipeline.inject.copy').start()
        self.daemon.start_workers()
        self.events = []
        self.results = []
        self.request_id = 0

    def connect(self):
        client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        client.settimeout(2)
        client.connect(str(self.path))
        self.addCleanup(client.close)
        stream = client.makefile('rb')
        self.addCleanup(stream.close)
        status = json.loads(stream.readline())
        self.assertEqual(status['protocol_version'], 1)
        return client, stream

    def read(self, stream, kind):
        if kind == 'capture-result' and self.results:
            return self.results.pop(0)
        while True:
            message = json.loads(stream.readline())
            self.events.append(message)
            if message['type'] == kind:
                return message
            if message['type'] == 'capture-result':
                self.results.append(message)

    def send(self, client, stream, command, args=None):
        self.request_id += 1
        client.sendall((json.dumps({'command': command, 'id': self.request_id, 'args': args or {}}) + '\n').encode())
        wait_for(lambda: not self.server.commands.empty())
        self.daemon._on_tick()
        return self.read(stream, 'reply')

    def start(self, client, stream, **args):
        reply = self.send(client, stream, 'capture-start', args)
        self.assertIsNone(reply['error'])
        return reply['capture_id']

    def settle(self):
        wait_for(lambda: not self.daemon.pipeline.ledger.busy)
        self.daemon._on_tick()
        self.assertIsNone(self.daemon.client_capture)

    def controller(self):
        stopped = threading.Event()
        def run():
            while not stopped.wait(.005):
                self.daemon._on_tick()
        thread = threading.Thread(target=run)
        thread.start()
        def close():
            stopped.set()
            thread.join(2)
        self.addCleanup(close)


class ClientCaptureTests(CaptureHarness):
    def test_arguments_private_events_text_processing_and_shared_models(self):
        d = self.daemon
        d.cfg.text.word_overrides = {'daemon': 'server'}
        d.cfg.dictation.post_transcription_hook = "sed 's/^/text: /'"
        client, stream = self.connect()
        other, other_stream = self.connect()
        identity = self.start(client, stream, seconds=7)
        self.assertEqual(d.client_capture.recorder.max_samples, 7 * 16000)
        self.assertEqual(self.read(stream, 'capture-progress')['state'], 'recording')
        self.assertTrue(d.status()['client_capture'])
        self.assertTrue(d.status()['listening'])
        reply = self.send(client, stream, 'capture-finish', {'capture_id': identity})
        self.assertIsNone(reply['error'])
        result = self.read(stream, 'capture-result')
        self.assertEqual(result['text'], 'text: Hello from the server.')
        self.assertEqual(result['id'], 1)
        self.assertEqual(result['capture_id'], identity)
        self.assertEqual([e['state'] for e in self.events if e['type'] == 'capture-progress'],
                         ['recording', 'transcribing'])
        self.settle()
        self.events.clear()
        self.send(other, other_stream, 'pin')
        self.assertFalse(any(e['type'].startswith('capture-') for e in self.events))
        self.assertEqual(latest(d.pipeline.journal)['final'], result['text'])
        d.backend.transcribe.assert_called_once()
        self.bind.assert_not_called()
        self.copy.assert_not_called()

    def test_invalid_arguments_and_capture_ownership(self):
        client, stream = self.connect()
        for args in ({'seconds': 0}, {'seconds': True}, {'seconds': float('inf')},
                     {'seconds': '10'}, {'wav': 'relative.wav'}, {'unknown': 1}):
            self.assertIsNotNone(self.send(client, stream, 'capture-start', args)['error'])
            self.assertIsNone(self.daemon.client_capture)
        identity = self.start(client, stream)
        other, other_stream = self.connect()
        for command in ('capture-finish', 'capture-cancel'):
            reply = self.send(other, other_stream, command, {'capture_id': identity})
            self.assertIn('owned by this connection', reply['error'])
        self.assertIsNotNone(self.send(client, stream, 'capture-finish', {'capture_id': 'wrong'})['error'])
        self.send(client, stream, 'capture-cancel', {'capture_id': identity})
        self.settle()

    def test_refused_while_listening_and_desktop_refused_during_client_capture(self):
        client, stream = self.connect()
        for attr in ('session', 'persistent'):
            setattr(self.daemon, attr, Mock())
            # Drain directly: fake desktop sessions need not implement tick/status.
            client.sendall(b'{"command":"capture-start","id":1}\n')
            wait_for(lambda: not self.server.commands.empty())
            self.server.drain(self.daemon.command)
            self.assertIn('Microphone busy', self.read(stream, 'reply')['error'])
            setattr(self.daemon, attr, None)
        identity = self.start(client, stream)
        for command in ('capture-start', 'start', 'start-typing', 'follow-focus', 'pin'):
            self.assertIsNotNone(self.send(client, stream, command)['error'])
        with patch.object(self.daemon, '_start_persistent') as persistent, patch.object(self.daemon, '_start') as hold:
            with patch('voicekey.daemon.notify') as notify:
                self.daemon._on_key('keyboard', ecodes.KEY_F10, 1)
                notify.assert_called_once()
            self.assertTrue(self.daemon.client_capture.listening)
            persistent.assert_not_called()
            hold.assert_not_called()
        self.send(client, stream, 'capture-cancel', {'capture_id': identity})
        self.settle()

    def test_cancel_discards_recording_and_returns_one_terminal_event(self):
        client, stream = self.connect()
        identity = self.start(client, stream)
        self.send(client, stream, 'capture-cancel', {'capture_id': identity})
        # Cancellation result may precede its command reply.
        results = [e for e in self.events if e['type'] == 'capture-result']
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]['error'], 'cancelled')
        self.settle()
        self.daemon.backend.transcribe.assert_not_called()
        self.assertEqual(list(self.daemon.pipeline.journal.directory.glob('*.jsonl')), [])
        self.copy.assert_not_called()

    def test_global_stop_from_another_connection_finishes_once_for_original_owner(self):
        client, stream = self.connect()
        other, other_stream = self.connect()
        identity = self.start(client, stream)
        entered, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)

        def transcribe(samples):
            entered.set()
            release.wait(2)
            return 'Stopped from the panel.'

        self.daemon.backend.transcribe.side_effect = transcribe
        with patch.object(self.daemon.pipeline, 'submit', wraps=self.daemon.pipeline.submit) as submit:
            self.assertIsNone(self.send(other, other_stream, 'stop')['error'])
            self.assertTrue(entered.wait(1))
            self.assertFalse(self.daemon.status()['listening'])
            self.assertTrue(self.daemon.status()['client_capture'])
            self.assertIsNone(self.send(other, other_stream, 'stop')['error'])
            submit.assert_called_once()
            self.assertFalse(any(e['type'].startswith('capture-') for e in self.events))
            release.set()
            result = self.read(stream, 'capture-result')
        self.assertEqual(result['capture_id'], identity)
        self.assertEqual(result['text'], 'Stopped from the panel.')
        self.settle()
        self.events.clear()
        self.assertIsNone(self.send(other, other_stream, 'stop')['error'])
        self.assertFalse(any(e['type'].startswith('capture-') for e in self.events))
        self.bind.assert_not_called()
        self.copy.assert_not_called()

    def test_dictation_hotkeys_finish_client_without_starting_desktop_capture(self):
        client, stream = self.connect()
        chord = frozenset({ecodes.KEY_F9})
        for action, behavior in (('persistent', 'tap/hold'), ('persistent', 'toggle'), ('dictate', 'hold')):
            with self.subTest(action=action, behavior=behavior):
                identity = self.start(client, stream)
                self.daemon.actions = {chord: (action, behavior)}
                with patch.object(self.daemon, '_start_persistent') as persistent, \
                        patch.object(self.daemon, '_start') as hold, \
                        patch('voicekey.daemon.notify') as notify, \
                        patch.object(self.daemon.pipeline, 'submit', wraps=self.daemon.pipeline.submit) as submit:
                    for value in (1, 2, 0):
                        self.daemon._on_key('keyboard', ecodes.KEY_F9, value)
                    notify.assert_not_called()
                    for value in (1, 0):
                        self.daemon._on_key('keyboard', ecodes.KEY_F9, value)
                    notify.assert_called_once_with('voicekey: busy', 'Client capture is still processing', attention=True, ms=3000)
                    self.assertFalse(self.daemon.status()['listening'])
                    submit.assert_called_once()
                    persistent.assert_not_called()
                    hold.assert_not_called()
                result = self.read(stream, 'capture-result')
                self.assertEqual(result['capture_id'], identity)
                self.assertEqual(result['text'], 'Hello from the daemon.')
                self.settle()

    def test_cancel_during_transcription_suppresses_delivery(self):
        client, stream = self.connect()
        entered, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)
        def transcribe(samples):
            entered.set()
            release.wait(2)
            return 'Late text'
        self.daemon.backend.transcribe.side_effect = transcribe
        self.daemon.polisher = Mock()
        identity = self.start(client, stream)
        self.send(client, stream, 'capture-finish', {'capture_id': identity})
        self.assertTrue(entered.wait(1))
        self.send(client, stream, 'capture-cancel', {'capture_id': identity})
        release.set()
        self.settle()
        results = [e for e in self.events if e['type'] == 'capture-result']
        self.assertEqual([e['error'] for e in results], ['cancelled'])
        self.daemon.polisher.polish.assert_not_called()
        with self.assertRaises(LookupError):
            latest(self.daemon.pipeline.journal)
        self.copy.assert_not_called()

    def test_disconnect_finishes_and_keeps_transcript_in_normal_history(self):
        client, stream = self.connect()
        self.start(client, stream)
        owner = self.daemon.client_capture.client
        stream.close()
        client.close()
        wait_for(lambda: owner.fileno() < 0)
        self.daemon._on_tick()
        self.settle()
        record = latest(self.daemon.pipeline.journal)
        self.assertEqual(record['final'], 'Hello from the daemon.')
        self.assertEqual(record['outcome'], 'saved')
        self.assertIn('disconnected', record['reason'])
        self.copy.assert_not_called()

    def test_cold_models_load_in_background_and_finish_waits_for_them(self):
        d = self.daemon
        model = d.backend
        d.backend = None
        d.model_state = 'unloaded'
        entered, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)
        def load():
            entered.set()
            release.wait(2)
            d.backend = model
        client, stream = self.connect()
        with patch.object(d, '_load_models', side_effect=load):
            identity = self.start(client, stream)
            self.assertTrue(entered.wait(1))
            self.assertEqual(self.read(stream, 'capture-progress')['state'], 'loading')
            self.assertEqual(self.read(stream, 'capture-progress')['state'], 'recording')
            self.send(client, stream, 'capture-finish', {'capture_id': identity})
            model.transcribe.assert_not_called()
            release.set()
            result = self.read(stream, 'capture-result')
            self.assertEqual(result['text'], 'Hello from the daemon.')
            self.settle()

    def test_processing_budget_is_independent_of_desktop_delivery_age(self):
        self.daemon.cfg.dictation.max_delay_seconds = .001
        client, stream = self.connect()
        identity = self.start(client, stream)
        # The command's two-second expiry no longer governs this admitted capture.
        self.daemon.client_capture.recorder.elapsed = 3
        self.daemon._on_tick()
        self.assertFalse(self.daemon.client_capture.submitted)
        self.send(client, stream, 'capture-finish', {'capture_id': identity})
        self.assertIsNone(self.read(stream, 'capture-result')['error'])
        self.settle()

    def test_source_exit_and_duration_limit_finish_automatically(self):
        client, stream = self.connect()
        for source_exit in (True, False):
            self.start(client, stream, seconds=2)
            capture = self.daemon.client_capture
            if source_exit:
                capture.recorder.finished = True
            else:
                capture.recorder.elapsed = 2.1
            self.daemon._on_tick()
            self.assertIsNone(self.read(stream, 'capture-result')['error'])
            self.settle()

    def test_recognition_error_and_empty_audio_are_terminal(self):
        client, stream = self.connect()
        self.daemon.backend.transcribe.side_effect = RuntimeError('broken recognizer')
        identity = self.start(client, stream)
        self.send(client, stream, 'capture-finish', {'capture_id': identity})
        result = self.read(stream, 'capture-result')
        self.assertEqual(result['error'], 'capture_failed')
        self.assertIn('broken recognizer', result['reason'])
        self.settle()
        identity = self.start(client, stream)
        self.daemon.client_capture.recorder.duration = 0
        self.send(client, stream, 'capture-finish', {'capture_id': identity})
        self.assertEqual(self.read(stream, 'capture-result')['error'], 'no_speech')
        self.settle()

    def test_start_failure_and_storage_failure_report_reason(self):
        client, stream = self.connect()
        with patch.object(FakeRecorder, 'start', side_effect=OSError('no microphone')):
            self.start(client, stream)
            self.assertIn('no microphone', self.read(stream, 'capture-result')['reason'])
        identity = self.start(client, stream)
        with patch.object(self.daemon.pipeline.journal, 'capture', side_effect=OSError('disk full')):
            self.send(client, stream, 'capture-finish', {'capture_id': identity})
            self.assertIn('disk full', self.read(stream, 'capture-result')['reason'])
            self.settle()

    def test_large_result_survives_partial_socket_writes(self):
        client, stream = self.connect()
        text = '界' * 30000
        self.daemon.backend.transcribe.return_value = text
        identity = self.start(client, stream)
        self.daemon.client_capture.client.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 4096)
        self.send(client, stream, 'capture-finish', {'capture_id': identity})
        self.assertEqual(self.read(stream, 'capture-result')['text'], text)
        self.settle()
        self.assertEqual(latest(self.daemon.pipeline.journal)['outcome'], 'submitted')

    def test_cold_model_timeout_produces_terminal_error(self):
        client, stream = self.connect()
        self.daemon.model_state = 'loading'
        self.daemon._models_ready.clear()
        self.daemon.cfg.pipeline.transcription_seconds = .1
        identity = self.start(client, stream)
        self.send(client, stream, 'capture-finish', {'capture_id': identity})
        result = self.read(stream, 'capture-result')
        self.assertEqual(result['error'], 'capture_failed')
        self.assertIn('transcription failed', result['reason'])
        self.settle()
        self.daemon._models_ready.set()

    def test_free_memory_finishes_client_before_releasing_models(self):
        client, stream = self.connect()
        self.start(client, stream)
        with patch.object(self.daemon, '_release_models') as release:
            self.send(client, stream, 'free-memory')
            self.assertIsNone(self.read(stream, 'capture-result')['error'])
            self.settle()
            wait_for(lambda: self.daemon.model_state == 'unloaded')
            release.assert_called_once()

    def test_capture_cancel_before_first_tick_never_opens_microphone(self):
        client, stream = self.connect()
        client.sendall(b'{"command":"capture-start","id":1}\n')
        wait_for(lambda: not self.server.commands.empty())
        self.server.drain(self.daemon.command)
        reply = self.read(stream, 'reply')
        recorder = self.daemon.client_capture.recorder
        self.send(client, stream, 'capture-cancel', {'capture_id': reply['capture_id']})
        self.assertFalse(recorder.active)
        self.assertIsNone(self.daemon.client_capture)
        self.daemon.backend.transcribe.assert_not_called()

    def test_partial_hotkey_chord_does_not_interrupt_editor_typing(self):
        client, stream = self.connect()
        self.start(client, stream)
        self.daemon.actions = {frozenset({ecodes.KEY_LEFTALT, ecodes.KEY_F9}): ('dictate', 'hold')}
        with patch('voicekey.daemon.notify') as notify:
            self.daemon._on_key('keyboard', ecodes.KEY_LEFTALT, 1)
            notify.assert_not_called()
            self.assertTrue(self.daemon.client_capture.listening)
            self.daemon._on_key('keyboard', ecodes.KEY_F9, 1)
            notify.assert_not_called()
            self.assertTrue(self.daemon.client_capture.submitted)
            self.daemon._on_key('keyboard', ecodes.KEY_F9, 0)
            self.daemon._on_key('keyboard', ecodes.KEY_LEFTALT, 0)
        self.assertEqual(self.daemon.pressed['keyboard'], set())
        self.assertIsNone(self.read(stream, 'capture-result')['error'])
        self.settle()

    def test_shutdown_before_first_tick_does_not_start_capture(self):
        client, stream = self.connect()
        client.sendall(b'{"command":"capture-start","id":1}\n')
        wait_for(lambda: not self.server.commands.empty())
        self.server.drain(self.daemon.command)
        self.read(stream, 'reply')
        recorder = self.daemon.client_capture.recorder
        self.daemon.close()
        self.assertFalse(recorder.active)
        self.assertFalse(self.daemon.pipeline.ledger.busy)

    def test_very_large_integer_duration_is_capped_without_crashing_controller(self):
        client, stream = self.connect()
        identity = self.start(client, stream, seconds=10**500)
        self.assertEqual(self.daemon.client_capture.seconds, self.daemon.cfg.max_seconds)
        self.send(client, stream, 'capture-cancel', {'capture_id': identity})
        self.settle()
