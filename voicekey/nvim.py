"""Neovim discovery and bounded JSON requests over its existing RPC socket.

Use Neovim's shipped remote-expr client: no extra Python runtime dependency or
home-grown MessagePack codec. Every subprocess is deadline bounded; no capture
client or shell is started. The Lua endpoint checks expiry and operation permits.
"""
from __future__ import annotations

from dataclasses import dataclass
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


@dataclass(frozen=True)
class Resolution:
    registration: dict | None = None
    reason: str = ''


# Ghostty's shell integration titles a surface with the command it runs;
# Neovim's own 'title' option ends its title with "NVIM".
EDITOR_COMMANDS = frozenset(('nvim', 'vim', 'vi', 'view', 'nvimdiff', 'vimdiff'))


def editor_title(title):
    words = (title or '').split()
    if not words or words[0].startswith('~') or (len(words) == 1 and os.path.isdir(words[0])):
        return False  # a prompt's directory title, e.g. ~/.config/nvim
    return os.path.basename(words[0]) in EDITOR_COMMANDS or title.rstrip().endswith(('NVIM', ' VIM'))


def resolve(destination):
    """A registered Neovim confirming focus gets buffer delivery.

    Otherwise a terminal keeps ordinary typing unless its title shows an
    editor in front, which is refused rather than sent as keystrokes. Neovims
    elsewhere in the terminal process never block typing: its PID cannot tell
    which tab or window is in front.
    """
    if not terminal(destination.app_id):
        return Resolution()
    tree = process_tree()
    processes = {pid for pid, (_, name) in tree.items() if name == 'nvim'
                 and (destination.pid is None or descendant(pid, destination.pid, tree))}
    candidates = []
    deadline = time.monotonic() + PIN_TIMEOUT
    for path in (runtime_dir() / 'nvim').glob('*.json'):
        if time.monotonic() >= deadline:
            break
        try:
            record = json.loads(path.read_text())
            pid, server = record['pid'], record['server']
            if type(pid) is not int or pid not in processes or not isinstance(server, str) or not server:
                continue
            answer = call(server, 'status', timeout=min(PROBE_TIMEOUT, deadline - time.monotonic()))
            if answer.get('pid') == pid and answer.get('server') == server and answer.get('focused') is True:
                candidates.append(record)
        except (OSError, ValueError, KeyError, TypeError, NvimError):
            continue
    if len(candidates) == 1:
        return Resolution(registration=candidates[0])
    if editor_title(destination.title):
        return Resolution(reason='Neovim appears to be in front but has not confirmed focus; '
                                 'terminal typing refused (switch away and back, then retry)')
    return Resolution()


def event_matches(destination, pid):
    return (terminal(destination.app_id) and
            (destination.pid is None or descendant(pid, destination.pid, process_tree())))
