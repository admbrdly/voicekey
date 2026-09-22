"""Local status/control socket. Mutations run on the keyboard controller thread.

The DMS widget subscribes to status; no model imports, polling subprocesses or
second copy of dictation state are needed in the shell. Slow clients are dropped.
"""
import json
import os
from pathlib import Path
import queue
import selectors
import socket
import threading
import time

COMMANDS = ('status', 'start', 'start-typing', 'stop', 'pause-on-switch', 'follow-focus', 'pin', 'free-memory')


def socket_path():
    return Path(os.environ.get('XDG_RUNTIME_DIR', f'/run/user/{os.getuid()}')) / 'voicekey' / 'control.sock'


class ControlServer:
    def __init__(self, path=None):
        self.path = Path(path) if path else socket_path()
        self.commands = queue.Queue(maxsize=32)
        self.responses = queue.SimpleQueue()
        self.status = {'state': 'loading', 'listening': False}
        self.closed = threading.Event()
        self.thread = None
        self.listener = None

    def start(self):
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        # Refuse to unlink a live server (e.g. a second daemon invocation).
        if self.path.exists():
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as probe:
                probe.settimeout(.2)
                try:
                    probe.connect(str(self.path))
                except ConnectionRefusedError:
                    self.path.unlink()
                else:
                    raise OSError('Voicekey control socket is already in use')
        self.listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            self.listener.bind(str(self.path))
            self.path.chmod(0o600)
            self.listener.listen(8)
            self.listener.setblocking(False)
        except Exception:
            self.listener.close()
            self.listener = None
            raise
        self.thread = threading.Thread(target=self._run, name='voicekey-control', daemon=True)
        self.thread.start()

    def publish(self, status):
        self.status = dict(status)

    def drain(self, handler):
        for _ in range(32):
            try:
                client, message, deadline = self.commands.get_nowait()
            except queue.Empty:
                break
            error = None
            if time.monotonic() > deadline or client.fileno() < 0:
                error = 'Control request expired'
            else:
                try:
                    handler(message['command'])
                except (ValueError, RuntimeError) as exc:
                    error = str(exc)
            self.responses.put((client, {'type': 'reply', 'id': message.get('id'), 'error': error}))

    def _run(self):
        clients = {}
        with selectors.DefaultSelector() as selector:
            selector.register(self.listener, selectors.EVENT_READ)
            def drop(client):
                if client in clients:
                    selector.unregister(client)
                    clients.pop(client)
                    client.close()
            def send(client, message):
                data = (json.dumps(message, ensure_ascii=True) + '\n').encode()
                try:
                    if client.send(data) != len(data):
                        drop(client)
                except OSError:
                    drop(client)
            previous, heartbeat = None, 0.0
            try:
                while not self.closed.is_set():
                    for key, _ in selector.select(.1):
                        client = key.fileobj
                        if client is self.listener:
                            client, _ = self.listener.accept()
                            if len(clients) >= 16:
                                client.close()
                                continue
                            client.setblocking(False)
                            clients[client] = b''
                            selector.register(client, selectors.EVENT_READ)
                            send(client, {'type': 'status', **self.status})
                            continue
                        try:
                            data = client.recv(4096)
                            if not data or len(clients[client]) + len(data) > 8192:
                                drop(client)
                                continue
                            clients[client] += data
                            while client in clients and b'\n' in clients[client]:
                                line, clients[client] = clients[client].split(b'\n', 1)
                                message = json.loads(line)
                                if not isinstance(message, dict) or message.get('command') not in COMMANDS:
                                    raise ValueError('Unknown control command')
                                if message['command'] == 'status':
                                    send(client, {'type': 'reply', 'id': message.get('id'), 'error': None, **self.status})
                                else:
                                    self.commands.put_nowait((client, message, time.monotonic() + 2))
                        except (OSError, ValueError, queue.Full):
                            drop(client)
                    while not self.responses.empty():
                        client, message = self.responses.get_nowait()
                        if client in clients:
                            send(client, message)
                    status = self.status
                    if status != previous or time.monotonic() - heartbeat > 2:
                        for client in tuple(clients):
                            send(client, {'type': 'status', **status})
                        previous, heartbeat = status, time.monotonic()
            finally:
                for client in tuple(clients):
                    drop(client)

    def close(self):
        self.closed.set()
        if self.thread is not None:
            self.thread.join(1)
        if self.listener is not None:
            self.listener.close()
            self.path.unlink(missing_ok=True)
            self.listener = None


def request(command, path=None):
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
        client.settimeout(3)
        client.connect(str(path or socket_path()))
        client.sendall((json.dumps({'command': command, 'id': 1}) + '\n').encode())
        with client.makefile('rb') as stream:
            while line := stream.readline(65536):
                result = json.loads(line)
                if result.get('type') == 'reply':
                    return result
    raise OSError('Voicekey disconnected before replying')
