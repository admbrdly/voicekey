# VoiceKey

## This fork: Neovim and terminals

This fork of [ejerzak/voicekey](https://github.com/ejerzak/voicekey) adds dictation
for **Neovim** and other terminal programs. The daemon's input-method text cannot
see Neovim's mode, so words sent to a terminal running Neovim in normal mode
would run as commands. The additions keep dictation safe there.

- **Neovim plugin** ([contrib/nvim](https://github.com/admbrdly/voicekey/tree/neovim-plugin/contrib/nvim)):
  `:VoiceKey` or `<F12>` records, and the transcript is inserted with
  `nvim_buf_set_text` at the point where you started, whatever the mode. Nothing is
  typed. An inline marker shows loading, recording and transcribing; `:VoiceKey cancel`
  discards. Recording and transcription go through the running daemon, which is the
  only process that loads speech models. Open as
  [PR #1](https://github.com/ejerzak/voicekey/pull/1).
- **One key everywhere** (`contrib/nvim/voicekey-route`): a Niri keybinding that
  inserts into the Neovim that has focus, starts the daemon in GUI apps, and refuses
  other terminal programs, where single letters are commands.
- **Bash prompt, Claude Code and Codex** ([contrib/bash](https://github.com/admbrdly/voicekey/tree/terminal-extras/contrib/bash),
  [contrib/claude-code](https://github.com/admbrdly/voicekey/tree/terminal-extras/contrib/claude-code)):
  dictation into the command line or prompt. It stops when you press Enter or submit,
  and before Claude Code shows a dialog, so a dictated "yes" cannot answer one.
- **No `input` group** ([control-without-keyboard](https://github.com/admbrdly/voicekey/tree/control-without-keyboard)):
  `evdev = false` opens no input devices; a compositor keybinding drives the daemon
  through `--control`, and commands are handled at once.

Quick start for the Neovim plugin (lazy.nvim, with voicekey installed as below):

```lua
{
  dir = "~/src/voicekey/contrib/nvim",
  name = "voicekey",
  lazy = false,
  keys = { { "<F12>", function() require("voicekey").toggle() end, mode = { "n", "i" }, desc = "Dictate" } },
}
```

| Branch | Contents |
| --- | --- |
| [`neovim-plugin`](https://github.com/admbrdly/voicekey/tree/neovim-plugin) | Neovim plugin and `voicekey-route` (PR #1), on upstream's `daemon-client-capture` |
| [`terminal-extras`](https://github.com/admbrdly/voicekey/tree/terminal-extras) | Adds bash, Claude Code and Codex routing |
| [`control-without-keyboard`](https://github.com/admbrdly/voicekey/tree/control-without-keyboard) | Daemon: control commands without evdev, `evdev = false` |
| [`adam-local`](https://github.com/admbrdly/voicekey/tree/adam-local) | All of the above merged; rebuilt, not a stable history |

Status: the Neovim plugin uses upstream's client-capture protocol (branch
`daemon-client-capture`, not yet in upstream `master`). `voicekey --capture-to-stdout`
is a thin client of the running daemon, so the daemon must be running and up to date;
dictations are recoverable with `voicekey --last`. This `master` is otherwise
identical to upstream; the original README follows.

## VoiceKey

Local voice dictation for people who use **Emacs, Niri, and DankMaterialShell**.
If that is your desktop, VoiceKey gives you hold-to-talk or hands-free dictation,
Emacs/Evil-aware insertion, and a native DMS bar widget to see and control listening.
It is a small, opinionated Linux tool; Fedora with Niri is the tested setup.

- **Hold the hotkey** to speak and release to stop, or **tap it** to listen until the next press.
- Speech is transcribed locally in batches at pauses, with live previews where supported.
- Emacs delivery uses a pinned buffer through `emacsclient`, with Evil insertion rules.
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
  Speech already captured for Emacs finishes in its pinned buffer; other
  pending text stays in recovery. Tap to start a new session; returning does
  not resume it.
- **Follow focused window** (Niri): new speech follows window switches.
- **Stay at original destination**: retain the starting destination. Emacs can
  receive text in its pinned buffer in the background; other destinations
  pause when their original field becomes unavailable.

These choices last until restart. Set `[persistent] destination_policy` to
`"pause"`, `"follow"`, or `"pin"` for a permanent default. Existing explicit
`follow_focus = true/false` settings still load as `"follow"`/`"pin"`.
Listening stops after **60 seconds of silence** by default (`silence_seconds`).

Persistent dictation requires a verified input-method field or Emacs buffer.
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
