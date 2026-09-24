# Neovim

Dictate into the current Neovim buffer. The plugin runs
`python -m voicekey --capture-to-stdout` and inserts the transcript with
`nvim_buf_set_text`. Nothing is typed, so the editor mode cannot turn dictated
words into commands, and this path needs no daemon, `input` group, or input
method. Neovim in a terminal is otherwise the case the daemon cannot serve
safely: Ghostty and other terminals accept input-method text, so words
committed in normal mode would run as commands.

Requires Neovim 0.10 and an installed voicekey (`./install.sh`, or at least the
venv and `python -m voicekey --download`). The daemon does not need to run.

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
  point. The model loads on each capture, so wait for `recording`.
- A space is added next to adjacent words. Paragraph breaks become lines;
  other control characters are removed.
- Recording stops by itself after `max_seconds` (90 s by default).
- Every transcript is journalled under `~/.local/state/voicekey/stdout/`
  (`--last` covers daemon dictation only). If the buffer is closed or the
  text cannot be inserted, recover it from the newest `.txt` there.

`[backend]`, `[polish]` and `[text.word_overrides]` from
`~/.config/voicekey/config.toml` apply as usual.

## Options

```lua
require("voicekey").setup({
  -- Any command that records until SIGINT and prints the transcript;
  -- SIGTERM must discard. Add "--config", "/path/to/config.toml" here if needed.
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
