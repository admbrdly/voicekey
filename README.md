# VoiceKey

Local voice dictation for people who use **Emacs, Niri, and DankMaterialShell**.
If that is your desktop, VoiceKey gives you hold-to-talk or hands-free dictation,
Emacs/Evil-aware insertion, and a native DMS bar widget to see and control listening.
It is a small, opinionated Linux tool; Fedora with Niri is the tested setup.

- **Hold F9** to speak and release to stop, or **tap F9** to listen until the next press.
- Speech is transcribed locally in batches at pauses, with live previews where supported.
- Emacs delivery uses a pinned buffer through `emacsclient`, with Evil insertion rules.
- On Niri, new speech follows window switches; pending text retains its original destination.
- Optional language-model cleanup, word corrections, and an F10 key for Hermes agent prompts.

## What you need

| Component | Requirement |
| --- | --- |
| Desktop | Linux, a systemd user session, Niri, and PipeWire with a microphone |
| Bar widget | DankMaterialShell; this is a DMS plugin, not a portable system tray app |
| Editor | Emacs with a running server reachable by `emacsclient`; Evil is optional |
| Tools | `uv`, `gcc`, `pw-record`, `wtype`, `wl-copy`, and `notify-send` |
| Keyboard access | Permission to read evdev devices, usually via the `input` group |
| Speech models | English-only defaults, downloaded during installation; CPU inference needs no GPU |

Emacs and DMS are optional for basic dictation into other applications. Live
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
daemon. VoiceKey loads its bundled Lisp integration on demand.

Reserve the keys in your Niri configuration so applications do not also act on them:

```kdl
F9  repeat=false allow-inhibiting=false hotkey-overlay-title="Voice Dictation" { spawn "true"; }
F10 repeat=false allow-inhibiting=false hotkey-overlay-title="Voice Agent" { spawn "true"; }
```

Settings live in `~/.config/voicekey/config.toml`; see the annotated
[example config](config.example.toml). Restart after changes:

```sh
systemctl --user restart voicekey.service
~/.local/share/voicekey/venv/bin/python -m voicekey --check
```

## Add the DMS widget

From the checkout:

```sh
mkdir -p ~/.config/DankMaterialShell/plugins
ln -s "$PWD/contrib/dms/Voicekey" ~/.config/DankMaterialShell/plugins/Voicekey
dms ipc call plugin-scan scan
# Once scanning finishes:
dms ipc call plugins enable voicekey
```

Add **Voicekey** in DMS Settings → DankBar → Widgets. The widget shows listening
status, microphone warnings, start/stop controls, and a follow-focus toggle.
It talks directly to the daemon over a private local socket; both must run as
the same user with the same `XDG_RUNTIME_DIR`. Startup never enables recording.

The toggle changes policy until restart. Set `[persistent] follow_focus = false`
to keep dictation pinned to its starting destination by default. Following is
by window, not by tabs, terminal panes, or buffers within the same Emacs window.

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
- [Configuration](config.example.toml): keys, models, timeouts, and retention limits.
- [Architecture](docs/persistent-mode-architecture.md) and [benchmarks](benchmarks/README.md).
- Tests: `~/.local/share/voicekey/venv/bin/python -m unittest discover -q`.
  They use isolated targets and private Emacs instances, not the active desktop.

MIT. See [LICENSE](LICENSE).
