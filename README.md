# voicekey

Hold-to-talk dictation for Wayland desktops. Hold a key and speak: the words
appear as you say them, inline in whatever you are typing into. Release the
key, and a second pass over the whole recording replaces them with the final
transcript. Speech recognition runs locally. Polish is off by default; configuring
a remote polish endpoint or agent explicitly sends text to that endpoint.

- **Live, in place.** The provisional text is drawn by the application
  itself, through the Wayland input-method protocol that CJK input uses,
  and swapped for the final text in one step. No overlay window, nothing
  typed and then deleted.
- **Accurate final text.** Streaming recognizers trade accuracy for
  latency, so the preview comes from a streaming model and the text that
  lands comes from an offline model with full context and proper
  punctuation.
- **Bound delivery.** An input-method commit uses the original field's
  activation. If that activation ends, the transcript is saved and copied;
  it is never automatically rebound to another field in the same window.
  Emacs uses an acknowledged buffer pin. Applications without input-method
  support use a window check before `wtype`, which cannot pin an individual
  field or prevent focus from changing during typing.
- **Optional polish pass.** A small language model can clean the transcript
  before it lands: fillers, stutters and self-corrections go, punctuation
  and numbers can be written out. Quick dictations below eight words skip polish
  for latency. The raw transcript is retained, and is used whenever the model
  is late or its output fails the checks. These checks are heuristics, not a
  guarantee that meaning is preserved.
