# Neovim destination integration

Voicekey's daemon owns capture, recognition, polish and recovery. Its normal
keys route terminal Neovim to buffer API insertion through `target.bind()`.
Neither `voicekey-route` nor `--capture-to-stdout` is used by that resolver.

## Destination selection

Emacs uses its existing acknowledged buffer adapter. In a known terminal:

1. Probe the registered Neovim instances under the terminal's PID. Exactly one
   confirming focus wins, whatever the window title says. Failure to pin it
   refuses; it never falls through to terminal typing.
2. Otherwise, if the window title shows an editor in front (Ghostty's command
   title `nvim …`, `vim …`, `vi …`, or Neovim's own `… - NVIM` title), refuse
   rather than type into it.
3. Otherwise use ordinary terminal delivery, exactly as before this integration.

Neovim running elsewhere never blocks typing. Ghostty runs every window and tab
in one process, so its PID cannot say which surface is in front, and treating
any Neovim under it as a reason to refuse would disable dictation into shells
and Claude Code whenever an editor is open somewhere.

Registrations are private per-instance JSON files under
`$XDG_RUNTIME_DIR/voicekey/nvim/`. Validation checks a responding socket, matching
Neovim PID/server address, ancestry under the terminal PID when available, and a
true plugin focus flag. Two claims of focus are not trusted (one must be stale).
The compositor is checked again after editor binding.

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
insertion and release. Emacs protocol v6 retains follow-point behavior for
ordinary sessions and adds an anchored, virtual preview for optional drafts.
Neovim uses an advancing extmark: edits elsewhere move it with the buffer, but
moving the cursor or selecting another buffer does not retarget dictation.
Normal-mode pins start after the cursor character; insert-mode pins start at it.
Selections and pending operators are refused when acquiring a pin.

Live and provisional Neovim text appears as inline virtual text at the pin,
not Wayland preedit. Pins and `:VoiceKey` captures share one renderer: a draft
is spaced like its final insertion and drawn with `VoiceKeyPreview`, and an
empty preview clears the draft and the pin's listening label. A commit clears that preview and advances the mark; the
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

- A Neovim in front that has not confirmed focus gets ordinary terminal typing
  unless its title gives it away. This covers a Neovim started by another
  program (`git commit`, Claude Code's Ctrl-G editor), whose title is the parent
  command, and a lost focus event. Typed words then act as Neovim commands in
  normal mode, as they did before this integration.
- A stale true focus flag can select the wrong Neovim buffer. Ghostty's shared
  PID cannot identify a window, tab or pane.
- Detached tmux, SSH, containers and renamed processes defeat ancestry, so such
  a Neovim is never selected; tmux also needs `focus-events on`.
- Unknown terminal app IDs retain generic routing unless added through
  `VOICEKEY_TERMINALS`.
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
Neovim tests require version 0.10 or newer and local socket access for private
editor and control sockets. Tests use temporary runtime
folders, synthetic audio, stubbed recognizers and mocked compositor/process
information; they do not contact live editor servers or microphones.

Coverage includes destination precedence, conflicting/unfocused/unresponsive
registrations, pin and buffer failures, expired/duplicate/revoked operations,
key gestures, preview tiers, ordered persistent commits, focus policies, old
speech after a switch, editor exit, refusal feedback and journal/clipboard recovery.
Title tests cover editor commands versus prompt directories and TUI titles.
Existing Emacs, client-capture and legacy-router tests remain in
the full suite.

Real Ghostty/microphone validation is still required before merging:

- Shell and Neovim in separate windows and tabs, including identical directories.
- Starting a command during speech and while transcription is pending.
- Whether newly opened Neovim receives `FocusGained` without switching away/back.
- Physical Right Win and Copilot keys, tap/hold/toggle, preview latency and spacing.
- Pause/follow/pin across Neovim, Emacs and other apps; queued speech before a
  switch and recovery after closing Neovim during processing.
