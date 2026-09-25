# Claude Code and Codex

`contrib/nvim/voicekey-route` dictates into Claude Code and Codex running in a
terminal, through the daemon's input method, when:

- Claude Code shows `✳ ` (idle) or a rotating half-circle (working) and no
  dialog is open. Its hooks stop dictation before permission prompts,
  questions and plan approval, so a dictated "1" or "yes" cannot answer them.
- Codex is idle. Codex titles show only the project by default; add the app
  name in `~/.codex/config.toml`:

  ```toml
  [tui]
  terminal_title = ["app-name", "spinner", "project"]
  ```

  The title is then `codex | <project>` only when idle; while Codex works it
  is `codex <spinner> <project>`, and at startup plain `codex`. Only the idle
  form is accepted.

Register `voicekey-claude-hook` for these events in `~/.claude/settings.json`
(one entry each, same command; it reads the event from stdin):

```json
"hooks": {
  "UserPromptSubmit":   [{"hooks": [{"type": "command", "command": "/path/to/voicekey/contrib/claude-code/voicekey-claude-hook", "timeout": 15}]}],
  "PermissionRequest":  [{"hooks": [{"type": "command", "command": "/path/to/voicekey/contrib/claude-code/voicekey-claude-hook", "timeout": 15}]}],
  "PreToolUse":         [{"hooks": [{"type": "command", "command": "/path/to/voicekey/contrib/claude-code/voicekey-claude-hook", "timeout": 15}]}],
  "PostToolUse":        [{"hooks": [{"type": "command", "command": "/path/to/voicekey/contrib/claude-code/voicekey-claude-hook"}]}],
  "PostToolUseFailure": [{"hooks": [{"type": "command", "command": "/path/to/voicekey/contrib/claude-code/voicekey-claude-hook"}]}],
  "PermissionDenied":   [{"hooks": [{"type": "command", "command": "/path/to/voicekey/contrib/claude-code/voicekey-claude-hook"}]}],
  "Stop":               [{"hooks": [{"type": "command", "command": "/path/to/voicekey/contrib/claude-code/voicekey-claude-hook"}]}],
  "SessionEnd":         [{"hooks": [{"type": "command", "command": "/path/to/voicekey/contrib/claude-code/voicekey-claude-hook"}]}]
}
```

Submitting a prompt, a permission request, and `AskUserQuestion` or
`ExitPlanMode` stop dictation and wait for pending speech first; late text
becomes a draft in the input box. Dialogs are marked open under
`$XDG_RUNTIME_DIR/voicekey/claude-dialog/` until the tool finishes or fails,
the next tool starts, the turn stops or the session ends; `voicekey-route`
refuses Claude Code while any mark exists. A crashed session can leave a mark
behind: delete the file if dictation keeps being refused. Create
`$XDG_RUNTIME_DIR/voicekey/claude-hook.log` to log each hook call.

The protection depends on these hooks running. If a Claude Code update renames
an event, dictation could reach a dialog; check the log after updates.

Limits:

- Codex has no submit hook: press the key again to stop dictation before you
  submit, or speech may answer an approval prompt (`y`, `a`, `n`).
- With Claude Code's `editorMode` set to `vim`, dictated words act as vim
  commands on the prompt after Esc; stop dictation first.
