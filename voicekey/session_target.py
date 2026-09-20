"""One destination/preview lifetime; independent, single-use delivery attempts."""
import threading
import time
import queue

from . import emacs, inject
from .delivery import UnsafeText
from .ime import ImeHung
from .ledger import Stage
from .spacing import spaced
from .target import (Target, EmacsTarget, ImeTarget, WtypeTarget, ImePreview,
                     NotifyPreview, Landing, Outcome)


def join_text(parts):
    result = ""
    for part in parts:
        if part:
            result += spaced(" " if result and not result[-1].isspace() else "", part)
    return result


def preview_text(text):
    # Wayland string payloads have a 4000-byte ceiling. Full text stays in the ledger.
    return text if len(text.encode()) <= 3900 else text.encode()[:3896].decode(errors="ignore") + "…"


class SessionTarget:
    def __init__(self, identity, target, ledger, *, scoped=False):
        self.id, self.target, self.ledger = identity, target, ledger
        self.members = set()
        self.scoped = scoped
        self.departed = threading.Event()
        self.failed = threading.Event()
        self.closed = False
        self._lock = threading.Lock()
        self._omitted = set()
        self._retired = queue.SimpleQueue()
        self._last = None
        self._fallback = NotifyPreview("dictate")

    def attempt(self, identity):
        self.members.add(identity)
        return UtteranceTarget(self, identity)

    def _text(self, exclude=""):
        while True:
            try:
                retired = self._retired.get_nowait()
                self._omitted.add(retired)
                self.members.discard(retired)
            except queue.Empty:
                break
        snapshots = [u for u in self.ledger.snapshots() if u.session_id == self.id
                     and (not self.scoped or u.id in self.members)]
        self._omitted.intersection_update(u.id for u in snapshots)
        return preview_text(join_text((u.final if u.stage in (Stage.READY, Stage.DELIVERING)
                                       else u.raw or u.live) for u in snapshots
                                     if u.id != exclude and u.id not in self._omitted))

    def available(self):
        if self.failed.is_set() or self.closed:
            return False
        if isinstance(self.target, EmacsTarget):
            return True  # the editor checks the pinned buffer at each transaction
        if isinstance(self.target, ImeTarget):
            return self.target.ime.activation() == self.target.preview.generation
        return self.target.window.focused(time.monotonic() + 0.2)

    def _show(self, text):
        if self.closed or self.departed.is_set():
            return
        preview = self.target.preview
        if isinstance(preview, ImePreview) and preview.ime.activation() != preview.generation:
            self._fallback.show(text)
        else:
            preview.show(text)

    def render(self):
        if not self._lock.acquire(blocking=False):
            return
        try:
            if self.closed or self.failed.is_set():
                return
            text = self._text()
            if text != self._last:
                self._show(text)
                self._last = text
        finally:
            self._lock.release()

    def retire(self, identity):
        self._retired.put(identity)
        self._last = None

    def deliver(self, identity, text, deadline, operation, prefix, permit, cancelled):
        if not self._lock.acquire(timeout=max(0, deadline - time.monotonic())):
            return Landing(reason="session preview is busy")
        try:
            if self.closed or self.failed.is_set() or cancelled.is_set() or time.monotonic() >= deadline:
                return Landing(reason="persistent destination is unavailable")
            target = self.target
            if self.departed.is_set() and not isinstance(target, EmacsTarget):
                return Landing(reason="focus moved; pending text kept for recovery")
            tail = self._text(exclude=identity)
            if isinstance(target, EmacsTarget):
                target.pinning.before(max(0, deadline - time.monotonic()))
                if not target.pinning.valid:
                    landing = Landing(reason=target.pinning.reason or "Emacs did not acknowledge the session buffer")
                else:
                    preview = target.preview
                    try:
                        cleared = (preview.ime.clear_preedit(preview.generation, preview.owner,
                            timeout=max(0, deadline - time.monotonic())) if isinstance(preview, ImePreview) else True)
                    except ImeHung:
                        cleared = False
                    if not cleared or cancelled.is_set():
                        landing = Landing(reason="preview cleanup did not finish")
                    else:
                        try:
                            emacs.insert(text, target.pinning.id, timeout=max(0, deadline - time.monotonic()),
                                         operation_id=operation, prefix=prefix, permit=permit, keep_pin=True)
                            landing = Landing(Outcome.CONFIRMED)
                        except emacs.EmacsRefused as exc:
                            landing = Landing(reason=str(exc))
                        except emacs.EmacsError as exc:
                            landing = Landing(Outcome.UNKNOWN, str(exc))
            elif isinstance(target, ImeTarget):
                if not target.window.focused(deadline):
                    landing = Landing(reason="the original window lost focus")
                else:
                    try:
                        sent = target.ime.commit(text, target.preview.generation,
                            timeout=max(0, deadline - time.monotonic()), owner=target.preview.owner,
                            prefix=prefix, cancelled=cancelled, tail=spaced(" " if text else "", tail),
                            allow_formatting=target.preview.allow_formatting)
                        landing = Landing(Outcome.SUBMITTED) if sent else Landing(reason="the original field activation ended")
                    except UnsafeText as exc:
                        landing = Landing(reason=str(exc))
                    except ImeHung as exc:
                        landing = Landing(Outcome.UNKNOWN, str(exc))
            elif isinstance(target, WtypeTarget):
                if not target.window.focused(deadline) or cancelled.is_set():
                    landing = Landing(reason="the original window lost focus")
                else:
                    try:
                        inject.type_text(spaced(prefix, text), timeout=max(0, deadline - time.monotonic()))
                        landing = Landing(Outcome.SUBMITTED)
                    except UnsafeText as exc:
                        landing = Landing(reason=str(exc))
                    except FileNotFoundError as exc:
                        landing = Landing(reason=str(exc))
                    except Exception as exc:
                        landing = Landing(Outcome.UNKNOWN, str(exc))
            else:
                landing = Landing(reason="persistent mode needs an insertion destination")
            if landing.landed:
                self._omitted.add(identity)
                self.target.prefix = " " if text and not text[-1].isspace() else ""
                self._show(tail)
                self._last = tail
            else:
                self.failed.set()
            return landing
        finally:
            self._lock.release()

    def leave(self):
        """Retire the preview; pending Emacs text can still use its old pin."""
        self.departed.set()
        self.target.preview.clear()
        self._fallback.clear()

    def close(self):
        self.closed = True
        self.target.cancel()
        self._fallback.clear()
        if isinstance(self.target, EmacsTarget):
            try:
                emacs.unpin(self.target.pinning.id)
            except emacs.EmacsError:
                pass


class UtteranceTarget(Target):
    def __init__(self, session, identity):
        target = session.target
        super().__init__(target.preview, target.window, target.app_id)
        self.session, self.identity = session, identity
        self.context_key = id(session)

    def show(self, text):
        pass  # the ledger supplies all tiers to the single session renderer

    def clear(self):
        self.session.retire(self.identity)

    def cancel(self):
        self.cancelled.set()
        self.session.failed.set()
        self.clear()

    def completed(self, outcome):
        if outcome not in (Outcome.CONFIRMED, Outcome.SUBMITTED, Outcome.DROPPED):
            self.session.failed.set()

    def describe(self):
        return self.session.target.describe()

    def _land(self, text, deadline, operation_id, prefix):
        return self.session.deliver(self.identity, text, deadline, operation_id, prefix, self.permit, self.cancelled)
