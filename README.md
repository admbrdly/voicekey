# VoiceKey

Local voice dictation into Emacs and Neovim on Linux/Wayland.

- **Hold** the hotkey to talk; **tap** it to keep listening until the next press.
- Speech is transcribed locally at each pause, with live previews where supported.
- Pending text stays with its original editor buffer. From Evil or Neovim normal
  mode, dictation enters insert mode and restores normal mode when finished,
  unless you changed mode yourself.
- **Draft mode** (experimental): hold a whole passage as a preview, then insert
  it as one undo step or discard it.
- Also dictates into other apps. Optional local or remote cleanup, word
  corrections, and an [agent hotkey](docs/reference.md#agent-key-optional).

Fedora with Niri is the tested setup.

## Requirements

- Linux with a systemd user session, Niri, PipeWire and a microphone
- For editor integration: Emacs with its server running (Evil optional), and/or
  terminal Neovim 0.10+ with the [plugin](contrib/nvim/README.md)
- DankMaterialShell for the bar widget (optional)
- `uv`, `gcc`, `pw-record`, `wtype`, `wl-copy`, `notify-send`
- Read access to keyboards (the `input` group)

The default speech models are English-only and run on the CPU; the optional
faster-whisper backend supports other languages and CUDA ([configuration](docs/reference.md#configuration)).
Live previews in other apps need Wayland input-method support; if you already
use an input method such as fcitx, set `[dictation] ime = false`. Sway and Hyprland
are untested; GNOME and KDE are unsupported.

## Install

```sh
sudo dnf install gcc pipewire-utils wl-clipboard libnotify wtype uv
git clone https://github.com/ejerzak/voicekey.git
cd voicekey
./install.sh
sudo usermod -aG input "$USER"   # then log out and back in
```

The installer sets up Python, downloads the models, enables `voicekey.service`,
and, if DMS is installed, links its widget: add **Voicekey** in DMS Settings →
DankBar → Widgets. Keep the checkout, and rerun `./install.sh` after upgrading.

- **Emacs:** run a server, with `(server-start)` or a daemon.
- **Neovim:** load `contrib/nvim` at startup ([instructions](contrib/nvim/README.md)).
- **Default hotkeys:** Right Win (`KEY_RIGHTMETA`) for dictation, F10 for the agent.
  Change `dictate_key` / `agent_key` in the config; keys can include modifier chords.
  Reserve your chosen keys in Niri so apps ignore them (defaults below):

```kdl
Super_R repeat=false allow-inhibiting=false hotkey-overlay-title="Voice Dictation" { spawn "true"; }
F10 repeat=false allow-inhibiting=false hotkey-overlay-title="Voice Agent" { spawn "true"; }
```

Settings are in `~/.config/voicekey/config.toml` (see the
[example](config.example.toml)). After changing them:

```sh
systemctl --user restart voicekey.service
~/.local/share/voicekey/venv/bin/python -m voicekey --check
```

## Using it

Hold or tap the hotkey. The widget shows where you are dictating and has
start/stop, **Free memory** (unloads the models) and **Disable VoiceKey**.

Switching windows pauses dictation by default; the widget can instead follow the
focused window or stay with the original one (`[persistent] destination_policy`).
Listening stops after 60 seconds of silence by default (`[persistent] silence_seconds`).
Continuous dictation needs a verified text field or editor buffer; otherwise it
pauses and keeps your speech for recovery. The widget offers simulated typing
as a per-session exception.

### Draft mode

Turn it on in the widget, or set `[persistent] draft = true`.

- Press the hotkey to start. Your words appear as a preview; the buffer is untouched.
- Press it again to insert everything as one undo step, or Escape to discard
  (configurable with `draft_cancel_key`; recoverable with `--copy-last`).
- Switching away stops recording and keeps the draft. Return to accept/discard
  by hotkey, or use the widget's **Accept draft** / **Discard draft**; returning
  does not resume recording.
- Outside editable Emacs and Neovim buffers, dictation works as usual.

Details: [draft mode](docs/draft-mode.md).

## Privacy and recovery

Speech recognition is local. Cleanup is off by default; a remote cleanup
endpoint or agent receives your text. Audio and transcripts are kept under
`~/.local/state/voicekey/sessions/`. Audio is deleted after successful delivery
or draft discard; unresolved recordings remain for recovery. Successful text is
normally retained for up to seven days (`[pipeline] history_days`). These files
can contain private text.

If something does not arrive, check the destination first, then:

```sh
~/.local/share/voicekey/venv/bin/python -m voicekey --last          # show the last dictation
~/.local/share/voicekey/venv/bin/python -m voicekey --copy-last     # copy it
~/.local/share/voicekey/venv/bin/python -m voicekey --explain-last  # what happened to it
```

## More

- [Reference](docs/reference.md) · [Draft mode](docs/draft-mode.md) ·
  [Neovim plugin](contrib/nvim/README.md) · [Example config](config.example.toml) ·
  [Architecture](docs/persistent-mode-architecture.md)
- Tests: `~/.local/share/voicekey/venv/bin/python -m unittest discover -q`
  (isolated targets and private editors; never the active desktop)

MIT. See [LICENSE](LICENSE).
