# Neovim

The daemon's normal dictation keys (including Right Win and Copilot/F23) now
resolve focused terminal Neovim to its buffer API, in single-shot and persistent
mode. No compositor binding or F12 is needed. The daemon still owns the
microphone, recognizers, polish and journal. The widget says **Neovim**.

Requires Neovim 0.10+, the plugin loaded at startup, a local Neovim server socket,
and an updated daemon. Live and provisional text appears as inline virtual text
at the insertion pin, never as Wayland preedit in the terminal.

`:VoiceKey` remains an independent single-shot client of the same daemon using
`--capture-to-stdout`. It does not create another microphone/model owner.

## Install

With lazy.nvim, from your checkout:

```lua
{
  dir = "~/src/voicekey/contrib/nvim",
  name = "voicekey",
  lazy = false, -- registration and focus reporting must run before the key press
}
```

Without a plugin manager, add the directory to `runtimepath`:

```lua
vim.opt.runtimepath:append(vim.fn.expand("~/src/voicekey/contrib/nvim"))
```

## Optional single-shot client

Run `:VoiceKey` to start and again to finish. `:VoiceKey cancel` discards the
recording. `:VoiceKey start`, `stop` and `toggle` are also available.

- The transcript lands where the cursor was when recording started: after
  the cursor character in normal mode, like `a`, and at the cursor in insert
  mode. You can move or edit elsewhere while speaking.
- An inline marker shows `loading`, `recording`, then `transcribing` at that
  point. Wait for `recording` before speaking: after **Free memory** the
  daemon reloads its models in the background, and recording can begin while
  they load. The marker also switches to `transcribing` after an external stop
  or the recording limit, before the transcript arrives.
- A space is added next to adjacent words. Paragraph breaks become lines;
  other control characters are removed.
- Recording stops by itself after the daemon's `max_seconds` (90 s by
  default). Stopping dictation elsewhere (the panel, `--control stop`,
  `voicekey-route`, a dictation hotkey) also finishes the capture, and the
  transcript still lands in this buffer. Only this Neovim can cancel it.
- While the daemon is dictating elsewhere or still processing, the capture is
  refused with a warning and nothing is inserted.
- Transcripts go to the daemon's normal journal. If the buffer is closed or
  the text cannot be inserted, recover the latest prepared dictation manually:

```sh
~/.local/share/voicekey/venv/bin/python -m voicekey --last
~/.local/share/voicekey/venv/bin/python -m voicekey --copy-last
```

The default command labels its destination **Neovim** in the DMS widget.
Restart the daemon after upgrading to enable client labels; older daemons with
capture support still work, with a generic status label. This is a display name,
not a change to the daemon's default polish style.

## Normal daemon keys and destination safety

Load `plugin/voicekey.lua` at startup, rather than lazy-loading on `:VoiceKey`.
`vim.v.servername` must name a local socket (Neovim normally creates one; an
explicit `nvim --listen /path/to/socket` also works). The plugin registers each
instance separately under `$XDG_RUNTIME_DIR/voicekey/nvim/<pid>.json`.
The daemon checks that the socket responds, its PID matches the registration,
the process belongs to the focused terminal when the compositor supplies a PID,
and the plugin reports focus. Multiple focused claims are refused.

Focus starts **unconfirmed** until `FocusGained` (or `VimResume`). If the terminal
does not send an initial event, switch away and back once after opening Neovim.
`FocusLost`, suspension, or entering a Neovim terminal buffer clears focus.
The plugin sends these changes directly to the daemon's control socket without
waiting for the keyboard listener's next tick. For tmux, enable:

```tmux
set -g focus-events on
```

Check focus reporting with your actual terminal tabs and tmux panes. A lost
`FocusLost` can leave one stale true flag and send dictation to the wrong Neovim
buffer. Ghostty uses one PID across windows/tabs; ancestry cannot distinguish
them. A detached tmux server, SSH, containers, hidden process information or a
renamed process can also defeat the ancestry check. Remote Neovim is unsupported.

Terminal policy (Ghostty, foot, kitty, Alacritty, WezTerm; additional app IDs via
`VOICEKEY_TERMINALS` in the daemon environment):

- A uniquely validated Neovim gets API delivery, even when the title also matches
  a shell. Ambiguous claims or incomplete/unresponsive probes refuse; titles
  cannot override them.
- No focused claim after completed probes, but a Neovim descendant exists: refuse
  and retain speech/text for recovery. A shell tab beside Neovim in the same
  Ghostty process is therefore also refused unless its title matches a local
  foreground Bash prompt (see below).
