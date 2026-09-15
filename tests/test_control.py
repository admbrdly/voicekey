import json
import os
import queue
import socket
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from voicekey.control import ControlServer, request
from voicekey.config import Config
from voicekey.daemon import Daemon
from voicekey.recovery import Journal
from tests.test_pipeline import wait_for


class ControlTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name)/'runtime'/'control.sock'
        self.server = ControlServer(self.path)
        self.server.start()
        self.addCleanup(self.server.close)

    def connect(self):
        client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        client.settimeout(1)
        client.connect(str(self.path))
        self.addCleanup(client.close)
        stream = client.makefile('rb')
        self.addCleanup(stream.close)
        self.assertEqual(json.loads(stream.readline())['type'], 'status')
        return client, stream

    def test_status_and_private_socket(self):
        self.server.publish({'state': 'idle', 'listening': False})
        result = request('status', self.path)
        self.assertEqual(result['state'], 'idle')
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.path.parent.stat().st_mode & 0o777, 0o700)

    def test_mutation_only_runs_when_controller_drains_then_acknowledges(self):
        client, stream = self.connect()
        client.sendall(b'{"command":"start","id":7}\n')
        wait_for(lambda: not self.server.commands.empty())
        handler = Mock()
        handler.assert_not_called()
        self.server.drain(handler)
        handler.assert_called_once_with('start')
        while (reply := json.loads(stream.readline()))['type'] != 'reply':
            pass
        self.assertEqual(reply, {'type': 'reply', 'id': 7, 'error': None})

    def test_expired_request_cannot_start_recording_late(self):
        client, _ = self.connect()
        self.server.commands.put((client, {'command': 'start'}, time.monotonic()-1))
        handler = Mock()
        self.server.drain(handler)
        handler.assert_not_called()

    def test_unknown_and_oversized_input_disconnect_without_mutation(self):
        for data in (b'{"command":"shell"}\n', b'x'*9000):
            client, stream = self.connect()
            client.sendall(data)
            # An initial status heartbeat may precede disconnect.
            while stream.readline():
                pass
        self.assertTrue(self.server.commands.empty())
        self.assertTrue(self.server.thread.is_alive())

    def test_second_server_cannot_replace_live_socket(self):
        other = ControlServer(self.path)
        with self.assertRaisesRegex(OSError, 'already in use'):
            other.start()
        self.assertEqual(request('status', self.path)['state'], 'loading')

    def test_reconnect_after_daemon_restart(self):
        self.server.close()
        self.assertFalse(self.path.exists())
        self.server = ControlServer(self.path)
        self.server.start()
        self.addCleanup(self.server.close)
        self.server.publish({'state': 'idle'})
        self.assertEqual(request('status', self.path)['state'], 'idle')


class DaemonControlTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.daemon = Daemon(Config(), journal=Journal(self.tmp.name))
        self.addCleanup(self.daemon.close)
        self.daemon.backend = Mock()
        self.daemon.vad = Mock()

    def test_policy_changes_are_idle_only_and_do_not_touch_app_styles(self):
        d = self.daemon
        styles = dict(d.cfg.polish.app_styles)
        with patch('voicekey.daemon.focus.compositor', return_value='niri'):
            d.command('pin')
            self.assertFalse(d.status()['follow_focus'])
            d.command('follow-focus')
            self.assertTrue(d.status()['follow_focus'])
        d.persistent = Mock()
        d.persistent.stopping.is_set.return_value = False
        self.assertTrue(d.status()['listening'])
        with self.assertRaisesRegex(ValueError, 'Stop dictation'):
            d.command('pin')
        self.assertEqual(d.cfg.polish.app_styles, styles)
        d.persistent = None

    def test_stop_uses_existing_session_stop_and_clears_hold_gesture(self):
        d = self.daemon
        d.persistent = Mock()
        d._gesture = ('session', 0)
        d.command('stop')
        d.persistent.request_stop.assert_called_once_with('stopped from panel')
        self.assertIsNone(d._gesture)
        d.persistent = None

    def test_start_is_idempotently_refused_while_busy(self):
        d = self.daemon
        with patch.object(d, '_start_persistent') as start:
            d.persistent = Mock()
            with self.assertRaises(ValueError):
                d.command('start')
            start.assert_not_called()
        d.persistent = None