- **Optional agent key.** A second key sends the transcript to a persistent
  [Hermes](https://hermes-agent.nousresearch.com) agent session instead.

## Requirements

- Linux with systemd and PipeWire, on a Wayland compositor that implements
  `zwp_input_method_v2`: niri, sway, Hyprland, river, labwc, Wayfire.
  The focused-window guard talks to niri, sway and Hyprland; on the others
  set `require_same_window = false` (the field-level guard still applies).
  Only those three also tell voicekey which application is focused, which
  the Emacs delivery below depends on: elsewhere Emacs gets the generic
  commit, so dictate into it in insert state only.
  GNOME and KDE implement neither this nor the virtual-keyboard protocol;
  there voicekey could only copy to the clipboard, so it is not supported.
- `uv` (installs Python 3.12 into a private venv), `gcc` (the evdev
  package builds from source), and `pw-record`, `wtype`, `wl-copy`,
  `notify-send`.
- About 1.1 GB of disk for the two models and 1.7 GB of RAM while running.
  Idle cost is nil. While you speak, the live recognizer keeps roughly one
  core busy (measured 0.75 core on a desktop i5); the final pass then
  takes about 5 % of the recording's length on four cores.
- English speech. Both models are English-only.

Fedora: `sudo dnf install gcc pipewire-utils wl-clipboard libnotify wtype uv`

## Install

```sh
git clone https://github.com/ejerzak/voicekey.git && cd voicekey
./install.sh                     # venv, models, user service, config file
sudo usermod -aG input "$USER"   # raw keyboard access; log out and back in
```

The compositor must not act on the dictation keys itself. Under niri, add
no-op binds (with Dank Material Shell they live in
`~/.config/niri/dms/binds.kdl`):

```kdl
F9  repeat=false allow-inhibiting=false hotkey-overlay-title="Voice Dictation" { spawn "true"; }
F10 repeat=false allow-inhibiting=false hotkey-overlay-title="Voice Agent" { spawn "true"; }
```

The daemon starts with your next graphical session as `voicekey.service`.
Hold F9, talk, release. Settings live in `~/.config/voicekey/config.toml`,
created from [`config.example.toml`](config.example.toml); every key is
documented there. To check the setup:

```sh
~/.local/share/voicekey/venv/bin/python -m voicekey --check
```

## How it works

```
key down  ─ pw-record ─ 100 ms frames ─┬─ streaming model ─ live text ─ preedit in the focused field
                                       │                               (or a notification)
                                       └─ buffer
key up    ─ buffer ─ offline model ─ raw text ─ [polish model ─ clean text] ─ commit in place of the preedit
                                                 (optional; raw text shown      (or wtype / clipboard)
                                                  as preedit meanwhile)
```

Keys are read directly from evdev, so press and release work even though
Wayland has no global hotkeys. Audio streams from `pw-record` into a
cache-aware streaming transducer (NVIDIA Nemotron Speech Streaming 0.6B, via
sherpa-onnx) whose output only ever grows; it is shown as *preedit*
— provisional text the application renders inline but never inserts. On
release, the whole recording goes to an offline model (NVIDIA Parakeet
Unified 0.6B, via sherpa-onnx; about a point of word error rate better and
much better punctuation), and its text is committed in place of the
preedit.

voicekey registers with the compositor as *the* input method. Applications
that speak `text-input-v3` (GTK, Qt, Firefox, Chromium and Electron,
Emacs pgtk, foot, Ghostty, kitty, Alacritty, …) get the in-field
experience; anything else gets the preview in a notification and the final
text through `wtype`. Because there is one input method per seat, voicekey
cannot coexist with an IME such as fcitx — set `ime = false` to keep one.

Emacs is a special case: committed keystrokes in Evil normal or visual
state can become commands. Voicekey instead requests a buffer pin through
`emacsclient`. Emacs must acknowledge it within 250 ms; a late or failed pin
cannot authorize insertion. The pin identifies the buffer when Emacs handles
that request, rather than claiming an atomic snapshot at physical key-down.
Once pinned, delivery follows point within that buffer and ignores compositor
focus. Insert state inserts at point; normal state appends after the cursor;
visual state replaces the selection; terminal buffers receive process input.
Normal and visual state finish in normal state. Spacing is computed at the
actual insertion position inside the editor transaction.

The packaged `voicekey/voicekey.el` provides those transactions. It is loaded
on demand and installs no hooks by default. Emacs uses the same Wayland
preedit preview as other supported applications, with notifications as the
fallback when an input-method activation is unavailable. The preview clear
request is flushed before the separate editor insertion is submitted, within
the delivery deadline. This orders the requests locally; the two channels
do not provide an atomic application-level transaction. Losing preview focus
does not invalidate an acknowledged buffer pin for final insertion.

Shared Wayland previews are also the default for persistent mode.
Start with dictation at one position and let pending text finish before moving
point; Emacs overlays and per-utterance insertion markers can be revisited if
actual use needs them. To bind the last buffer used through Emacs's command
loop, even when an agent changes the selected buffer, load the library in your
Emacs configuration and explicitly
enable `(voicekey-tracking-mode 1)`. This tracking behavior is tested against
a private Emacs server; it does not make an asynchronous request a historical
snapshot of point.

An expired Emacs insertion is refused inside Emacs before mutation. Each
operation has a unique ID and a revocable permission file. A duplicate request
returns its previous result. A timeout or an error after mutation begins is
reported as uncertain: inspect the field and the saved transcript before
repeating it. Buffer text edits are grouped atomically; terminal writes cannot
be rolled back.

When a generic field loses focus, applications may keep or discard its
provisional text without reporting what happened. Voicekey leaves that text
alone and preserves the final transcript instead of guessing which field or
text to replace. Check any remaining provisional text before pasting. An IME
success notification means the request was sent to the compositor; the
protocol provides no application-level insertion acknowledgement.

Spacing uses current surrounding text inside the IME operation, or the
editor's actual insertion position. Where the application reports no cursor
context, a leading space is added only when voicekey was the last thing to
type in that window. A keystroke on any keyboard resets that fallback; a
modifier alone does not. Mouse movement is not tracked.

## Configuration

| key | meaning |
|---|---|
| `dictate_key`, `agent_key` | evdev key names or chords to hold (`KEY_F9`, `KEY_RIGHTALT+KEY_F23`) |
| `dictate_toggle_key`, `agent_toggle_key` | optional press-to-start, press-to-stop keys |
| `min_seconds`, `max_seconds` | shorter recordings are taps and discarded; longer ones are stopped and transcribed (stuck key?) |
| `recordings_dir` | keep the audio and both transcripts of every recording (off by default) |
| `[backend]` | final pass: `parakeet` (CPU) or `faster-whisper` (CUDA), and its model |
| `[streaming] model_dir` | live-preview model; `""` disables the preview |
| `[persistent]` | dedicated toggle key, speech detector, pause and silence thresholds, utterance and delivery limits |
| `[dictation] ime` | use the input method for preview and commit (default true) |
| `[dictation] inject` | without an input method: `wtype` (type it) or `clipboard` (copy it and say so) |
| `[dictation] max_delay_seconds` | deadline from key release; expired work is saved/copied, outstanding requests may remain uncertain |
| `[dictation] require_same_window` | copy instead of typing if the focused window changed (Emacs is exempt: its buffer is pinned) |
| `[polish]` | third pass: backend, endpoint, format, style, `min_words` (8), `max_wait_seconds` (4) |
| `[pipeline]` | pending count/audio limits, transcription and shutdown deadlines, recovery quota and history retention |
| `[polish.server]` | `model_file` makes voicekey run `llama-server` itself, as a child process; `command` names which one |
| `[agent]` | Hermes target, local or over SSH via Tailscale |

`install.sh` downloads the models the config names and verifies their
SHA-256 digests.

## Polish pass (optional)

A transcript is what you said; a draft is what you meant. The polish pass
hands the offline transcript to a language model and commits what comes
back, so "so um i need to like send the the report by uh friday no wait make
that thursday" lands as "So I need to send the report by Thursday." While
the model works, the raw transcript stands in the field as preedit, so the
words are visible at the same moment as before; what moves is when they
solidify.

The default model is [S1-mini by Superwhisper](https://huggingface.co/superwhisper/s1-mini),
a 0.6B text normaliser trained for speech cleanup. Earlier measurements were
about half a second per sentence and 1.5 s per 500-character paragraph on four
CPU threads. To turn it on:

```toml
[polish]
backend = "openai"
[polish.server]
model_file = "~/.local/share/voicekey/s1-mini-q4_k_m.gguf"
```

then `./install.sh` (which fetches the model and a pinned llama.cpp release
build, verifying both digests) and `systemctl --user restart voicekey`.
voicekey starts `llama-server` as a child process and stops it with the
daemon. `style` picks the register (`casual`, `semi-casual`, `semi-formal`,
`formal`). A distribution's llama.cpp works too (`command = "llama-server"`
under `[polish.server]`), but check its speed: Fedora's `llama-cpp` package
is a ROCm build whose CPU path took 1.4 s for a sentence where the upstream
CPU build took 0.4 s on the same machine.

Styles can be overridden per destination app with an exact, case-sensitive
app ID match. For example, under `[polish]`:

```toml
style = "semi-formal"
app_styles = { "org.signal.Signal" = "semi-casual" }
```

Unknown or unmatched apps use `style`. The app ID comes from the destination
captured when recording starts; persistent utterances inherit their session's
original destination, even after focus changes. This applies to both `s1-mini`
and `instruct` formats and does not change the minimum-word thresholds or agent
mode's cleanup bypass. On niri, `niri msg -j windows` lists app IDs. Restart
voicekey after changing these settings.

Any OpenAI-compatible chat endpoint works in place of the child server:
leave `model_file` empty and point `url` at Ollama, vLLM or a llama-server
elsewhere. With `format = "instruct"` voicekey sends its own prompt (or
yours, from `prompt_file`) for a general model that can do more, such as
LaTeX from a formula described in words.

`min_words = 8` skips the model for short quick-mode corrections; set it to `0`
to try polish for every nonempty quick dictation. Persistent mode has its own
`polish_min_words = 0` default. A skipped dictation still waits behind
earlier dictations, preserving speech order.

Empty or truncated replies, excessive growth or deletion, lost negations or
qualifications, and changes to already-written numbers trigger raw fallback.
The checks also limit novel vocabulary. They cannot establish semantic
identity: review polished paper prose, especially names and formulae. Both
raw and final text are kept in the recovery journal. `recordings_dir` retains
a separate audio/transcript corpus when configured.

The polish deadline starts when transcription finishes and includes queueing.
After `max_wait_seconds` (four seconds by default, within the delivery budget),
raw text becomes final. A hung request keeps its one execution slot; later
utterances use raw text until it returns. Late results never replace text
that has already moved on.

## Agent key (optional)

Hold the agent key, speak, release: the transcript goes to a persistent
Hermes TUI. On the first dispatch voicekey starts a dedicated tmux server in
a supervised systemd user unit, runs `hermes --tui` there in a neutral
working directory, opens a Ghostty window attached to it, and waits for the
composer to be idle and empty before submitting. Later dispatches reuse the
conversation; closing the window only detaches. With
`transport = "ssh-over-tailscale"`, recording and transcription stay local
and Hermes runs on another machine over OpenSSH with strict host-key
checking. Without Hermes installed, the agent key only shows a notification.

## Diagnostics

```sh
journalctl --user -u voicekey -f                                     # what each dictation did
~/.local/share/voicekey/venv/bin/python -m voicekey --check          # models, input method, keyboards
systemctl --user stop voicekey                                       # frees the input method, then:
~/.local/share/voicekey/venv/bin/python -m voicekey --replay x.wav   # dictate a 16 kHz mono WAV
```

`--check` exits 0 when ready, 2 when no keyboard is readable, 3 when only
the agent target is unavailable, 1 on a configuration, dependency or model
failure; with the polish pass on it also runs one sentence through the model.
For a local polish model, the check starts a temporary server on a free
localhost port with its own temporary log, so it can run alongside the daemon.
This validates model startup and generation independently of the daemon's
configured endpoint. External polish endpoints are tested at their configured
URL. Transcription, polish and delivery have separate supervised workers;
a clipboard copy is given three seconds. The polish server's output goes to
`~/.local/state/voicekey/polish-server.log`.

## Persistent dictation

Set `[persistent] key = "KEY_F11"` (or a dedicated evdev chord), run
`python -m voicekey --download` to fetch the small Silero speech detector, and
restart the service. Reserve the key in your compositor, as for F9/F10; for niri:

```kdl
F11 repeat=false allow-inhibiting=false hotkey-overlay-title="Persistent Dictation" { spawn "true"; }
```

Press once to listen continuously, and again to stop. Release does nothing;
Escape remains an ordinary editing key. A persistent notification shows
whether the microphone is listening, finishing, off or paused. Automatic
startup never enables the microphone.

Speech pauses of `pause_seconds` (default 1.2) queue an utterance for
transcription and optional polish while capture continues. Pending text and
new live text share one preview, and final commits remain in speaking order.
The speech detector works independently of the optional streaming recognizer.
Persistent mode sends even short meaningful utterances through the configured
cleanup model. `[persistent] polish_min_words` controls its threshold separately
from quick dictation. Whole utterances containing only recognized hesitation or
noise interjections (um, uh, er, erm, ah, eh, ach, ugh, gah, hmm, and stretched
spellings such as errrr or uhhhh) are omitted by default. Their raw text and
the drop reason remain in the journal. Mixed utterances, quoted words and
hyphenated responses such as uh-huh/uh-uh are not dropped by this rule.
Set `[persistent] drop_filler_only = false` when dictating literal interjections.
Empty model replies for meaningful text still fall back to the raw transcript.

`silence_seconds` (default 120) without detected speech turns listening fully
off; only the key starts another session. `max_utterance_seconds` (default 30)
forces a cut during uninterrupted speech. All three settings are configurable.

In Emacs, the session stays bound to the buffer where it started. Dictate in
section 2, read a PDF while dictating into section 2 in the background, then
return and move point to section 5: subsequent commits follow point there.
Another Emacs buffer does not become the destination. Let pending text land
before moving point. Each commit follows the existing Evil insertion gesture.
Native inline previews depend on the original Wayland activation; after it
ends, the session uses notification previews while pinned Emacs insertion
continues. Inline previews return with a new session.

Generic IME fields and `wtype` destinations require their original activation
or window to remain available. Losing that destination pauses capture and
preserves pending text; it never redirects the queue to another field. Press
the key after draining to start a fresh session at the desired destination.
Clipboard-only targets cannot start persistent dictation.

The queue uses the existing count, audio and recovery limits. A capture slot
reserves `max_utterance_seconds + 4` seconds of audio, including room for
processing lag. Long silence retains only a short lookback and does not hold
the agent coordination lock once earlier utterances are settled. Overload,
failed storage, lost keyboard or failed capture stops the microphone and
preserves available speech. There is no automatic resume after a pause.

Queued persistent text does not inherit quick dictation's ten-second age
limit. Transcription, polish and insertion calls still have finite deadlines.
Stopping signals the microphone immediately, drains for
`pipeline.shutdown_seconds`, and saves the remainder without repeatedly
overwriting the clipboard. Session and ordered utterance IDs appear in the
recovery journal. A paced WAV can exercise this path with
`--replay recording.wav --persistent`; this CLI command performs real desktop
delivery, whereas the automated replay tests use isolated targets.

## Recovery and limits

Each accepted recording is saved before transcription under
`~/.local/state/voicekey/sessions/`, using a unique ID. Its `.wav` contains
captured audio; `.jsonl` records stages and delivery attempts; `.txt` is a
readable transcript history. Known text is saved before delivery, including
before a clipboard attempt. `last-recovery.txt` remains a convenience copy;
a later failure does not overwrite the individual records.

Successful audio is removed after text and the outcome are saved, unless a
recording/transcription failure makes the original audio useful for recovery. Unresolved
audio and text remain for manual recovery. Successful text is retained for
up to seven days, and may be removed sooner to make room. The default quota
is 256 MB; unresolved records are never automatically deleted to meet it.
A full or unavailable recovery store disables new recordings until it is
repaired and the daemon restarted. Files are private to the user.

The pipeline admits at most eight recordings for capture/transcription/dictation,
with a separate eight-prompt limit for agent dispatch. It reserves recovery space and audio capacity before
capture (180 seconds by default, including a full `max_seconds` for a new
recording). Full admission refuses the key-down rather than capturing and
then discarding it. Recorder failure and keyboard disconnect preserve the
available samples. Only a deliberate short tap is discarded without journaling.
A native model that ignores its timeout cannot spawn an unlimited succession
of replacement threads.

SIGTERM and Ctrl-C stop capture, drain pending work for `shutdown_seconds`
(default ten seconds), save unresolved work and invalidate pending targets.
Bounded resource cleanup follows; the systemd service imposes a 30-second
stop limit. On restart, mode is off and old attempts are not automatically
retried. Journaling protects completed writes against a process crash; it
does not promise recovery of an in-memory recording or unflushed data after
power loss. An uncertain delivery remains uncertain even when its text is
safely saved.

## Development

Run `python -m unittest discover -q` in the project environment. The suite
uses fake targets and a real paced WAV source, local HTTP servers, and a
private Emacs server; it never types into the current desktop. If Emacs and
Evil are available, editor transactions are also exercised in batch Emacs.
CI installs both. See [the architecture](docs/persistent-mode-architecture.md)
and [the audit](docs/audit-2026-09-05.md) for the persistent-mode prerequisites.

## Agents

Coding agents drive the same desktop — `emacsclient`, `wl-copy`, `wtype`,
compositor actions — and one of them evaluating Lisp in Emacs mid-dictation
can steal focus or the clipboard. From key-down until dictation is delivered
or preserved for recovery,
voicekey holds an exclusive `flock` on `$XDG_RUNTIME_DIR/voicekey/lock`. A
hook that takes a shared lock before such tools run, waits a bounded time
and then refuses with a reason keeps agents out of the way (for Claude Code
and Codex: `ai/shared/hooks/voicekey-lock.sh` in the config repo). The lock
is advisory; voicekey retries acquisition without delaying capture. The
utterance owns the lock through release finalization and all pipeline stages.
Agent prompts release their gate ownership once transcription is queued for
agent dispatch; a busy agent does not lock desktop tools out for minutes.

## Caveats

- Membership in the `input` group lets every process of your user read raw
  keyboard events. That is the tradeoff for press-and-release on Wayland;
  a minimal privileged helper would be the fix.
- The agent path infers Hermes's state from its visible TUI, so a Hermes
  update can break the "never submit into a dialog" guarantee until the
  patterns are refreshed.
- Tested on Fedora with niri. The sway and Hyprland focus queries and the
  other listed compositors follow the same protocols but have not been
  exercised.

## License

MIT. `voicekey/_input_method_v2.py` is generated from wlroots'
`input-method-unstable-v2` protocol, whose MIT notice it carries.
