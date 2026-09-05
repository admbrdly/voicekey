"""An acknowledged buffer binding and expiring, deduplicated editor operations.

A successful pin names the buffer when Emacs handles the request. It must
answer promptly; a delayed pin is never treated as a key-down snapshot.
Optional voicekey-tracking-mode binds the last command-loop buffer instead.
"""
from __future__ import annotations

import ast
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


def _lisp_string(text: str) -> str:
    return '"' + text.replace('\\', '\\\\').replace('"', '\\"') + '"'


def _form(body: str) -> str:
    return (f'(progn (unless (and (boundp \'voicekey--protocol-version) '
            f'(= voicekey--protocol-version 1)) (load {_lisp_string(LIBRARY)} nil t)) {body})')


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


def pin(pin_id: str | None = None) -> Pin:
    pin_id = pin_id or secrets.token_hex(8)
    expires = time.time() + PIN_TIMEOUT
    try:
        value = _eval(_form(f'(voicekey--pin {_lisp_string(pin_id)} {expires!r})'), PIN_TIMEOUT)
    except EmacsError as exc:
        log.info("could not bind Emacs: %s", exc)
        return Pin(pin_id, None, False)
    return Pin(pin_id, value)


class PendingPin:
    def __init__(self):
        self.id = secrets.token_hex(8)
        self._pin = Pin(self.id, None, False)
        self._done = threading.Event()
        self.thread = threading.Thread(target=self._run, name="emacs-pin", daemon=True)
        self.thread.start()

    def _run(self):
        try:
            self._pin = pin(self.id)
        finally:
            self._done.set()

    @property
    def valid(self):
        return self._done.is_set() and self._pin.valid

    def before(self, wait: float):
        self._done.wait(max(0.0, wait))
        return self._pin.before


def insert(text: str, pin_id: str, timeout: float = TIMEOUT, *,
           operation_id: str | None = None, prefix: str = "", permit: str | None = None) -> None:
    operation_id = operation_id or secrets.token_hex(16)
    expires = time.time() + max(0, timeout)
    permission = _lisp_string(permit) if permit is not None else "nil"
    body = (f'(voicekey--insert {_lisp_string(pin_id)} {_lisp_string(operation_id)} '
            f'{expires!r} {_lisp_string(text)} {_lisp_string(prefix)} {permission})')
    value = _eval(_form(body), timeout)
    if value != "ok":
        raise EmacsError("Emacs did not confirm insertion")
