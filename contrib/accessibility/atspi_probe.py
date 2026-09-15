#!/usr/bin/env python3
"""Probe AT-SPI support without reading user text; test only owned fixtures.

Run with the system Python (PyGObject + the Atspi typelib), not Voicekey's venv.
The self-test launches two disposable GTK windows and restores Niri focus.
"""
import argparse
from collections import Counter, deque
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import uuid

import gi
gi.require_version('Atspi', '2.0')
from gi.repository import Atspi, GLib

BASELINE = 'α original field'
INSERT = 'Voicekey ✓ '


def walk(root, limit=2000, seconds=10):
    pending = deque([(root, ())])
    deadline = time.monotonic() + seconds
    count = 0
    while pending and count < limit and time.monotonic() < deadline:
        node, path = pending.popleft()
        count += 1
        try:
            interfaces = list(node.get_interfaces())
            role = node.get_role_name()
            yield node, path, role, interfaces
            children = node.get_child_count()
            if len(path) < 50:
                for i in range(min(children, max(0, limit-count-len(pending)))):
                    if time.monotonic() >= deadline:
                        break
                    child = node.get_child_at_index(i)
                    if child is not None:
                        pending.append((child, path + (i,)))
        except GLib.Error:
            continue


def apps():
    desktop = Atspi.get_desktop(0)
    return [desktop.get_child_at_index(i) for i in range(desktop.get_child_count())]


def inspect(pid=None):
    reports = []
    for app in apps():
        app_pid = app.get_process_id()
        if pid is not None and app_pid != pid:
            continue
        roles, fields, visited = Counter(), [], 0
        for node, path, role, interfaces in walk(app):
            roles[role] += 1
            visited += 1
            if 'Text' in interfaces or 'EditableText' in interfaces:
                fields.append({'path': list(path), 'role': role, 'interfaces': interfaces,
                               'focused': node.get_state_set().contains(Atspi.StateType.FOCUSED),
                               'editable': node.get_state_set().contains(Atspi.StateType.EDITABLE)})
        reports.append({'application': app.get_name(), 'pid': app_pid,
                        'visited': visited, 'scan_limits': {'nodes': 2000, 'seconds': 10, 'depth': 50},
                        'roles': dict(roles), 'text_fields': fields})
    return reports


def focused_window():
    result = subprocess.run(['niri', 'msg', '--json', 'focused-window'],
                            capture_output=True, text=True, check=True, timeout=2)
    return json.loads(result.stdout)


def focus_window(identity):
    subprocess.run(['niri', 'msg', 'action', 'focus-window', '--id', str(identity)],
                   check=True, capture_output=True, timeout=2)


def self_test():
    original = focused_window()
    token = 'Voicekey accessibility probe ' + uuid.uuid4().hex[:8]
    result = {'fixture': 'GTK 3 Entry', 'background_insert': False}
    with tempfile.TemporaryDirectory(prefix='voicekey-atspi-') as tmp:
        fixture = Path(__file__).with_name('atspi_fixture.py')
        proc = subprocess.Popen([sys.executable, str(fixture), token],
                                stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        try:
            deadline = time.monotonic() + 10
            fields, windows = {}, {}
            while time.monotonic() < deadline:
                if proc.poll() is not None:
                    raise RuntimeError(proc.stderr.read().decode()[-1000:])
                fields = {}
                for app in apps():
                    if app.get_process_id() != proc.pid:
                        continue
                    for node, _, _, interfaces in walk(app):
                        if 'EditableText' in interfaces and node.get_name() in (token+' A', token+' B'):
                            fields[node.get_name()[-1]] = node
                output = subprocess.check_output(['niri', 'msg', '--json', 'windows'], timeout=2)
                windows = {w['title'][-1]: w['id'] for w in json.loads(output)
                           if w.get('pid') == proc.pid and w.get('title') in (token+' A', token+' B')}
                if len(fields) == len(windows) == 2:
                    break
                time.sleep(.1)
            if len(fields) != 2 or len(windows) != 2:
                raise RuntimeError('Disposable GTK fields were not exposed through AT-SPI')
            a, b = fields['A'], fields['B']
            focus_window(windows['A'])
            a.get_component_iface().grab_focus()
            Atspi.Text.set_caret_offset(a, 2)
            time.sleep(.15)
            # Save the actual accessible object and character offset while focused.
            pin, offset = a, Atspi.Text.get_caret_offset(a)
            assert Atspi.Text.get_text(a, 0, -1) == BASELINE
            assert offset == 2
            focus_window(windows['B'])
            b.get_component_iface().grab_focus()
            time.sleep(.15)
            before = focused_window()['id']
            assert before == windows['B']
            assert not pin.get_state_set().contains(Atspi.StateType.FOCUSED)
            sent = Atspi.EditableText.insert_text(pin, offset, INSERT, len(INSERT.encode()))
            actual = Atspi.Text.get_text(pin, 0, -1)
            expected = BASELINE[:offset] + INSERT + BASELINE[offset:]
            result.update(background_insert=bool(sent and actual == expected),
                          other_field_unchanged=Atspi.Text.get_text(b, 0, -1) == BASELINE,
                          focus_unchanged=focused_window()['id'] == before,
                          unicode_offset_correct=actual == expected)
            if not all(result[k] for k in ('background_insert', 'other_field_unchanged', 'focus_unchanged', 'unicode_offset_correct')):
                raise RuntimeError('Background insertion did not meet fixture assertions: '+json.dumps(result))
        finally:
            proc.terminate()
            try:
                proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=1)
            proc.stderr.close()
            if original is not None:
                focus_window(original['id'])
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--pid', type=int, help='inspect only this application process')
    parser.add_argument('--self-test', action='store_true', help='test background insertion in two disposable GTK windows')
    args = parser.parse_args()
    if args.self_test and args.pid is not None:
        parser.error('--self-test cannot be combined with --pid')
    Atspi.set_timeout(300, 500)
    print(json.dumps(self_test() if args.self_test else inspect(args.pid), indent=2))


if __name__ == '__main__':
    main()
