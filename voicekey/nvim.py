"""Neovim discovery and bounded JSON requests over its existing RPC socket.

Use Neovim's shipped remote-expr client: no extra Python runtime dependency or
home-grown MessagePack codec. Every subprocess is deadline bounded; no capture
client or shell is started. The Lua endpoint checks expiry and operation permits.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import time

PIN_TIMEOUT = 0.25
PROBE_TIMEOUT = 0.1
TERMINALS = frozenset(('com.mitchellh.ghostty', 'foot', 'footclient', 'kitty',
                       'Alacritty', 'org.wezfurlong.wezterm'))


class NvimError(Exception):
    """Uncertain operation; never retry an insertion."""


class NvimRefused(NvimError):
    """Definitely refused before buffer mutation."""


def call(server, method, args=None, *, timeout=PIN_TIMEOUT):
    if timeout <= 0:
        raise NvimRefused('Neovim request expired before submission')
    deadline = time.monotonic() + timeout
    payload = json.dumps({'method': method, 'args': args or {},
                          'expires': time.time() + timeout}, ensure_ascii=True)
    # Vim single-quoted string only escapes apostrophes by doubling them.
    expression = 'luaeval("require(\'voicekey\').rpc(_A)", ' + "'" + payload.replace("'", "''") + "')"
    try:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise NvimRefused('Neovim request expired before submission')
        result = subprocess.run(['nvim', '--server', server, '--remote-expr', expression],
                                capture_output=True, text=True, timeout=remaining)
    except subprocess.TimeoutExpired as exc:
        raise NvimError('Neovim RPC timed out; the operation may have executed') from exc
    except OSError as exc:
        raise NvimRefused(f'Neovim RPC could not start: {exc}') from exc
    if time.monotonic() >= deadline:
        raise NvimError('Neovim acknowledgement arrived after the deadline; inspect the buffer')
    if result.returncode:
        raise NvimError('Neovim RPC failed: ' + result.stderr.strip()[:300])
    try:
        reply = json.loads(result.stdout)
        if not isinstance(reply, dict) or reply.get('status') not in ('ok', 'refused', 'unknown'):
            raise ValueError('invalid acknowledgement')
    except ValueError as exc:
        raise NvimError('Invalid Neovim acknowledgement') from exc
    if reply['status'] == 'refused':
        raise NvimRefused(reply.get('reason', 'Neovim refused'))
    if reply['status'] == 'unknown':
        raise NvimError(reply.get('reason', 'Neovim operation uncertain'))
    return reply


def runtime_dir():
    return Path(os.environ.get('XDG_RUNTIME_DIR', f'/run/user/{os.getuid()}')) / 'voicekey'


def process_tree():
    """One /proc snapshot; a terminal PID cannot distinguish its tabs/panes."""
    result = {}
    for path in Path('/proc').glob('[0-9]*/stat'):
        try:
            stat = path.read_text()
            end = stat.rindex(')')
            result[int(path.parent.name)] = (int(stat[end + 2:].split()[1]), stat[stat.index('(') + 1:end])
        except (OSError, ValueError, IndexError):
            continue
    return result


def descendant(pid, parent, tree):
    seen = set()
    while pid in tree and pid not in seen:
        if pid == parent:
            return True
        seen.add(pid)
        pid = tree[pid][0]
    return False


def terminal(app_id):
    return app_id in set(os.environ.get('VOICEKEY_TERMINALS', '').split()) | TERMINALS


def resolve(destination):
    """Return (registration, refusal). None/empty means ordinary terminal delivery.

    Multiple focused claims are ambiguous, never last-writer-wins. A single
    stale true focus flag remains a known limitation of terminal focus evidence.
    """
    if not terminal(destination.app_id):
        return None, ''
    tree = process_tree()
    processes = {pid for pid, (_, name) in tree.items() if name == 'nvim'
                 and (destination.pid is None or descendant(pid, destination.pid, tree))}
    candidates = []
    incomplete = False
    deadline = time.monotonic() + PIN_TIMEOUT
    paths = list((runtime_dir() / 'nvim').glob('*.json'))
    for path in paths:
        try:
            record = json.loads(path.read_text())
            pid, server = record['pid'], record['server']
            if type(pid) is not int or pid not in processes or not isinstance(server, str) or not server:
                continue
            if time.monotonic() >= deadline:
                incomplete = True
                break
            answer = call(server, 'status', timeout=min(PROBE_TIMEOUT, deadline - time.monotonic()))
            if answer.get('pid') == pid and answer.get('focused') is True and answer.get('server') == server:
                candidates.append(record)
        except (OSError, ValueError, KeyError, TypeError, NvimError):
            continue
    if len(candidates) == 1 and not incomplete:
        return candidates[0], ''
    if candidates or processes:
        return None, 'Neovim focus is unverified or ambiguous; terminal typing refused'
    return None, ''


def event_matches(destination, pid):
    return (terminal(destination.app_id) and
            (destination.pid is None or descendant(pid, destination.pid, process_tree())))
