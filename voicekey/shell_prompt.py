"""Best-effort Ghostty prompt routing using its existing Bash directory title.

Titles can be stale or forged, especially across surfaces sharing one process.
The title is routing evidence, not a verified per-surface identity.
A prompt-shaped title alone is insufficient: match a foreground Bash's cwd too.
"""
from dataclasses import dataclass
from pathlib import Path
import os
import time

from . import focus, nvim
from .target import Window


@dataclass(frozen=True)
class Process:
    name: str
    state: str
    group: int
    tty: int
    foreground: int
    started: int


def process(pid):
    try:
        text = (Path('/proc') / str(pid) / 'stat').read_text()
        end = text.rindex(')')
        fields = text[end+2:].split()
        return Process(text[text.index('(')+1:end], fields[0], int(fields[2]),
                       int(fields[4]), int(fields[5]), int(fields[19]))
    except (OSError, ValueError, IndexError):
        return None


def working_directory(pid):
    try:
        return os.readlink(Path('/proc') / str(pid) / 'cwd')
    except OSError:
        return None


def candidates(destination):
    """Matching foreground shells; this does not prove which surface is focused."""
    title = destination.title
    if (destination.app_id != 'com.mitchellh.ghostty' or destination.pid is None
            or not (title == '~' or title.startswith('~/') or title.startswith('/'))):
        return {}
    directory = os.path.realpath(os.path.expanduser(title))
    tree = nvim.process_tree()
    matches = {}
    for pid, (_, name) in tree.items():
        if name != 'bash' or not nvim.descendant(pid, destination.pid, tree):
            continue
        shell = process(pid)
        if (shell is None or shell.name != 'bash' or shell.state in ('T', 't', 'Z', 'X')
                or not shell.tty or shell.group != pid or shell.foreground != shell.group
                or working_directory(pid) != directory):
            continue
        # Job control off can leave a command in the shell's own foreground group.
        for child in tree:
            if child != pid and nvim.descendant(child, pid, tree):
                current = process(child)
                if current is None or (current.tty == shell.tty and current.group == shell.group):
                    break
        else:
            matches[pid] = shell.started
    return matches


class PromptWindow(Window):
    """Bind the observed window/title/shells and check them before typing."""
    def __init__(self, destination, shells):
        super().__init__(destination.id, True, destination.pid)
        self.destination, self.shells = destination, shells
        self._checked, self._issue = 0., ''

    def focused(self, deadline):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return False
        current = focus.focused(timeout=min(.2, remaining))
        if current.id is None or current != self.destination or current.title != self.destination.title:
            return False
        matching = candidates(current)
        return (any(matching.get(pid) == started for pid, started in self.shells.items())
                and time.monotonic() < deadline)

    def availability_issue(self, deadline=None):
        # Periodic capture checks; delivery always uses focused() afresh.
        if time.monotonic() >= self._checked:
            end = time.monotonic() + .2
            if deadline is not None:
                end = min(end, deadline)
            self._issue = '' if self.focused(end) else 'The bound shell prompt changed; speech kept for recovery'
            self._checked = time.monotonic() + 1
        return self._issue
