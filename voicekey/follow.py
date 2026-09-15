"""Niri focus events, sampled against the recorder's audio clock.

The event reader never binds destinations or runs desktop commands. It only
reports window identities; the capture worker owns cuts and rebinding.
"""
import json
import os
import socket
import threading

from .focus import Focus, _focus


class NiriFocusWatch:
    def __init__(self, changed, failed):
        self.changed, self.failed = changed, failed
        self.windows = {}
        self.current = Focus()
        self.initialized = False
        self.closed = threading.Event()
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            self.sock.settimeout(1)
            self.sock.connect(os.environ['NIRI_SOCKET'])
            self.sock.sendall(b'"EventStream"\n')
            self.sock.settimeout(None)
        except Exception:
            self.sock.close()
            raise
        self.thread = threading.Thread(target=self._run, name='niri-focus', daemon=True)
        self.thread.start()

    def _select(self, identity):
        window = self.windows.get(identity, {})
        current = _focus(window.get('id'), window.get('app_id'), window.get('pid'))
        if not self.initialized or current != self.current:
            self.current = current
            self.initialized = True
            self.changed(current)

    def event(self, event):
        if 'WindowsChanged' in event:
            windows = event['WindowsChanged']['windows']
            self.windows = {w['id']: w for w in windows}
            self._select(next((w['id'] for w in windows if w.get('is_focused')), None))
        elif 'WindowOpenedOrChanged' in event:
            window = event['WindowOpenedOrChanged']['window']
            self.windows[window['id']] = window
            if window.get('is_focused'):
                self._select(window['id'])
        elif 'WindowFocusChanged' in event:
            self._select(event['WindowFocusChanged']['id'])
        elif 'WindowClosed' in event:
            identity = event['WindowClosed']['id']
            self.windows.pop(identity, None)
            if self.current.id == identity:
                self._select(None)

    def _run(self):
        try:
            with self.sock.makefile('rb') as stream:
                while not self.closed.is_set():
                    line = stream.readline(4 * 1024 * 1024)
                    if not line or not line.endswith(b'\n'):
                        raise OSError('Niri focus event stream ended')
                    self.event(json.loads(line))
        except Exception as exc:
            if not self.closed.is_set():
                self.failed(str(exc))

    def close(self):
        self.closed.set()
        try:
            self.sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        self.sock.close()
        self.thread.join(1)
