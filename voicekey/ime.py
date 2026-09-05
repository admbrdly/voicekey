"""Live text in the focused field through Wayland's input-method protocol.

The daemon registers as the compositor's input method (zwp_input_method_v2).
When an application focuses a text field that speaks text-input-v3, the
compositor *activates* us; we may then show preedit text — provisional, drawn
inline by the application, never inserted — and finally commit a string, which
replaces the preedit in place. Applications without text-input support never
activate us; callers check ``activation()`` and fall back to notifications
and wtype.

One connection, one thread. ``preedit`` is fire-and-forget, ``commit`` waits.
Every request names an activation generation; a later field's activation
cannot consume it. Surrounding state is published on done, and replacement
requires the exact checked snapshot. Preview updates are coalesced separately
from commands. A flushed commit is submission to the compositor, not an
application acknowledgement. Started failures remain uncertain.
"""

from __future__ import annotations

import logging
import os
import queue
import select
import socket
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass

log = logging.getLogger("voicekey.ime")

CALL_TIMEOUT = 2.0  # seconds a call may stay pending before it is cancelled
STARTED_TIMEOUT = 10.0  # seconds a started call may run before the connection is written off

EVENTS = (
    "activate", "deactivate", "surrounding_text", "text_change_cause",
    "content_type", "done", "unavailable",
)


class ImeUnavailable(Exception):
    """No Wayland display, no input-method support, or another IME is bound."""


class ImeHung(Exception):
    """A started request did not complete: the compositor stopped responding.
    The connection has been severed, but a request already handed to the
    kernel may still reach the compositor when it recovers."""


@dataclass(frozen=True)
class Snapshot:
    generation: int | None
    serial: int
    surrounding: tuple[str, int] | None


