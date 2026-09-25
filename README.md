# VoiceKey

Local voice dictation into Emacs and Neovim, on Linux with Niri and DankMaterialShell.

- **Hold** the hotkey to talk; **tap** it to keep listening until the next press.
- Speech is transcribed locally at each pause, with live previews where supported.
- Text goes into the buffer you started in, even as you move around. From Evil
  or Neovim normal mode, dictation enters insert mode and leaves it when you finish.
- **Draft mode** (experimental): hold a whole passage as a preview, then insert
  it as one undo step or discard it.
- Also dictates into other apps. Optional local cleanup, word corrections, and an
  [agent hotkey](docs/reference.md#agent-key-optional).

Fedora with Niri is the tested setup.

## Requirements

- Linux with a systemd user session, Niri, PipeWire and a microphone
- Emacs with its server running (Evil optional), and/or terminal Neovim 0.10+
  with the [plugin](contrib/nvim/README.md)
- DankMaterialShell for the bar widget (optional)
- `uv`, `gcc`, `pw-record`, `wtype`, `wl-copy`, `notify-send`
- Read access to keyboards (the `input` group)

Speech models are English-only and run on the CPU. Live previews in other apps
need Wayland input-method support; if you already use an input method such as
fcitx, set `[dictation] ime = false`. Sway and Hyprland are untested; GNOME and
KDE are unsupported.

## Install

```sh
sudo dnf install gcc pipewire-utils wl-clipboard libnotify wtype uv
git clone https://github.com/ejerzak/voicekey.git
cd voicekey
./install.sh
sudo usermod -aG input "$USER"   # then log out and back in
```

The installer sets up Python, downloads the models, enables `voicekey.service`,
and links the DMS widget: add **Voicekey** in DMS Settings → DankBar → Widgets.
Keep the checkout, and rerun `./install.sh` after upgrading.

- **Emacs:** run a server, with `(server-start)` or a daemon.
- **Neovim:** load `contrib/nvim` at startup ([instructions](contrib/nvim/README.md)).
- **Hotkeys:** dictation is Right Win (`KEY_RIGHTMETA`), the agent key F10.
  Reserve them in Niri so apps ignore them:

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
Listening stops after 60 seconds of silence. Continuous dictation needs a
verified text field or editor buffer; otherwise it pauses and keeps your speech
for recovery. The widget offers simulated typing as a per-session exception.

### Draft mode

Turn it on in the widget, or set `[persistent] draft = true`.

- Press the hotkey to start. Your words appear as a preview; the buffer is untouched.
- Press it again to insert everything as one undo step, or Escape to discard
  (recoverable with `--copy-last`).
- Switching away pauses the draft until you come back, or use the widget's
  **Accept draft** / **Discard draft**.
- Outside editable Emacs and Neovim buffers, dictation works as usual.

Details: [draft mode](docs/draft-mode.md).

## Privacy and recovery

Speech recognition is local. Cleanup is off by default; a remote cleanup
endpoint or agent receives your text. Audio and transcripts are kept under
`~/.local/state/voicekey/sessions/`: audio until processed, text for seven
days, unresolved recordings until you recover them. They can contain private text.

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
