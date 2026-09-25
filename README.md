# VoiceKey

Local voice dictation for people who write in **Emacs or Neovim** on Linux.
VoiceKey is built for Niri and DankMaterialShell: hold-to-talk or hands-free
dictation straight into your editor buffer, an optional draft mode that holds a
whole passage until you accept it, and a native DMS bar widget to see and
control listening. It also dictates into other applications. It is a small,
opinionated Linux tool; Fedora with Niri is the tested setup.

- **Hold the hotkey** to speak and release to stop, or **tap it** to listen until the next press.
- Speech is transcribed locally in batches at pauses, with live previews where supported.
- **Emacs and Neovim:** dictation goes into the buffer you started in, through
  `emacsclient` or the [Neovim plugin](contrib/nvim/README.md) for terminal
  Neovim, and keeps going there while you move around the editor. From Evil or Neovim
  normal mode it switches to insert mode as `a` does, and back when you finish.
- **Draft mode** (experimental): see the whole passage as a preview, then press
  the key again to insert it as one undo step, or Escape to discard it.
- On Niri, new speech follows window switches; pending text retains its original destination.
- Optional language-model cleanup, word corrections, and a separate hotkey for [sending prompts to an agent](docs/reference.md#agent-key-optional).

The agent hotkey supports persistent Hermes sessions or a configurable local
command. Set `agent.target = "command"` and an `agent.command` argument array
to send each transcript to a program through stdin; see the
[agent setup reference](docs/reference.md#agent-key-optional).

## What you need

| Component | Requirement |
| --- | --- |
| Desktop | Linux, a systemd user session, Niri, and PipeWire with a microphone |
| Bar widget | DankMaterialShell; this is a DMS plugin, not a portable system tray app |
| Editor | Emacs with a running server reachable by `emacsclient` (Evil optional), and/or terminal Neovim 0.10+ with the bundled plugin |
| Tools | `uv`, `gcc`, `pw-record`, `wtype`, `wl-copy`, and `notify-send` |
| Keyboard access | Permission to read evdev devices, usually via the `input` group |
| Speech models | English-only defaults, downloaded during installation; CPU inference needs no GPU |

An editor and DMS are optional for basic dictation into other applications. Live
in-field previews require Wayland input-method-v2 and application text-input
support. Sway and Hyprland have focus adapters but are untested; focus-following
currently requires Niri. GNOME and KDE are not supported by this integration.
If you already use an input method such as fcitx, set `[dictation] ime = false`.

## Install

On Fedora, with Niri, DMS, and Emacs already installed:

```sh
sudo dnf install gcc pipewire-utils wl-clipboard libnotify wtype uv
git clone https://github.com/ejerzak/voicekey.git
cd voicekey
./install.sh
sudo usermod -aG input "$USER"
```

Log out and back in after changing group membership. The installer creates a
Python 3.12 environment, downloads models, and enables `voicekey.service` for
your graphical session. Keep the checkout: the installation links to it.
Membership in `input` allows processes running as your user to read raw keyboard events.

Enable Emacs's server with `(server-start)` in your init file, or use an Emacs
daemon. VoiceKey loads its bundled Lisp integration on demand. For Neovim, load
`contrib/nvim` at startup; see the [Neovim plugin](contrib/nvim/README.md).

The default dictation hotkey is **Right Win (Right Super)**, configured as
`KEY_RIGHTMETA`; the agent hotkey defaults to F10. Reserve them in your Niri
configuration so applications do not also act on them:

```kdl
Super_R repeat=false allow-inhibiting=false hotkey-overlay-title="Voice Dictation" { spawn "true"; }
F10 repeat=false allow-inhibiting=false hotkey-overlay-title="Voice Agent" { spawn "true"; }
```

Settings live in `~/.config/voicekey/config.toml`; see the annotated
[example config](config.example.toml). Restart after changes:

```sh
systemctl --user restart voicekey.service
~/.local/share/voicekey/venv/bin/python -m voicekey --check
```

## Add the DMS widget

`./install.sh` links and enables the widget when DMS is installed and running.
Add **Voicekey** in DMS Settings → DankBar → Widgets to put it on your bar.
After upgrading an existing checkout, rerun `./install.sh` to get this setup;
restarting VoiceKey alone does not install the widget. If DMS is not running,
the installer links the widget and prints commands to enable it later.

To install only the widget manually, run from the checkout:

```sh
mkdir -p "${XDG_CONFIG_HOME:-$HOME/.config}/DankMaterialShell/plugins"
ln -s "$PWD/contrib/dms/Voicekey" "${XDG_CONFIG_HOME:-$HOME/.config}/DankMaterialShell/plugins/Voicekey"
dms ipc call plugin-scan scan
# Once scanning finishes:
dms ipc call plugins enable voicekey
```

Add **Voicekey** in DMS Settings → DankBar → Widgets. The widget shows listening
status, microphone warnings, start/stop controls, and three destination choices.
It talks directly to the daemon over a private local socket; both must run as
the same user with the same `XDG_RUNTIME_DIR`. Startup never enables recording.

- **Pause on window switch** (default): switching away stops the microphone.
  Speech already captured for Emacs or Neovim finishes in its buffer; other
  pending text stays in recovery. Tap to start a new session; returning does
  not resume it.
- **Follow focused window** (Niri): new speech follows window switches.
- **Stay at original destination**: retain the starting destination. Emacs and
  Neovim can receive text in their buffer in the background; other destinations
  pause when their original field becomes unavailable.

These choices last until restart. Set `[persistent] destination_policy` to
`"pause"`, `"follow"`, or `"pin"` for a permanent default. Existing explicit
`follow_focus = true/false` settings still load as `"follow"`/`"pin"`.
Listening stops after **60 seconds of silence** by default (`silence_seconds`).

Persistent dictation requires a verified input-method field or an Emacs or Neovim buffer.
If none is detected, it pauses and preserves pending speech for recovery.
For unsupported fields, the panel offers **Use simulated typing this session**:
focus the field first, because these keystrokes can trigger shortcuts elsewhere.
This exception always pauses on window switches and expires with the session.
The bar shows the destination while listening and **Off · reason** after an
automatic stop. Clipboard-only configurations do not offer simulated typing.
Window policies do not distinguish tabs or panes inside one window; loss of
an input-method field activation also pauses dictation.

**Free memory** stops listening, finishes pending speech, and unloads the speech
models and managed cleanup server. Hotkeys stay available: the next dictation
captures immediately while models reload, with a delayed first preview.
**Disable VoiceKey** stops the user service and hotkeys for this login session;
**Enable VoiceKey** starts it without recording. Other apps can still use the mic.

## Dictating in Emacs and Neovim

Dictation lands where your cursor was when you started: after the cursor
character in normal mode, as `a` would, or at the cursor in insert mode. From
Evil or Neovim normal mode, VoiceKey switches to insert mode for the session so
the cursor follows the text, and returns to normal mode when you finish, unless
you changed mode yourself. In Emacs, a visual selection is replaced by the
first utterance; Neovim refuses to start with a selection active. Read-only
buffers are refused, and Emacs terminal buffers (vterm, term) receive the text
as terminal input without any mode change.

By default each utterance is inserted as soon as it is ready, as its own undo
step. **[Draft mode](docs/draft-mode.md)** (experimental) instead keeps the
whole session as a preview until you decide:

- Turn it on with **Draft mode** in the widget while idle, or set
  `[persistent] draft = true` for the default.
- Press the dictation key to start; it is always a toggle in a draft. Your
  words appear as a preview that does not touch the buffer.
- Press the key again to insert the whole draft at once, as one undo step, or
  press Escape to discard it (configurable with `draft_cancel_key`). A
  discarded draft stays recoverable with `--copy-last`.
- Switching windows or a silence timeout pauses the microphone and keeps the
  draft waiting for you. The keys act only in the draft's own window; the
  widget's **Accept draft** and **Discard draft** work from anywhere.
- Cleanup costs the same as ordinary dictation: each utterance is cleaned
  once. Because nothing is inserted yet, a sentence split by a pause can be
  rejoined.
- Where a draft is impossible (browser fields, terminals, read-only or
  terminal buffers), dictation behaves as it would without draft mode.

## Privacy and recovery

Speech recognition runs locally. Cleanup is off by default; configuring a remote
cleanup endpoint or agent sends text to that service. Local cleanup is also available.

VoiceKey keeps recovery audio and transcript history under
`~/.local/state/voicekey/sessions/`. Successful audio is removed after processing;
successful text is normally retained for up to seven days. Unresolved recordings
remain for manual recovery. Logs and recovery files can contain private text or
buffer names; review them before sharing.

If delivery fails or its outcome is uncertain, inspect the destination before retrying:

```sh
~/.local/share/voicekey/venv/bin/python -m voicekey --last
~/.local/share/voicekey/venv/bin/python -m voicekey --copy-last
~/.local/share/voicekey/venv/bin/python -m voicekey --explain-last
```

## More

- [Reference](docs/reference.md): delivery behavior, cleanup, hooks, agent setup, and diagnostics.
- [Draft mode](docs/draft-mode.md) and the [Neovim plugin](contrib/nvim/README.md).
- [Configuration](config.example.toml): keys, models, timeouts, and retention limits.
- [Architecture](docs/persistent-mode-architecture.md) and [benchmarks](benchmarks/README.md).
- Tests: `~/.local/share/voicekey/venv/bin/python -m unittest discover -q`.
  They use isolated targets and private Emacs instances, not the active desktop.

MIT. See [LICENSE](LICENSE).
