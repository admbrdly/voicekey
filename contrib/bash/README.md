# Bash

Dictate at bash's prompt with `contrib/nvim/voicekey-route`: the daemon's
input-method text lands in the command line, and nothing runs until you press
Enter.

Add to `~/.bashrc`, after anything that sets `PS0` or `PROMPT_COMMAND`
(starship, bash-preexec):

```bash
. ~/src/voicekey/contrib/bash/voicekey.bash
```

How it works:

- At the prompt the terminal title is `❯ <directory>`, printed from `PS1` so
  terminal shell integrations cannot overwrite it; while a command runs it is
  the command. `voicekey-route` starts dictation in a terminal only when the
  focused window shows the mark, so `less`, `htop` or an SSH session get
  nothing.
- Pressing Enter stops that dictation from `PS0`, before the command runs:
  bash waits (up to about 10 s) for pending speech, then discards input still
  queued for the terminal, so late dictation never reaches the command.

Limit: in vi command mode (after Esc) dictated words act as vi commands on the
line. They cannot run anything before Enter, but stop dictation (press the key
again) before pressing Esc.

`VOICEKEY_PROMPT_MARK` changes the mark; set it for both the shell and the
route script.
