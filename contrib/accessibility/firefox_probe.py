"""Test a local textarea in an isolated, offline Firefox profile via AT-SPI."""
import json
from pathlib import Path
import subprocess
import tempfile
import time
import uuid

from atspi_probe import Atspi, BASELINE, INSERT, apps, walk, focused_window, focus_window


def run():
    Atspi.set_timeout(300, 500)
    original = focused_window()
    token = 'Voicekey Firefox probe ' + uuid.uuid4().hex[:8]
    result = {'fixture': 'Firefox textarea (isolated profile, accessibility forced on)'}
    with tempfile.TemporaryDirectory(prefix='voicekey-firefox-') as tmp:
        root = Path(tmp)
        profile = root/'profile'
        profile.mkdir()
        prefs = {'accessibility.force_disabled': -1, 'browser.shell.checkDefaultBrowser': False,
                 'browser.startup.homepage_override.mstone': 'ignore', 'browser.aboutwelcome.enabled': False,
                 'browser.startup.page': 0, 'datareporting.policy.dataSubmissionEnabled': False,
                 'toolkit.telemetry.reportingpolicy.firstRun': False}
        (profile/'user.js').write_text('\n'.join(f'user_pref({json.dumps(k)}, {json.dumps(v)});' for k,v in prefs.items()))
        page = root/'fixture.html'
        page.write_text(f'''<!doctype html><meta charset="utf-8"><title>{token}</title>
<h1>Disposable Voicekey accessibility test</h1>
<textarea id="a" aria-label="{token} A" autofocus>{BASELINE}</textarea>
<textarea id="b" aria-label="{token} B">{BASELINE}</textarea>
<script>let count=0; document.querySelector('#a').addEventListener('input',()=>{{document.title='{token} input='+ ++count;}});</script>''')
        proc = subprocess.Popen(['firefox', '--no-remote', '--offline', '--profile', str(profile),
                                 '--new-window', page.as_uri()],
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            deadline = time.monotonic()+25
            fields = {}
            while time.monotonic()<deadline:
                if proc.poll() is not None:
                    raise RuntimeError('Isolated Firefox exited early')
                for app in apps():
                    if app.get_process_id()!=proc.pid:
                        continue
                    for node,_,_,interfaces in walk(app):
                        if 'EditableText' in interfaces and node.get_name() in (token+' A',token+' B'):
                            fields[node.get_name()[-1]]=node
                if len(fields)==2:
                    break
                time.sleep(.2)
            if len(fields)!=2:
                result.update(background_insert=False, reason='Firefox fixture did not expose both editable fields')
                return result
            windows=json.loads(subprocess.check_output(['niri','msg','--json','windows'],timeout=2))
            window=next(w for w in windows if w.get('pid')==proc.pid and token in (w.get('title') or ''))
            focus_window(window['id'])
            a,b=fields['A'],fields['B']
            a.get_component_iface().grab_focus()
            Atspi.Text.set_caret_offset(a,2)
            time.sleep(.15)
            pin,offset=a,Atspi.Text.get_caret_offset(a)
            assert Atspi.Text.get_text(pin,0,-1)==BASELINE
            assert offset==2
            if original is None:
                raise RuntimeError('Need an original Niri window to test background insertion')
            focus_window(original['id'])
            time.sleep(.2)
            assert focused_window()['id']==original['id']
            # Some applications keep their internal focused state when their
            # window is inactive; compositor focus is the background criterion.
            result.update(field_role=pin.get_role_name(), editable_state=pin.get_state_set().contains(Atspi.StateType.EDITABLE), field_interfaces=list(pin.get_interfaces()))
            sent=Atspi.EditableText.insert_text(pin,offset,INSERT,len(INSERT.encode()))
            deadline=time.monotonic()+2
            expected=BASELINE[:offset]+INSERT+BASELINE[offset:]
            while time.monotonic()<deadline:
                actual=Atspi.Text.get_text(pin,0,-1)
                if actual==expected:
                    break
                time.sleep(.05)
            expected=BASELINE[:offset]+INSERT+BASELINE[offset:]
            time.sleep(.2)
            windows=json.loads(subprocess.check_output(['niri','msg','--json','windows'],timeout=2))
            title=next(w['title'] for w in windows if w['id']==window['id'])
            result.update(api_returned=sent, background_insert=bool(sent and actual==expected),
                          other_field_unchanged=Atspi.Text.get_text(b,0,-1)==BASELINE,
                          focus_unchanged=focused_window()['id']==original['id'],
                          unicode_offset_correct=actual==expected,
                          dom_input_event=title==token+' input=1')
            # A foreground positive control distinguishes lack of background
            # support from an exposed interface which does not perform edits.
            focus_window(window['id'])
            b.get_component_iface().grab_focus()
            time.sleep(.15)
            foreground_marker='CONTROL '
            foreground_sent=Atspi.EditableText.insert_text(b,2,foreground_marker,len(foreground_marker))
            time.sleep(.5)
            result.update(foreground_api_returned=foreground_sent,
                          foreground_insert=Atspi.Text.get_text(b,0,-1)==BASELINE[:2]+foreground_marker+BASELINE[2:])
            return result
        finally:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=2)
            if original is not None:
                focus_window(original['id'])


if __name__=='__main__':
    report=run()
    print(json.dumps(report,indent=2))
    raise SystemExit(0 if report.get('background_insert') and report.get('other_field_unchanged')
                     and report.get('focus_unchanged') and report.get('dom_input_event') else 1)
