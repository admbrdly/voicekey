# Claude Code and Codex

`contrib/nvim/voicekey-route` dictates into Claude Code and Codex running in a
terminal, through the daemon's input method, when:

- Claude Code is idle: its title starts with `✳ `. While it works (spinner
  titles) dictation is refused, because permission dialogs appear then and
  a dictated "1" or "yes" could answer them.
- Codex has focus: it sets no title, so bash's `codex …` command title from
  [contrib/bash](../bash/README.md) identifies it.

Submitting a Claude Code prompt stops that dictation first. Add the hook to
`~/.claude/settings.json`:

```json
"hooks": {
  "UserPromptSubmit": [
    {"hooks": [{"type": "command", "command": "/path/to/voicekey/contrib/claude-code/voicekey-submit-hook"}]}
  ]
}
```

It stops the daemon and waits for pending speech before Claude sees the
prompt; late text becomes a draft in the empty input box.

Limits:

- Codex has no submit hook: press the key again to stop dictation before you
  submit, or speech may answer an approval prompt (`y`, `a`, `n`).
- With Claude Code's `editorMode` set to `vim`, dictated words act as vim
  commands on the prompt after Esc; stop dictation first.