- No Neovim descendant: retain ordinary input-method delivery (or the existing
  configured fallback). This is not proof that a shell or another terminal
  application is safe for typing.

For Ghostty with its existing Bash title integration, an absolute directory title
or `~`/`~/...` can identify a shell prompt. The daemon matches it against the
working directory of a local foreground Bash under Ghostty, excluding running
foreground jobs. This check runs only after completed Neovim probes find no
focused claim. That permits shell dictation while Neovim is open elsewhere,
without a shell hook or config change. Before delivery it checks the window,
title and shell processes again; persistent capture polls for changes.

**This deliberately accepts stale-title risk when Neovim focus evidence is
missing.** A stale directory title, combined with a lost Neovim focus event or
absent registration, can match a shell in another window. The daemon cannot
prove which surface owns that title and could choose terminal IME delivery.
A validated focused Neovim always takes precedence.
Multiple shells with the same directory are allowed. A quick command and return
to the same title between observations can go undetected. Custom, shortened or
remote titles may not match; other terminals retain the conservative policy.

If the compositor supplies no PID, discovery considers all local Neovims and
therefore refuses more often. There is no narrower reliable tab/pane identity in
this integration. A socket failure, dead/unloaded/unmodifiable buffer or pin
failure never falls back to terminal input. Single-shot capture reports a known
binding refusal immediately and saves/copies definite refused transcripts when
possible; cancellation and uncertain insertion never trigger that copy fallback.
Persistent recovery remains grouped rather than overwriting the clipboard.
Timeouts after submission are
uncertain: inspect the buffer and journal before manually recovering text.

## Persistent sessions

The daemon's tap-to-listen and hold-to-talk behavior is unchanged. A session pins
one buffer position and inserts each utterance in order, advancing the extmark.
Edits elsewhere move the mark with the buffer. Moving the cursor does **not**
retarget it: this deliberately differs from Emacs's follow-point behavior.
Normal-mode pins start after the cursor character; insert-mode pins start at it.
Selections and pending operators are refused at pin acquisition.

- **pause**: a window switch or reported terminal-local focus loss stops capture;
  already-recorded speech still finishes through the original pin.
- **follow**: a focus event cuts the audio, then the same daemon resolver chooses
  the next destination. Old editor pins remain until earlier speech drains, then
  are released. An unverified shell/terminal destination pauses with recovery.
- **pin**: capture and insertion may continue into the original buffer in the
  background. Editor/socket loss stops capture and preserves undelivered speech.

The plugin's events cover terminal tabs/panes only when the terminal forwards
those events. Moving between ordinary buffers inside Neovim keeps the original
pin. Focus boundaries are observed events, not exact physical input timestamps.
Exiting Neovim removes its registration; periodic pin health checks also catch a
crash or vanished socket even if the exit notification cannot be sent.

## Upgrade and legacy routing

Update the installed Python package if it is not an editable checkout, then
restart `voicekey.service` yourself. Change a lazy plugin declaration to
`lazy = false` in `init.lua`, then restart Neovim to reload both Lua files and
install focus reporting. No Emacs reload, new keybinding or model change is
needed. If already loading the plugin at startup, no `init.lua` change is needed.

`voicekey-route` is retained for compatibility with existing explicit Niri
bindings. It still toggles the single-shot `:VoiceKey` client and uses the older
`nvim-focus` file; it lacks the daemon resolver's ancestry and ambiguity checks.
It is not required or called by the daemon. Remove an old routing binding if you
want to use only the normal daemon keys; do not bind the same key to both paths.

## Options

```lua
require("voicekey").setup({
  -- A client that records until SIGINT, cancels on SIGTERM, prints the
  -- transcript to stdout and "Recording;" / "Transcribing;" on stderr. The daemon's
  -- configuration applies; --config is not accepted.
  cmd = { vim.fn.expand("~/.local/share/voicekey/venv/bin/python"), "-m", "voicekey", "--capture-to-stdout", "--client-name", "Neovim" },
  marker = true,   -- inline status at the insertion point
  spacing = true,  -- separate the transcript from adjacent words
  notify = true,   -- informational messages; warnings always show
})
```

`require("voicekey").status()` returns `"loading"`, `"recording"`,
`"transcribing"` or `nil`, for a statusline component.

## Tests

`python -m unittest` runs the existing client-capture Lua tests and the isolated
RPC/resolver/key/persistent tests in `tests/test_nvim_target.py` when Neovim 0.10+
is installed. Tests use temporary sockets/runtime directories, fake compositor
and process trees, synthetic PCM and stubbed recognizers. No live editor or
microphone is used. See the [current design](../../docs/neovim-integration.md)
for routing precedence, limitations and the remaining real-microphone checks.
