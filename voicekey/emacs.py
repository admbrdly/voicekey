"""An acknowledged buffer binding and expiring, deduplicated editor operations.

A successful pin names the buffer when Emacs handles the request. It must
answer promptly; a delayed pin is never treated as a key-down snapshot.
Optional voicekey-tracking-mode binds the last command-loop buffer instead.

emacsclient reaches one Emacs server. When the compositor reports which
process owns the focused window, the pin request names it and the server
refuses to bind a buffer for a window of another Emacs process. The
acknowledgement describes the pinned buffer for logs and the journal.
"""
from __future__ import annotations

import ast
import json
import logging
import secrets
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger("voicekey.emacs")
TIMEOUT = 5.0
PIN_TIMEOUT = 0.25
PROTOCOL_VERSION = 3
LIBRARY = str(Path(__file__).with_name("voicekey.el"))


class EmacsError(Exception):
    """An operation may have started; callers preserve rather than repeat it."""


class EmacsRefused(EmacsError):
    """A definite refusal before mutation."""


class EmacsTimeout(EmacsError):
    pass


@dataclass(frozen=True)
class Pin:
    id: str
    before: str | None
    valid: bool = True
    buffer: str = ""
    mode: str = ""
    read_only: bool = False
    state: str = ""
    reason: str = ""

    def describe(self) -> str:
        if not self.valid:
            return f"no pinned buffer ({self.reason})" if self.reason else "no pinned buffer"
        details = [self.mode]
        if self.read_only:
            details.append("read-only")
        if self.state and self.state != "none":
            details.append(f"evil {self.state}")
        return f"buffer {self.buffer!r} ({', '.join(d for d in details if d)})"


def _lisp_string(text: str) -> str:
    return '"' + text.replace('\\', '\\\\').replace('"', '\\"') + '"'


def _form(body: str) -> str:
    return (f'(progn (unless (and (boundp \'voicekey--protocol-version) '
            f'(= voicekey--protocol-version {PROTOCOL_VERSION})) (load {_lisp_string(LIBRARY)} nil t)) {body})')


def _eval(form: str, timeout: float = TIMEOUT) -> str:
    if timeout <= 0:
        raise EmacsRefused("operation expired before submission")
    try:
        result = subprocess.run(["emacsclient", "-e", form], capture_output=True,
                                text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        raise EmacsTimeout("Emacs did not acknowledge the operation; it may have executed")
    except OSError as exc:
        raise EmacsRefused(f"emacsclient could not start: {exc}") from exc
    output = (result.stdout + result.stderr).strip()
    if result.returncode != 0 or output.startswith("*ERROR*"):
        raise EmacsError(output.removeprefix("*ERROR*:").strip() or "emacsclient failed")
    try:
        value = ast.literal_eval(result.stdout.strip())
    except (ValueError, SyntaxError) as exc:
        raise EmacsError("unrecognised Emacs acknowledgement") from exc
    if not isinstance(value, str):
        raise EmacsError("unrecognised Emacs acknowledgement")
    if value.startswith("refused:"):
        raise EmacsRefused(value.removeprefix("refused:").strip())
    if value.startswith("unknown:"):
        raise EmacsError(value.removeprefix("unknown:").strip())
    return value


def _acknowledgement(value: str) -> dict:
    """The pin's JSON description of the bound buffer."""
    try:
        fields = json.loads(value)
    except ValueError as exc:
        raise EmacsError("unrecognised pin acknowledgement") from exc
    if not isinstance(fields, dict) or not isinstance(fields.get("before"), str):
        raise EmacsError("unrecognised pin acknowledgement")
    return fields


def pin(pin_id: str | None = None, pid: int | None = None) -> Pin:
    """Bind the buffer Emacs selects now. PID is the focused window's process, when known."""
    pin_id = pin_id or secrets.token_hex(8)
    expires = time.time() + PIN_TIMEOUT
    owner = "nil" if pid is None else str(int(pid))
    try:
        fields = _acknowledgement(
            _eval(_form(f'(voicekey--pin {_lisp_string(pin_id)} {expires!r} {owner})'), PIN_TIMEOUT))
    except EmacsTimeout:
        reason = f"Emacs did not acknowledge the buffer pin within {PIN_TIMEOUT:.2f}s"
        log.info("could not bind Emacs: %s", reason)
        return Pin(pin_id, None, False, reason=reason)
    except EmacsError as exc:
        log.info("could not bind Emacs: %s", exc)
        return Pin(pin_id, None, False, reason=str(exc))
    pinned = Pin(pin_id, fields["before"], True, buffer=str(fields.get("buffer", "")),
                 mode=str(fields.get("mode", "")), read_only=fields.get("read_only") is True,
                 state=str(fields.get("state", "")))
    log.info("pinned Emacs %s", pinned.describe())
    return pinned


class PendingPin:
    def __init__(self, pid: int | None = None):
        self.id = secrets.token_hex(8)
        self.pid = pid
        self._pin = Pin(self.id, None, False)
        self._done = threading.Event()
        self.thread = threading.Thread(target=self._run, name="emacs-pin", daemon=True)
        self.thread.start()

    def _run(self):
        try:
            self._pin = pin(self.id, self.pid)
        finally:
            self._done.set()

    @property
    def valid(self):
        return self._done.is_set() and self._pin.valid

    @property
    def reason(self) -> str:
        """Why no buffer is bound, once Emacs has answered."""
        return self._pin.reason if self._done.is_set() else ""

    def describe(self) -> str:
        return self._pin.describe() if self._done.is_set() else "pin pending"

    def before(self, wait: float):
        self._done.wait(max(0.0, wait))
        return self._pin.before


def insert(text: str, pin_id: str, timeout: float = TIMEOUT, *,
           operation_id: str | None = None, prefix: str = "", permit: str | None = None,
           keep_pin: bool = False) -> None:
    operation_id = operation_id or secrets.token_hex(16)
    expires = time.time() + max(0, timeout)
    permission = _lisp_string(permit) if permit is not None else "nil"
    body = (f'(voicekey--insert {_lisp_string(pin_id)} {_lisp_string(operation_id)} '
            f'{expires!r} {_lisp_string(text)} {_lisp_string(prefix)} {permission} {"t" if keep_pin else "nil"})')
    value = _eval(_form(body), timeout)
    if value != "ok":
        raise EmacsError("Emacs did not confirm insertion")


def unpin(pin_id: str) -> None:
    _eval(_form(f'(voicekey--unpin {_lisp_string(pin_id)})'), 0.25)
