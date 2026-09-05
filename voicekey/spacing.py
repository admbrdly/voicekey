"""Text joining and fallback for applications without cursor reports."""
import threading

NO_SPACE_BEFORE = ",.;:!?)]}"
NO_SPACE_AFTER = " \t\n([{\"'“‘"


def spaced(prefix: str, text: str) -> str:
    if not text or text[0] in NO_SPACE_BEFORE or text[0].isspace():
        return text
    return prefix + text


def owed(before: str | None, fallback: str = "") -> str:
    if before is None:
        return fallback
    return "" if not before or before in NO_SPACE_AFTER else " "


class Spacing:
    def __init__(self):
        self._lock = threading.Lock()
        self._continuing = None
        self._activity = 0

    def prefix(self, window_id) -> str:
        with self._lock:
            return " " if window_id is not None and self._continuing == window_id else ""

    def mark(self) -> int:
        with self._lock:
            return self._activity

    def inserted(self, window_id, text: str, mark: int) -> None:
        with self._lock:
            if mark == self._activity:
                self._continuing = None if text[-1:].isspace() else window_id

    def user_typed(self) -> None:
        with self._lock:
            self._activity += 1
            self._continuing = None
