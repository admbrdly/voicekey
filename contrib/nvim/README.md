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
- Daemon dictation (the voicekey hotkey, not `:VoiceKey`) behaves as typing:
  from normal mode it enters insert mode as `a` does, so the cursor follows
  the text, and returns to normal mode as `<Esc>` does when the session ends,
  unless you left insert mode yourself.
- Your words appear at that point as you speak, as dimmed virtual text
  (highlight `VoiceKeyPreview`, linked to `Comment`). This is a live draft
  from the daemon's streaming recognizer: it revises itself as you talk, and
  a trailing `…` means recording stopped and the final text is being prepared.
  The buffer is not touched until the final transcript arrives and replaces the
  draft, so there is one undo step and no churn for LSP or autocommands. The
  final text is recognized again from the whole recording and polished, so it
  can differ from the draft. Line breaks in a draft show as `↵`. Daemon
  dictation into a pinned buffer draws its drafts the same way.
- Before the first words (and with `preview = false`) an inline marker shows
  `loading`, `recording`, then `transcribing`. Wait for `recording` before
  speaking: after **Free memory** the daemon reloads its models in the
  background, and recording can begin while they load. A capture that starts
  while models are loading has no live draft, only the final text. The marker
  also switches to `transcribing` after an external stop or the recording
  limit, before the transcript arrives.
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
Restart the daemon after upgrading to enable client labels and live drafts;
older daemons with capture support still work, with a generic status label and
only the final text. This is a display name,
not a change to the daemon's default polish style.

## Normal daemon keys and destination safety

Load `plugin/voicekey.lua` at startup, rather than lazy-loading on `:VoiceKey`.
`vim.v.servername` must name a local socket (Neovim normally creates one; an
explicit `nvim --listen /path/to/socket` also works). The plugin registers each
instance separately under `$XDG_RUNTIME_DIR/voicekey/nvim/<pid>.json`.
The daemon checks that the socket responds, its PID matches the registration,
the process belongs to the focused terminal when the compositor supplies a PID,
and the plugin reports focus. Two focused claims are not trusted.

Focus starts **unconfirmed** until `FocusGained` (or `VimResume`). If the terminal
does not send an initial event, switch away and back once after opening Neovim.
`FocusLost`, suspension, or entering a Neovim terminal buffer clears focus.
The plugin sends these changes directly to the daemon's control socket without
waiting for the keyboard listener's next tick. For tmux, enable:

```tmux
set -g focus-events on
```

Terminal policy (Ghostty, foot, kitty, Alacritty, WezTerm; additional app IDs via
`VOICEKEY_TERMINALS` in the daemon environment):

- A Neovim confirming focus gets API delivery, whatever the window title says.
  A failure to pin or insert never falls back to terminal input.
- No Neovim confirms focus, but the title shows an editor in front (`nvim …`,
  `vim …`, `… - NVIM`): refused, with speech and text kept for recovery.
- Anything else: ordinary input-method delivery, as before. A Neovim open in
  another tab or window never blocks shells or other programs.

A Neovim that is in front without confirming focus and without an editor title
(opened by `git commit` or another program, or after a lost focus event) gets
ordinary typing, and normal mode may run the words as commands. A stale true
focus flag can send dictation to the wrong Neovim buffer. A detached tmux server,
SSH, containers or a renamed process hide Neovim from the ancestry check, so it
is never selected. Remote Neovim is unsupported. Single-shot capture reports a
binding refusal immediately and saves/copies definite refused transcripts;
cancellation and uncertain insertion never copy.

Normal-mode pins start after the cursor character; insert-mode pins start at it.
Selections and pending operators are refused at pin acquisition.

- **pause**: a window switch or reported terminal-local focus loss stops capture;
  already-recorded speech still finishes through the original pin.
- **follow**: a focus event cuts the audio, then the same daemon resolver chooses
  the next destination. Old editor pins remain until earlier speech drains, then
  are released. A refused destination pauses with recovery.
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

In this fork the router also covers other terminal programs: a bash prompt
([contrib/bash](../bash/README.md)), Claude Code and Codex
([contrib/claude-code](../claude-code/README.md)), and it refuses everything else
in a terminal. It is the activation path when the daemon runs with
`evdev = false`, since the daemon's own keys need evdev.

## Options

```lua
require("voicekey").setup({
  -- A client that records until SIGINT, cancels on SIGTERM, prints the
  -- transcript to stdout and "Recording;" / "Transcribing;" / `Preview; "<json>"`
  -- on stderr. The daemon's configuration applies; --config is not accepted.
  cmd = { vim.fn.expand("~/.local/share/voicekey/venv/bin/python"), "-m", "voicekey",
          "--capture-to-stdout", "--client-name", "Neovim", "--preview" },
  preview = true,  -- :VoiceKey live draft while you speak (daemon pins keep theirs)
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
