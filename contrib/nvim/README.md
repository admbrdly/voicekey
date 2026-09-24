# Neovim

Dictate into the current Neovim buffer. The plugin runs
`python -m voicekey --capture-to-stdout` and inserts the transcript with
`nvim_buf_set_text`. Nothing is typed, so the editor mode cannot turn dictated
words into commands, and this path needs no `input` group or input method.
Neovim in a terminal is otherwise the case the daemon's desktop delivery cannot
serve safely: Ghostty and other terminals accept input-method text, so words
committed in normal mode would run as commands.

`--capture-to-stdout` is a thin client of the running daemon. The daemon records
and transcribes with the models it already has loaded (nothing is loaded per
capture), and its own configuration applies: `[backend]`, `[polish]`,
`[text.word_overrides]` and the dictation hook.

Requires Neovim 0.10 and a running, up-to-date voicekey daemon
(`voicekey.service`) with client capture support; restart the service after
upgrading. With no daemon, or an older one, the plugin warns and inserts
nothing.

## Install

With lazy.nvim, from your checkout:

```lua
{
  dir = "~/src/voicekey/contrib/nvim",
  name = "voicekey",
  cmd = "VoiceKey",
  keys = {
    { "<F12>", function() require("voicekey").toggle() end, mode = { "n", "i" }, desc = "Dictate" },
  },
}
```

Without a plugin manager, add the directory to `runtimepath`:

```lua
vim.opt.runtimepath:append(vim.fn.expand("~/src/voicekey/contrib/nvim"))
vim.keymap.set({ "n", "i" }, "<F12>", function() require("voicekey").toggle() end, { desc = "Dictate" })
```

## Use

Press the key to start and again to finish. `:VoiceKey cancel` discards the
recording. `:VoiceKey start`, `stop` and `toggle` are also available.

- The transcript lands where the cursor was when recording started: after
  the cursor character in normal mode, like `a`, and at the cursor in insert
  mode. You can move or edit elsewhere while speaking.
- An inline marker shows `loading`, `recording`, then `transcribing` at that
  point. Wait for `recording` before speaking: after **Free memory** the
  daemon reloads its models first, and recording can begin while they load.
- A space is added next to adjacent words. Paragraph breaks become lines;
  other control characters are removed.
- Recording stops by itself after the daemon's `max_seconds` (90 s by
  default). Stopping dictation elsewhere (the panel, `--control stop`,
  `voicekey-route`, a dictation hotkey) also finishes the capture, and the
  transcript still lands in this buffer. Only this Neovim can cancel it.
- While the daemon is dictating elsewhere or still processing, the capture is
  refused with a warning and nothing is inserted.
- Transcripts go to the daemon's normal journal. If the buffer is closed or
  the text cannot be inserted, recover it manually with
  `python -m voicekey --last` or `--copy-last`, which show the latest prepared
  dictation.

## One keybinding for Neovim and everything else

The daemon's input-method delivery cannot see Neovim's mode, so dictating into
a terminal with it would send words that normal mode runs as commands.
`voicekey-route` lets one Niri keybinding choose the safe path:

> **Warning:** focus tracking relies on the terminal reporting focus changes
> for windows, tabs and splits. Where it does not (tmux without
> `focus-events on`, some tab setups), a Neovim that lost focus can stay
> recorded, and the keybinding would dictate into it while you look at
> something else. Check this before relying on it.

- If the daemon is listening, it stops it, wherever focus is.
- If a terminal is focused, it toggles `:VoiceKey` in the Neovim that has
  focus there. At a bash prompt set up with [contrib/bash](../bash/README.md)
  it records and inserts into the command line. Anything else in a terminal
  is refused with a notification.
- Otherwise, it starts the daemon for input-method dictation.

```kdl
F12 repeat=false hotkey-overlay-title="Dictate" { spawn "/home/you/src/voicekey/contrib/nvim/voicekey-route"; }
```

It needs `jq`, and `plugin/voicekey.lua` loaded at startup (with lazy.nvim,
`lazy = false`): each Neovim records its server address in
`$XDG_RUNTIME_DIR/voicekey/nvim-focus` on `FocusGained` and removes it on
`FocusLost`, using the terminal's focus reporting. Set
`vim.g.voicekey_track_focus = false` to turn that off. Terminal app IDs default
to Ghostty, foot, kitty, Alacritty and WezTerm; override them with
`VOICEKEY_TERMINALS`. The daemon is driven through `--control start` and
`--control stop`; if it is not running, the script says so in a notification.

The daemon's own hotkey (`dictate_key`, Right Super by default) bypasses this
script and still sends input-method text into whatever is focused, terminals
included. Reserve that key for `voicekey-route`, or do not use it while a
terminal running Neovim has focus.

## Options

```lua
require("voicekey").setup({
  -- A client that records until SIGINT, cancels on SIGTERM, prints the
  -- transcript to stdout and "Recording;" on stderr. The daemon's
  -- configuration applies; --config is not accepted.
  cmd = { vim.fn.expand("~/.local/share/voicekey/venv/bin/python"), "-m", "voicekey", "--capture-to-stdout" },
  marker = true,   -- inline status at the insertion point
  spacing = true,  -- separate the transcript from adjacent words
  notify = true,   -- informational messages; warnings always show
})
```

`require("voicekey").status()` returns `"loading"`, `"recording"`,
`"transcribing"` or `nil`, for a statusline component.

## Tests

`nvim --headless -u NONE -i NONE -l tests/nvim-tests.lua`, also run by
`python -m unittest` when Neovim 0.10 or newer is installed. The tests use a
stand-in capture command, not the microphone.
