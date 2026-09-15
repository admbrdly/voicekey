# AT-SPI background-insertion probe

This is a standalone experiment, not an enabled Voicekey delivery backend.
It answers whether an application exposes a particular editable field and
whether a saved field object can accept text after its window loses focus.

Use the **system Python**, with PyGObject and the Atspi typelib installed.
The GTK self-test also needs the GTK 3 typelib. On this Fedora installation
these dependencies were already available. No Python dependencies were added
to Voicekey.

## Read-only inspection

```bash
python3 contrib/accessibility/atspi_probe.py
python3 contrib/accessibility/atspi_probe.py --pid PROCESS_ID
```

The report lists application names/PIDs, roles, interfaces, and focused/editable
states. It does not read textbox contents, textbox names, or window titles.
Traversal is bounded to 2,000 visited objects, depth 50, and about 10 seconds
per application, with individual AT-SPI call timeouts. Large or unresponsive
trees may therefore produce incomplete results. An application absent from
the bus cannot be classified from this probe alone.

## Disposable insertion tests

Run while dictation is off. These tests temporarily change desktop focus.
They close their own windows/processes and restore the original Niri window.

```bash
python3 contrib/accessibility/atspi_probe.py --self-test
python3 contrib/accessibility/firefox_probe.py
```

The GTK test creates two named Entry widgets in a dedicated process. It captures
the first object's character offset while focused, focuses the second window,
and calls `EditableText.insert_text` on the saved first object. It checks the
resulting text, Unicode offset, untouched second field, and compositor focus.

The Firefox test creates an isolated temporary profile, enables accessibility
only in that profile, starts Firefox offline, and opens a local HTML fixture.
It discovers only that process's uniquely named textareas. It tests background
insertion and waits up to two seconds for text read-back, checks a DOM `input`
event via the fixture title, and runs a separate ASCII foreground control.
The existing Firefox profile and user pages are not modified. The test exits
nonzero when the background-insertion contract is not met; this is an expected
experimental result, not a failure of the production Voicekey test suite.

## Results, 2026-09-15

| Target | Editable field exposed? | Insertion result |
| --- | --- | --- |
| Running Ghostty 1.3.1, GTK 4.22.5 | No Text or EditableText interfaces in its 35-object tree | Cannot attempt targeted insertion |
| Disposable GTK 3.24.52 Entry | Yes | Background insertion passed; other field and focus unchanged; Unicode offset correct |
| Running Firefox instance at initial inspection | Not present on AT-SPI bus | Not tested or classified |
| Isolated Firefox 155.0 textarea, accessibility enabled | Yes, entry role and EDITABLE state | API returned true, but text remained unchanged and no DOM input event appeared; foreground ASCII control also made no edit |

Firefox was retried with delayed read-back. Early runs retained compositor
focus; the final recorded run did not. The probe does not establish whether
that focus change came from Firefox or concurrent desktop activity. The
repeated no-edit result, including the foreground control, already rules out
treating its true return value as a delivery acknowledgement on this setup.

Results with host and process identifiers omitted are in `results-2026-09-15.json`. Environment:
`at-spi2-core 2.60.6`, `python3-gobject 3.56.3`, GTK 3.24.52 / GTK 4.22.5.

## Consequences for Voicekey

AT-SPI can target an unfocused widget on this desktop, but that capability
is application-dependent. It does not currently solve background delivery to
the installed Ghostty. Interface presence and a successful method return are
also insufficient evidence of insertion in the tested Firefox build.

No fallback was wired into dictation. A production integration would still
need to validate editable/password/read-only state, handle object destruction
and selection, reject stale insertion offsets after edits, verify application
identity, and distinguish confirmed edits from ambiguous calls. In particular,
a character offset is not a moving insertion marker. This experiment covers
static disposable fields only, not arbitrary sites, rich editors, or terminal
programs.

## Protocol references

- [AT-SPI insert_text](https://gnome.pages.gitlab.gnome.org/at-spi2-core/libatspi/method.EditableText.insert_text.html): character offset and UTF-8 length contract.
- [AT-SPI get_editable_text_iface](https://gnome.pages.gitlab.gnome.org/at-spi2-core/libatspi/method.Accessible.get_editable_text_iface.html): interface availability.
- [Firefox ATK insertion adapter](https://github.com/mozilla-firefox/firefox/blob/main/accessible/atk/nsMaiInterfaceEditableText.cpp): upstream implementation, not proof that the local edit completed.
