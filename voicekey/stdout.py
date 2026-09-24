"""Thin stdout client for the running daemon; no model or desktop imports."""
from __future__ import annotations

import json
from pathlib import Path
import signal
import socket
import sys
import time

from .control import PROTOCOL_VERSION, socket_path


def capture(*, wav=None, seconds=None, path=None) -> int:
    """Ctrl-C finishes; SIGTERM cancels. Configuration belongs to the daemon."""
    stopped = aborted = False

    def stop(signum, frame):
        nonlocal stopped, aborted
        stopped = True
        aborted |= signum == signal.SIGTERM

    previous = {sig: signal.signal(sig, stop) for sig in (signal.SIGINT, signal.SIGTERM)}
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
            client.settimeout(3)
            client.connect(str(path or socket_path()))
            client.settimeout(.1)
            buffer = b''
            ready = False
            identity = None
            pending = {}
            next_id = 0
            finish_sent = cancel_sent = False
            greeting_deadline = time.monotonic() + 3

            def send(command, args):
                nonlocal next_id
                next_id += 1
                client.sendall((json.dumps({'id': next_id, 'command': command, 'args': args}) + '\n').encode())
                pending[next_id] = time.monotonic() + 3

            while True:
                if identity is not None:
                    if aborted and not cancel_sent:
                        send('capture-cancel', {'capture_id': identity})
                        cancel_sent = True
                    elif stopped and not finish_sent and not cancel_sent:
                        send('capture-finish', {'capture_id': identity})
                        finish_sent = True
                now = time.monotonic()
                if ((not ready and now >= greeting_deadline)
                        or any(now >= deadline for deadline in pending.values())):
                    raise RuntimeError('Daemon did not acknowledge the command')
                try:
                    data = client.recv(65536)
                except socket.timeout:
                    continue
                if not data:
                    raise RuntimeError('Daemon disconnected; recover prepared text with --last')
                buffer += data
                if len(buffer) > 1024 * 1024:
                    raise RuntimeError('Oversized daemon response')
                while b'\n' in buffer:
                    line, buffer = buffer.split(b'\n', 1)
                    message = json.loads(line)
                    if not ready:
                        if message.get('type') != 'status' or message.get('protocol_version') != PROTOCOL_VERSION:
                            raise RuntimeError('Daemon lacks client capture support; restart voicekey.service')
                        ready = True
                        args = {}
                        if seconds is not None:
                            args['seconds'] = seconds
                        if wav is not None:
                            args['wav'] = str(Path(wav).resolve())
                        send('capture-start', args)
                    elif message.get('type') == 'reply':
                        pending.pop(message.get('id'), None)
                        if message.get('error'):
                            raise RuntimeError(message['error'])
                        if message.get('id') == 1:
                            identity = message['capture_id']
                    elif message.get('type') == 'capture-progress' and message.get('capture_id') == identity:
                        if message.get('state') == 'recording':
                            print('Recording; Ctrl-C finishes, SIGTERM cancels.', file=sys.stderr, flush=True)
                    elif message.get('type') == 'capture-result' and message.get('capture_id') == identity:
                        if aborted or message.get('error') == 'cancelled':
                            return 130
                        if message.get('error'):
                            raise RuntimeError(message.get('reason') or message['error'])
                        sys.stdout.write(message['text'])
                        sys.stdout.flush()
                        return 0
    except Exception as exc:
        print(f'voicekey capture: {exc}', file=sys.stderr)
        return 130 if aborted else 1
    finally:
        for sig, handler in previous.items():
            signal.signal(sig, handler)
