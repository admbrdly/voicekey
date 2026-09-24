# Neovim destination integration

Voicekey's daemon owns capture, recognition, polish and recovery. Its normal
keys route terminal Neovim to buffer API insertion through `target.bind()`.
Neither `voicekey-route` nor `--capture-to-stdout` is used by that resolver.

## Destination selection

Emacs uses its existing acknowledged buffer adapter. Known terminals use this
order, before the ordinary IME/wtype/clipboard path:

1. Probe registered Neovim instances. A uniquely validated focused instance wins,
   even if the window title also looks like a shell prompt.
2. Refuse ambiguous focused claims or an incomplete/unresponsive probe. A title
   cannot override that uncertainty. Failure to pin a selected Neovim also refuses;
   it never falls through to terminal delivery.
3. Only when the probes complete without a focused claim, consider Ghostty's
   existing Bash prompt title. It must be an absolute or home-relative directory
   matching a local foreground Bash under Ghostty's PID.
4. Without a matching prompt, a Neovim descendant of the terminal causes refusal.
   If there is no such descendant, retain ordinary terminal delivery.

Registrations are private per-instance JSON files under
`$XDG_RUNTIME_DIR/voicekey/nvim/`. Validation checks a responding socket, matching
Neovim PID/server address, ancestry under the terminal PID when available, and a
true plugin focus flag. Multiple instances cannot overwrite each other's records.
The compositor is checked again after editor binding.

Ghostty's Bash exception checks the shell's cwd, process start time, controlling
tty and foreground process group. Suspended shells and descendants sharing the
shell's foreground group are excluded. Delivery rechecks the exact window/title
and original matching shell identities. Persistent capture also polls that
evidence; a detected title or foreground-job change pauses with recovery.
Ordinary title changes are not compositor window identity changes, so changing
an Emacs buffer's title does not pause dictation.

## Capture, preview and insertion

The single-shot path is:

    key → daemon recording → resolver → acknowledged buffer pin
        → finalization/transcription/polish → journalled attempt and permit
        → bounded RPC → Lua validation → buffer API insertion → acknowledgement

Normal dictation keys use persistent tap/hold: a tap leaves capture running;
holding then releasing stops it. The microphone starts before binding. VAD cuts
feed the same ordered processing pipeline, with repeated commits through one
acknowledged editor pin. Right Win, Copilot/F23, toggle and agent-key handling
use the existing controller.

`PinnedEditorTarget` supplies acknowledgement, validity, description, repeated
insertion and release. Emacs keeps protocol v4 and its follow-point behavior.
Neovim uses an advancing extmark: edits elsewhere move it with the buffer, but
moving the cursor or selecting another buffer does not retarget dictation.
Normal-mode pins start after the cursor character; insert-mode pins start at it.
Selections and pending operators are refused when acquiring a pin.

Live and provisional Neovim text appears as inline virtual text at the pin,
not Wayland preedit. A commit clears that preview and advances the mark; the
session renderer then shows any pending tail. Closed, unloaded or unmodifiable
buffers refuse insertion. No failure of a bound Neovim becomes terminal typing.

Known single-shot binding refusals notify as soon as resolution finishes while
capture continues for recovery. Definite refused transcripts are saved and copied
if possible. Clipboard failure still leaves journal/audio recovery. Cancellation
and uncertain insertion never trigger this fallback. Persistent recovery stays
grouped rather than repeatedly replacing the clipboard.

## Transport and deadlines

The daemon sends JSON to the Lua endpoint through
`nvim --server SOCKET --remote-expr`. This adds process startup per request but
requires no Python runtime dependency or custom MessagePack implementation.
Probes and previews have 100 ms bounds, pin acquisition has a 250 ms bound, and
insertion/tail-preview calls use the remaining delivery budget. Late
acknowledgements cannot authorize insertion. Discovery and pinning can block the
single-shot key handler for bounded compositor/RPC waits; microphone capture
continues during those waits.

Lua checks wall-clock expiry and the journal's revocable permission file before
mutation. Operation results are retained through their execution deadline, and
the daemon prevents reuse of a delivery attempt. Sending an old ID with a new
later deadline after the Lua cache expires is not a supported retry protocol.
Timeout or error after submission is uncertain and is never retried automatically.
A successful API response confirms insertion, not that the buffer was saved.

## Persistent focus policies

The plugin starts with focus unconfirmed. `FocusGained` or `VimResume` confirms
it; `FocusLost`, suspension or entry into a Neovim terminal buffer clears it.
If no initial event arrives, switch away and back once after opening Neovim.

