"""Pinned Neovim destination; no failure can become terminal input."""
import threading
import time
import uuid

from . import nvim
from .target import PinnedEditorTarget, Landing, Outcome


class Pin:
    def __init__(self, server, pid):
        self.id, self.server, self.pid = uuid.uuid4().hex, server, pid
        self.valid, self.reason, self.buffer, self.prefix = False, '', '', None
        try:
            reply = nvim.call(server, 'pin', {'id': self.id})
            self.valid, self.buffer, self.prefix = True, reply['buffer'], reply['before']
        except (nvim.NvimError, KeyError) as exc:
            self.reason = str(exc)

    def before(self, wait=0):
        return self.prefix

    def describe(self):
        return f'buffer {self.buffer or "[No Name]"!r}' if self.valid else f'no pinned buffer ({self.reason})'


class Preview:
    name = 'Neovim virtual text'

    def __init__(self, pin):
        self.pin = pin
        self.closed = False
        self.lock = threading.Lock()

    def show(self, text, *, timeout=.1):
        # A slow preview never blocks delivery beyond its remaining budget.
        end = time.monotonic() + max(0, timeout)
        if not self.lock.acquire(timeout=max(0, timeout)):
            return
        try:
            if self.closed or not self.pin.valid:
                return
            try:
                nvim.call(self.pin.server, 'preview', {'id': self.pin.id, 'text': text},
                          timeout=max(0, end - time.monotonic()))
            except nvim.NvimError:
                pass  # delivery/health checks report unavailable pins
        finally:
            self.lock.release()

    def clear(self, *, timeout=None):
        budget = .1 if timeout is None else min(.1, timeout)
        end = time.monotonic() + budget
        if not self.lock.acquire(timeout=max(0, budget)):
            return False
        try:
            self.closed = True
            if not self.pin.valid:
                return True
            try:
                nvim.call(self.pin.server, 'preview', {'id': self.pin.id, 'text': ''},
                          timeout=max(0, end - time.monotonic()))
                return True
            except nvim.NvimError:
                return False
        finally:
            self.lock.release()


class NeovimTarget(PinnedEditorTarget):
    kind = 'neovim'

    @property
    def clipboard_fallback(self):
        # The pipeline only consults this after a definite refusal, never after
        # an uncertain insertion. Cancellation should not overwrite a clipboard.
        return not self.cancelled.is_set()

    @property
    def binding_refusal(self):
        return "" if self.pin_valid else self.pin_reason or "Neovim did not acknowledge the buffer pin"

    def __init__(self, window, app_id, registration):
        self.focus_identity = (registration['pid'], registration['server'])
        self.pinning = Pin(registration['server'], registration['pid'])
        super().__init__(Preview(self.pinning), window, app_id)
        self._health_at, self._health_issue = 0., ''

    @property
    def application_name(self):
        return 'Neovim'

    def describe(self):
        return f'neovim {self.pinning.describe()}'

    def show_tail(self, text, deadline):
        self.preview.show(text, timeout=min(.1, max(0, deadline - time.monotonic())))

    def show_draft(self, text, *, timeout=.25):
        nvim.call(self.pinning.server, 'draft', {'id': self.pinning.id, 'text': text}, timeout=timeout)

    def availability_issue(self, deadline=None):
        if not self.pin_valid:
            return self.pin_reason
        if time.monotonic() >= self._health_at:
            try:
                timeout = .1 if deadline is None else min(.1, max(0, deadline - time.monotonic()))
                nvim.call(self.pinning.server, 'check', {'id': self.pinning.id}, timeout=timeout)
                self._health_issue = ''
            except nvim.NvimError as exc:
                self._health_issue = str(exc)
            self._health_at = time.monotonic() + .5
        return self._health_issue

    def _insert(self, text, deadline, operation, permit, cancelled, keep_pin):
        if not self.pin_valid:
            return Landing(reason=self.pin_reason)
        if cancelled.is_set() or self.cancelled.is_set() or time.monotonic() >= deadline:
            return Landing(reason='Neovim insertion expired or cancelled')
        try:
            # Lua clears preview and mutates the buffer in the same editor call.
            nvim.call(self.pinning.server, 'insert', {'id': self.pinning.id,
                'text': text, 'operation': operation, 'permit': permit, 'keep_pin': keep_pin},
                timeout=max(0, deadline - time.monotonic()))
            return Landing(Outcome.CONFIRMED)
        except nvim.NvimRefused as exc:
            return Landing(reason=str(exc))
        except nvim.NvimError as exc:
            return Landing(Outcome.UNKNOWN, str(exc))

    def _land(self, text, deadline, operation_id, prefix):
        if not self.preview.lock.acquire(timeout=max(0, deadline - time.monotonic())):
            return Landing(reason='Neovim preview is busy')
        try:
            self.preview.closed = True
        finally:
            self.preview.lock.release()
        return self._insert(text, deadline, operation_id, self.permit, self.cancelled, False)

    def clear(self):
        self.preview.clear()
        self.unpin()

    def insert_pinned(self, text, deadline, operation, prefix, permit, cancelled):
        return self._insert(text, deadline, operation, permit, cancelled, True)

    def unpin(self):
        try:
            nvim.call(self.pinning.server, 'unpin', {'id': self.pinning.id}, timeout=.1)
        except nvim.NvimError:
            pass
