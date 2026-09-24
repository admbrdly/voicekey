"""Best-effort focused-window tracking through the compositor's IPC.

Implemented for niri, sway and Hyprland (detected by their socket variables
or XDG_CURRENT_DESKTOP). Elsewhere focus is unverifiable: set
``require_same_window = false`` to dictate without the window guard.

The client process ID of the focused window is reported when the compositor
knows it. Emacs uses it to refuse a buffer pin for a window that belongs to
another Emacs process than the server ``emacsclient`` reaches."""

from __future__ import annotations

import json
import logging
import os
import subprocess
from dataclasses import dataclass, field

log = logging.getLogger("voicekey.focus")


@dataclass(frozen=True)
class Focus:
    id: int | str | None = None
    app_id: str | None = None
    pid: int | None = None
    # Titles are routing evidence, not window identity (e.g. Emacs edits its title).
    title: str = field(default="", compare=False, repr=False)


def focused(*, timeout: float = 2.0) -> Focus:
    """The focused window, or an empty Focus when it cannot be queried."""
    name = compositor()
    if name == "niri":
        data = _json(["niri", "msg", "--json", "focused-window"], timeout)
        return _focus(data.get("id"), data.get("app_id"), data.get("pid"), data.get("title")) if isinstance(data, dict) else Focus()
    if name == "sway":
        node = _sway_focused(_json(["swaymsg", "-t", "get_tree"], timeout))
        if node is None:
            return Focus()
        app_id = node.get("app_id") or (node.get("window_properties") or {}).get("class")
        return _focus(node.get("id"), app_id, node.get("pid"), node.get("name"))
    if name == "hyprland":
        data = _json(["hyprctl", "-j", "activewindow"], timeout)
        return _focus(data.get("address"), data.get("class"), data.get("pid"), data.get("title")) if isinstance(data, dict) else Focus()
    return Focus()


def window_id(*, timeout: float = 2.0) -> int | str | None:
    return focused(timeout=timeout).id


def compositor() -> str | None:
    """'niri', 'sway' or 'hyprland' when one is detected, else None."""
    for variable, name in (("NIRI_SOCKET", "niri"), ("SWAYSOCK", "sway"),
                           ("HYPRLAND_INSTANCE_SIGNATURE", "hyprland")):
        if os.environ.get(variable):
            return name
    desktop = os.environ.get("XDG_CURRENT_DESKTOP", "").lower()
    return next((name for name in ("niri", "sway", "hyprland") if name in desktop), None)


def _json(argv: list[str], timeout: float = 2.0):
    if timeout <= 0:
        return None
    try:
        result = subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError:
        log.warning("%s returned invalid JSON", argv[0])
        return None


def _sway_focused(node):
    if not isinstance(node, dict):
        return None
    if node.get("focused") is True and node.get("type") in ("con", "floating_con"):
        return node
    for child in node.get("nodes", []) + node.get("floating_nodes", []):
        found = _sway_focused(child)
        if found is not None:
            return found
    return None


def _focus(window_id, app_id, pid=None, title=None) -> Focus:
    valid_id = (isinstance(window_id, int) and not isinstance(window_id, bool)) or (
        isinstance(window_id, str) and window_id)
    valid_pid = isinstance(pid, int) and not isinstance(pid, bool) and pid > 0
    return Focus(window_id if valid_id else None, app_id if isinstance(app_id, str) else None,
                 pid if valid_pid else None, title if isinstance(title, str) else "")
