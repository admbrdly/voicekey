"""Explicit spelling overrides and a bounded, optional shell transform.

These run after polish. Replacement text is literal and never matched again;
hooks receive text on stdin, never interpolated into the shell command.
"""
from __future__ import annotations

import logging
import os
import re
import signal
import subprocess
import tempfile
import time

MAX_TEXT_BYTES = 100000
HOOK_SECONDS = 5.0
log = logging.getLogger("voicekey.text")


def override(text: str, replacements: dict[str, str]) -> str:
    if not replacements:
        return text
    # Longest match wins. Only word-like edges need a boundary: this also
    # handles names such as C++ without treating replacement strings as regex.
    patterns = []
    values = {}
    for index, phrase in enumerate(sorted(replacements, key=len, reverse=True)):
        left = r"(?<!\w)" if phrase[0].isalnum() or phrase[0] == "_" else ""
        right = r"(?!\w)" if phrase[-1].isalnum() or phrase[-1] == "_" else ""
        name = f"word{index}"
        patterns.append(f"(?P<{name}>" + left + re.escape(phrase) + right + ")")
        values[name] = replacements[phrase]
    result = re.sub("|".join(patterns), lambda m: values[m.lastgroup], text, flags=re.IGNORECASE)
    if len(result.encode()) > MAX_TEXT_BYTES:
        raise ValueError("word overrides produced oversized text")
    return result


def hook(text: str, command: str, deadline: float) -> tuple[str, str]:
    """Return prepared text and a diagnostic. Every failure preserves input.

    Files avoid pipe deadlocks and unbounded in-memory output. Check output
    size while waiting, and kill the process group before closing the files.
    Descendants that deliberately detach are outside this timeout contract.
    """
    if not command:
        return text, "disabled"
    deadline = min(deadline, time.monotonic() + HOOK_SECONDS)
    proc = None
    try:
        if time.monotonic() >= deadline:
            raise TimeoutError("delivery deadline reached")
        with tempfile.TemporaryFile() as source, tempfile.TemporaryFile() as output:
            source.write(text.encode())
            source.seek(0)
            try:
                proc = subprocess.Popen(command, shell=True, stdin=source, stdout=output,
                                        stderr=subprocess.DEVNULL, start_new_session=True)
                while proc.poll() is None:
                    if os.fstat(output.fileno()).st_size > MAX_TEXT_BYTES:
                        raise ValueError("output exceeds text limit")
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise TimeoutError("timed out")
                    try:
                        proc.wait(timeout=min(0.02, remaining))
                    except subprocess.TimeoutExpired:
                        pass
                if proc.returncode:
                    raise ValueError(f"exit status {proc.returncode}")
                if time.monotonic() >= deadline:
                    raise TimeoutError("timed out")
            finally:
                if proc is not None:
                    try:
                        os.killpg(proc.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                    proc.wait(timeout=0.5)
            output.seek(0)
            data = output.read(MAX_TEXT_BYTES + 1)
            if len(data) > MAX_TEXT_BYTES:
                raise ValueError("output exceeds text limit")
            result = data.decode("utf-8")
            if "\x00" in result:
                raise ValueError("output contains NUL")
            return (result, "applied") if result.strip() else (text, "empty output; unchanged")
    except (OSError, ValueError, TimeoutError, subprocess.SubprocessError) as exc:
        reason = f"fallback: {exc}"
        log.warning("transcription hook %s", reason)
        return text, reason