class InputMethod:
    def __init__(self) -> None:
        try:
            from pywayland.client import Display
            from pywayland.protocol.wayland import WlSeat

            from ._input_method_v2 import ZwpInputMethodManagerV2
        except ImportError as exc:
            raise ImeUnavailable(f"pywayland is not installed: {exc}")
        self._reset()
        try:
            self._display = Display()
            self._display.connect()
        except Exception as exc:
            self._close_pipe()
            raise ImeUnavailable(f"cannot connect to the Wayland display: {exc}")
        self._seat = self._manager = None

        def on_global(registry, name, interface, version):
            if interface == "wl_seat" and self._seat is None:
                self._seat = registry.bind(name, WlSeat, min(version, 7))
            elif interface == "zwp_input_method_manager_v2":
                self._manager = registry.bind(name, ZwpInputMethodManagerV2, 1)

        try:
            registry = self._display.get_registry()
            registry.dispatcher["global"] = on_global
            self._display.roundtrip()
            if self._seat is None or self._manager is None:
                raise ImeUnavailable("the compositor does not offer zwp_input_method_v2")
            self._im = None
            if not self._bind():
                raise ImeUnavailable("another input method is already bound")
        except BaseException:
            # Whatever failed — a refusal above or a compositor that went
            # away mid-roundtrip — leaves neither connection nor pipe behind.
            try:
                self._display.disconnect()
            except Exception:
                pass
            self._close_pipe()
            raise
        self._thread = threading.Thread(target=self._run, name="ime", daemon=True)
        self._thread.start()

    def _reset(self) -> None:
        self._active = False
        self._pending_active = False
        self._generation = 0
        self._serial = 0  # number of `done` events received; echoed in commit()
        self._unavailable = False
        self._surrounding: tuple[str, int] | None = None
        self._pending_surrounding: tuple[str, int] | None = None
        self._activated = False
        self._snapshot = Snapshot(None, 0, None)
        self._shown = ""  # preedit the field is showing now (applied requests only)
        self._left_showing = ""  # preedit the field had when it was last deactivated
        self._history: OrderedDict[int, str] = OrderedDict()
        self._dead = False
        self._closing = False
        self._commands: queue.Queue = queue.Queue(maxsize=64)
        self._wake_r, self._wake_w = os.pipe()
        os.set_blocking(self._wake_w, False)
        self._preview_lock = threading.Lock()
        self._preview_owner = None
        self._preview_text = ""
        self._preview_pending = None

    def _close_pipe(self) -> None:
        for name in ("_wake_r", "_wake_w"):
            fd = getattr(self, name)
            setattr(self, name, -1)
            try:
                os.close(fd)
            except OSError:
                pass

    def _bind(self) -> bool:
        """(Re)create our input-method object.

        niri (smithay) keeps one input method per seat, and destroying *any*
        input-method object — even another client's stale one — silently
        drops the current instance without telling it. So the daemon rebinds
        before every recording: that reclaims the seat and guarantees the
        activation state that follows is fresh."""
        if self._im is not None:
            self._im.destroy()
        self._unavailable = False
        self._active = self._pending_active = False
        self._serial = 0  # the compositor counts `done` per object
        self._im = self._manager.get_input_method(self._seat)
        for event in EVENTS:
            self._im.dispatcher[event] = getattr(self, f"_on_{event}")
        self._display.roundtrip()
        return not self._unavailable

    # --- public, any thread ---

    def activation(self) -> int | None:
        """Generation of the current activation, or None when no field is active."""
        if self._dead or self._unavailable or not self._active:
            return None
        return self._generation

    def before_cursor(self) -> str | None:
        """The character before the cursor in the active field, "" when the
        application reports surrounding text with nothing before the cursor,
        None when it reports none at all (terminals)."""
        surrounding = self.surrounding_text()
        return None if surrounding is None else surrounding[0][-1:]

    def surrounding_text(self) -> tuple[str, str] | None:
        """(text before the cursor, text after) as the active field reports
        it — a window around the cursor, not the whole field — or None when
        it reports nothing (terminals)."""
        if not self._active or self._surrounding is None:
            return None
        text, cursor = self._surrounding
        raw = text.encode()
        return (raw[:cursor].decode(errors="ignore"), raw[cursor:].decode(errors="ignore"))

    def left_showing(self) -> str:
        """The preedit the field was showing when it was last deactivated;
        what the application did with it is for the caller to find out."""
        return self._left_showing

    def snapshot(self) -> Snapshot:
        state = self._snapshot
        return Snapshot(None, state.serial, None) if self.activation() is None else state

    def shown_for(self, generation: int) -> str:
        return self._shown if generation == self.activation() else self._history.get(generation, "")

    def claim_preview(self, owner: str) -> None:
        with self._preview_lock:
            self._preview_owner = owner
            self._preview_text = ""
            self._preview_pending = None

    def rebind(self, timeout: float | None = None) -> bool:
        """Bind afresh; the activation for a focused field follows shortly."""
        return self._call(self._bind, timeout=timeout)

    def preedit(self, text: str, generation: int, owner: str | None = None) -> None:
        if self._dead or self._closing:
            return
        with self._preview_lock:
            if owner is not None and owner != self._preview_owner:
                return
            self._preview_text = text
            self._preview_pending = (text, generation, owner)
        self._wake()

    def clear_preedit(self, generation: int, owner: str, *, timeout: float) -> bool:
        """Flush this preview's removal before a separate editor insertion.

        A stale activation or a newer owner has nothing of ours to clear.
        Success orders local submissions; it is not an editor acknowledgement.
        """
        if self.activation() != generation or owner != self._preview_owner:
            return True
        deadline = time.monotonic() + max(0, timeout)

        def clear():
            with self._preview_lock:
                if self.activation() != generation or owner != self._preview_owner:
                    return True
                self._preview_pending = None
                self._preview_text = ""
            return self._apply(generation, preedit="", owner=owner, deadline=deadline)

        return self._call(clear, timeout=timeout)

    def commit(self, text: str, generation: int, *, timeout: float | None = None,
               owner: str | None = None, prefix: str | None = None, cancelled=None) -> bool:
        """Insert TEXT in place of the preedit. False if the field went away."""
        deadline = time.monotonic() + (CALL_TIMEOUT if timeout is None else max(0, timeout))
        return self._call(lambda: self._apply(generation, commit=text, deadline=deadline,
                                             owner=owner, prefix=prefix, cancelled=cancelled), timeout=timeout)

    def replace(self, before: int, text: str, generation: int, *, expected: Snapshot,
                timeout: float | None = None) -> bool:
        """Delete BEFORE bytes before the cursor and insert TEXT there, in
        one step. False if the field went away."""
        deadline = time.monotonic() + (CALL_TIMEOUT if timeout is None else max(0, timeout))
        return self._call(lambda: self._apply(generation, commit=text, delete_before=before,
                                             expected=expected, deadline=deadline), timeout=timeout)

    def _call(self, function, *, timeout: float | None = None) -> bool:
        """Run FUNCTION on the loop thread and wait for its result.

        A call still pending when the timeout expires is cancelled, so it
        cannot fire later — after the caller has delivered the text another
        way. A call that has already started cannot be cancelled; the caller
        waits for its real result, since text may have landed — but not
        forever: a compositor that has hung for STARTED_TIMEOUT gets the
        connection written off rather than the daemon wedged."""
        if self._dead:
            return False
        lock = threading.Lock()
        state = {"started": False, "cancelled": False}
        done = threading.Event()
        result = []
        errors = []
        deadline = time.monotonic() + (CALL_TIMEOUT + STARTED_TIMEOUT if timeout is None else max(0, timeout))

        def run():
            with lock:
                if state["cancelled"] or time.monotonic() >= deadline:
                    done.set()
                    return
                state["started"] = True
            try:
                result.append(bool(function()))
            except Exception as exc:
                errors.append(exc)
            finally:
                done.set()

        if not self._post(run):
            return False
        if not done.wait(min(CALL_TIMEOUT, max(0, deadline - time.monotonic()))):
            with lock:
                if not state["started"]:
                    state["cancelled"] = True
                    log.warning("the input method did not respond within %.0fs", CALL_TIMEOUT)
                    return False
            if not done.wait(max(0, deadline - time.monotonic())):
                self._dead = True
                self._active = False
                self._sever()
                raise ImeHung("the input method stopped responding; in-field text is off until restart")
        if errors:
            self._dead = True
            self._sever()
            raise ImeHung(f"a started input-method request failed: {errors[0]}") from errors[0]
        return bool(result and result[0])

    def _sever(self) -> None:
        """Shut the socket down from this thread so the loop thread's blocked
        request fails instead of completing later."""
        try:
            with socket.socket(fileno=os.dup(self._display.get_fd())) as connection:
                connection.shutdown(socket.SHUT_RDWR)
        except Exception as exc:
            log.warning("could not sever the input-method connection: %s", exc)

    def close(self) -> None:
        if self._closing:
            return
        self._closing = True
        self._wake()
        self._thread.join(2.0)
        if self._thread.is_alive():
            self._sever()
            self._thread.join(0.2)

    # --- protocol events (loop thread); state is applied on `done` ---

    def _on_activate(self, im) -> None:
        self._pending_active = True
        self._activated = True
        self._pending_surrounding = None

    def _on_deactivate(self, im) -> None:
        self._pending_active = False

    def _on_surrounding_text(self, im, text, cursor, anchor) -> None:
        self._pending_surrounding = (text, cursor)

    def _on_text_change_cause(self, im, cause) -> None:
        pass

    def _on_content_type(self, im, hint, purpose) -> None:
        pass

    def _on_done(self, im) -> None:
        self._serial += 1
        self._surrounding = self._pending_surrounding
        if self._pending_active != self._active or self._activated:
            if self._active:
                self._left_showing = self._shown
                self._history[self._generation] = self._shown
                while len(self._history) > 32:
                    self._history.popitem(last=False)
            self._active = self._pending_active
            if self._active:
                self._generation += 1
            else:
                self._left_showing = self._shown
                self._shown = ""
            log.debug("input method %s (generation %d)",
                      "active" if self._active else "inactive", self._generation)
            self._shown = ""
        self._activated = False
        self._snapshot = Snapshot(self.activation(), self._serial, self._surrounding)

    def _on_unavailable(self, im) -> None:
        self._unavailable = True
        self._active = self._pending_active = False
        self._snapshot = Snapshot(None, self._serial, None)
        log.warning("another client bound the input method; "
                    "in-field text is off until the next recording")

    # --- requests (loop thread) ---

    def _apply(self, generation: int, *, preedit: str | None = None,
               commit: str | None = None, delete_before: int = 0,
               expected: Snapshot | None = None, deadline: float | None = None,
               owner: str | None = None, prefix: str | None = None, cancelled=None) -> bool:
        if (self._dead or self._closing or self._unavailable or not self._active or self._generation != generation
                or (deadline is not None and time.monotonic() >= deadline)
                or (cancelled is not None and cancelled.is_set())):
            return False
        if delete_before and (expected is None or expected != self.snapshot()):
            return False
        if delete_before:
            surrounding = self.surrounding_text()
            if surrounding is None or delete_before > len(surrounding[0].encode()):
                return False
            try:
                surrounding[0].encode()[-delete_before:].decode()
            except UnicodeDecodeError:
                return False
        if preedit is not None and owner is not None and owner != self._preview_owner:
            return False
        if commit is not None and prefix is not None:
            from .spacing import owed, spaced
            commit = spaced(owed(self.before_cursor(), prefix), commit)
        # Text-input state is double-buffered and resets on every commit, so a
        # commit that carries no preedit request *removes* the preedit. Never
        # send an empty preedit string instead: GTK treats "" as a preedit that
        # is still present and skips preedit-end, which leaves Ghostty in its
        # composing state, swallowing every printable key.
        if delete_before:
            self._im.delete_surrounding_text(delete_before, 0)
        if commit is not None:
            self._im.commit_string(commit)
            self._shown = ""
            with self._preview_lock:
                if owner is None or owner == self._preview_owner:
                    self._preview_pending = None
                    self._preview_text = ""
                else:
                    tail = self._preview_text
                    if tail:
                        end = len(tail.encode())
                        self._im.set_preedit_string(tail, end, end)
                        self._shown = tail
        elif preedit:
            end = len(preedit.encode())
            self._im.set_preedit_string(preedit, end, end)
            self._shown = preedit
        else:
            self._shown = ""
        self._im.commit(self._serial)
        self._flush(deadline or time.monotonic() + CALL_TIMEOUT)
        return True

    def _flush(self, deadline: float) -> None:
        while self._display.flush() == -1:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise ImeHung("input-method output did not flush before its deadline")
            select.select([], [self._display.get_fd()], [], min(remaining, 0.05))
            time.sleep(min(remaining, 0.001))

    def _wake(self) -> None:
        try:
            os.write(self._wake_w, b"x")
        except (BlockingIOError, OSError):
            pass

    def _post(self, command) -> bool:
        if self._dead or self._closing:
            return False
        try:
            self._commands.put_nowait(command)
        except queue.Full:
            return False
        self._wake()
        return True

    def _run(self) -> None:
        try:
            fd = self._display.get_fd()
            while not self._closing:
                self._flush(time.monotonic() + CALL_TIMEOUT)
                readable, _, _ = select.select([fd, self._wake_r], [], [], 1.0)
                if fd in readable:
                    self._display.dispatch(block=True)
                if self._wake_r in readable:
                    os.read(self._wake_r, 4096)
                while True:
                    try:
                        command = self._commands.get_nowait()
                    except queue.Empty:
                        break
                    try:
                        command()
                    except Exception:
                        log.exception("input-method request failed")
                with self._preview_lock:
                    preview, self._preview_pending = self._preview_pending, None
                if preview is not None:
                    text, generation, owner = preview
                    self._apply(generation, preedit=text, owner=owner)
        except Exception:
            log.exception("input method connection failed; in-field text is off")
        finally:
            self._dead = True
            self._active = False
            try:
                self._display.disconnect()
            except Exception:
                pass
            self._close_pipe()