Async reports go directly to the daemon's control socket. Socket peer credentials
verify the sender PID; sequence numbers reject repeated/out-of-order reports.
The control server forwards them without waiting for an evdev tick. Terminal-local
changes use the same observed PCM boundaries as compositor focus changes:

- **pause:** stop capture; earlier speech may finish through the original pin.
- **follow:** cut at the observed boundary and rebind through `target.bind()`.
  Old editor pins remain until their earlier speech drains, then are released.
- **pin:** continue into the original buffer in the background.

Editor/socket health checks stop capture and preserve pending speech after a
crash, vanished socket or unavailable buffer, even if an exit report is lost.
Normal completion, stop, cancellation and follow cleanup explicitly unpin.
Abandoned pins are aged out after a day on the next Lua request; a daemon crash
may leave a cosmetic preview until cleanup or editor restart.

## Limits of focus evidence

- Ghostty's shared PID cannot identify a window, tab or pane. A stale true
  Neovim focus flag can select the wrong buffer; two true flags cause refusal.
- A shell-looking title never overrides a validated Neovim claim. However, a
  stale directory title **combined with missing Neovim focus evidence**, such as
  a lost focus event or absent registration, can match an idle shell elsewhere
  and select terminal IME delivery. PID/cwd evidence cannot identify that surface.
- Multiple shells in the same directory are allowed. Title spoofing, a quick
  command and return between checks, and a switch between validation and IME
  submission remain possible. An SSH tab titled `~` can match a local idle shell
  at home; this is not remote prompt detection.
- Prompt inference supports local Bash in Ghostty only. Custom titles or shortened
  `\w` titles (`PROMPT_DIRTRIM`) may not match, retaining refusal when Neovim is
  present. No shell hook or extra configuration is required for ordinary titles.
- Detached tmux, SSH, containers, renamed processes and hidden `/proc` data can
  defeat ancestry. If no Neovim descendant is visible, ordinary terminal delivery
  can remain enabled even when Neovim is visible. `tmux focus-events on` is
  necessary for focus reports but does not establish terminal-to-pane ancestry.
- Without a compositor PID, discovery considers all local Neovims, which can
  refuse unrelated shells or accept the only stale claim. Unknown terminal app
  IDs retain generic routing unless added through `VOICEKEY_TERMINALS`.
- Focus and pin acquisition are asynchronous observations, not atomic key-down
  snapshots. Buffer switches inside Neovim retain the original insertion pin.

## Setup and compatibility

Update the daemon's installed package and restart `voicekey.service` when ready.
Load the Neovim plugin at startup (`lazy = false` rather than command-only lazy
loading), and restart Neovim after changing its Lua files. `init.lua` needs a
change only if startup loading is missing. Emacs needs no reload. Python-only
routing fixes require a daemon restart, not another plugin reload.

`:VoiceKey` still provides single-shot client capture through the same daemon
and shares the Lua insertion helpers. `voicekey-route` remains an optional legacy
binding with weaker last-writer/socket checks; normal daemon keys need neither it
nor F12. Keep the daemon's keyboard listener enabled for tap/hold and agent keys.

## Verification

Run `python -m unittest` using an environment with the project dependencies.
Neovim tests require version 0.10 or newer; private editor/control sockets and a
private Bash PTY require local socket/PTY access. Tests use temporary runtime
folders, synthetic audio, stubbed recognizers and mocked compositor/process
information; they do not contact live editor servers or microphones.

Coverage includes destination precedence, conflicting/unfocused/unresponsive
registrations, pin and buffer failures, expired/duplicate/revoked operations,
key gestures, preview tiers, ordered persistent commits, focus policies, old
speech after a switch, editor exit, refusal feedback and journal/clipboard recovery.
Bash tests cover cwd/title matching and real foreground-job transitions in an
isolated PTY. Existing Emacs, client-capture and legacy-router tests remain in
the full suite.

Real Ghostty/microphone validation is still required before merging:

- Shell and Neovim in separate windows and tabs, including identical directories.
- Starting a command during speech and while transcription is pending.
- Whether newly opened Neovim receives `FocusGained` without switching away/back.
- Physical Right Win and Copilot keys, tap/hold/toggle, preview latency and spacing.
- Pause/follow/pin across Neovim, Emacs and other apps; queued speech before a
  switch and recovery after closing Neovim during processing.
