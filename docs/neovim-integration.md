# Neovim destination integration — 2026-09-24

Implemented on `feature/pinned-neovim`, based on master `53231c8`.
The first commit (`e9c9760`) extracts `PinnedEditorTarget`; it changes neither
Emacs protocol v4 nor its delivery behavior. Its 425-test suite passes unchanged.
The second commit adds Neovim resolution, buffer transactions, focus reports,
tests and documentation. No live editor was contacted, service restarted,
user configuration edited, model loaded or microphone used during this work.

## Activation and delivery

There is one resolver: `target.bind()`. Emacs remains its existing adapter.
Known terminal app IDs consult Neovim registrations; all other destinations keep
their existing IME/wtype/clipboard policy. No router script or capture client is
spawned by the resolver. A bound Neovim failure has no typing or clipboard fallback.

**Single-shot path:** keyboard controller → daemon recording → resolver →
acknowledged Neovim extmark → finalization/transcription/polish → journalled
attempt/permit → bounded RPC → Lua expiry/permit/buffer checks →
`nvim_buf_set_text` → acknowledged outcome → pin cleanup. Final insertion does
not depend on current compositor focus. The existing single-shot controller,
replay and client-capture APIs remain available. `:VoiceKey` still uses the
client-capture path; it shares the same Lua position/spacing/insertion helpers.

**Normal dictation keys on this master actually start persistent capture** with
tap/hold behavior: a tap leaves capture running; holding then releasing stops
it. Neither mapping nor gesture handling was changed. Right Win and F23 tests
exercise this path. The persistent controller starts the microphone before
binding, segments PCM using the existing VAD, and queues utterances into the same
ordered pipeline. One acknowledged editor pin survives repeated commits.
The session renderer sends live/raw/final pending tiers as virtual text at that
pin; insertion clears its old preview and advances the extmark, then renders the
remaining tail. No terminal IME preview or commit is used for Neovim.

Pause stops capture at a reported window/editor focus change, but earlier speech
may finish at its original editor pin. Follow cuts at the observed audio boundary
and rebinds through `target.bind()`; old pins are released after their pending
speech drains. Pin policy continues into the original buffer in the background.
Health checks stop capture on editor/socket/buffer loss; outstanding speech stays
in recovery. Unknown insertion is never retried automatically.

Neovim deliberately uses a fixed, advancing extmark in both hold and persistent
paths. Editing elsewhere moves it with the buffer; moving point does not retarget
it. This differs from Emacs, which retains its original follow-point behavior.
Selections/operator-pending states are refused when acquiring a Neovim pin.

## Transport and focus

`nvim --server SOCKET --remote-expr` transports JSON to the Lua endpoint. It adds
a process per request but needs no Python dependency or custom MessagePack
implementation. A 50-call isolated local measurement gave **2.31 ms median,
7.03 ms maximum**, including startup. Persistent insertion, preview and health
checks are bounded; probes and preview calls use 100 ms, acquisition uses 250 ms,
and insertion/preview-tail calls use the remaining delivery budget. Lua also
checks wall-clock expiry and the journal's revocable permission file before
mutation, and remembers operation results through their execution deadline.
This measurement does not establish latency under load or on the laptop.

Registration uses one private JSON file per process in
`$XDG_RUNTIME_DIR/voicekey/nvim/`; a last-writer-wins file cannot represent
multiple instances safely. Validation requires a responsive socket, matching
Neovim PID/server address, ancestry under the terminal PID when reported, and a
true focus flag. Multiple valid focus claims are refused. The compositor is
checked again after binding. A focus change during binding refuses the target.

The focus flag starts false, becomes true on `FocusGained`/`VimResume`, and clears
on `FocusLost`, suspension or entry into a Neovim terminal buffer. Startup only
registers the instance; it does not assert focus. If the terminal sends no initial
focus event, switch away and back once. Reports use async local sockets, include
a sequence number, and are checked against socket peer credentials. The control
server forwards them immediately to the persistent focus handler; it does not
wait for an evdev tick. Out-of-order/repeated reports are ignored. The persistent
controller contains no Neovim target type checks or direct editor insertion calls.

## Remaining limitations

- **A lost terminal focus event can still select the wrong Neovim buffer.**
  Ghostty's shared PID cannot distinguish windows, tabs or panes. One stale true
  focus flag can satisfy all checks. Two true flags cause refusal instead.
- **The process-tree policy is intentionally incomplete.** With no validated
  registration but a Neovim descendant of the focused terminal, typing is refused.
  A shell tab next to Neovim in the same Ghostty process is consequently refused.
  There is no narrower reliable tab identity in the available evidence.
- **Detached tmux, SSH, containers, renamed processes and hidden `/proc` data can
  defeat ancestry.** A detached tmux server may not descend from the terminal's
  client process. If no Neovim descendant is visible, the chosen middle policy
  preserves ordinary IME delivery, even though a Neovim might actually be visible.
  Thus `focus-events on` is necessary for tmux focus reporting but is not enough
  to prove ancestry. This is not a guarantee of safe dictation in every tmux setup;
  terminal-client ↔ tmux-server/pane association needs additional evidence.
- Without a compositor PID, all local Neovims are considered; this can refuse
  unrelated shells or accept the only stale focus claim. Unknown terminal app
  IDs must be added through `VOICEKEY_TERMINALS` or they retain generic delivery.
- Focus and pin acquisition are asynchronous, not atomic key-down snapshots.
  A switch away and back before observation can be missed. Exit reports may not
  flush before process exit; periodic pin health checks catch the vanished socket.
- Buffer switches within Neovim do not rebind the session. Buffer closure,
  unloading or loss of modifiability refuses insertion. A successful API response
  confirms insertion, not that the user has saved the buffer to disk.
- Abandoned Lua pins are aged out after a day on the next request; normal stop,
  completion, cancellation and follow cleanup explicitly unpin. A daemon crash
  may leave a cosmetic preview until cleanup/restart. No automatic replay occurs.

## Adam's branch and compatibility

Reviewed `adam/adam-local`, including its router, evdev/control changes,
`contrib/bash`, `contrib/claude-code` and tests. Its useful idea here is refusing
unsafe terminal routing. Its shell/title integrations were not imported.

`evdev = false` is a reasonable separate opt-in for compositor-only installations,
but deliberately disables the daemon key listener, hold/tap and agent keys.
It does not solve this activation requirement. The wake pipe is a separate useful
responsiveness improvement for ordinary control requests (the current keyboard
loop can sleep before draining them). Neither change was merged. Editor focus
reports use a dedicated immediate callback on the existing control server.

`voicekey-route` is retained for existing explicit bindings and its tests still
pass. It remains a single-shot compatibility path with weaker last-writer/socket
checks; the daemon never runs it. New users need neither that binding nor F12.

## Upgrade and manual validation

1. Update the daemon's installed package if it is not an editable checkout, then
   restart `voicekey.service` yourself when ready. It was not restarted here.
2. Load the plugin at startup (`lazy = false`); remove a `cmd = "VoiceKey"`-only
   lazy-loading restriction. Change `init.lua` only if necessary for startup
   loading. No dictation key change is required.
3. Restart Neovim to load both updated Lua files. Confirm `FocusGained` reaches
   it, switching away/back if needed. Emacs needs no reload.

Real microphone checks still required on both machines:

- Right Win and the physical Copilot chord: tap/hold, toggle, stop during
  processing, agent-key behavior, and multiple utterances with actual models.
- Normal/insert mode, edits during speech, virtual preview placement and latency;
  confirm no transcript appears as terminal commands.
- Ghostty windows/tabs and tmux panes: verify real focus reports and PID ancestry,
  including Neovim-to-shell moves, pause, follow and pin. Do not infer tmux safety
  from the headless tests.
- Follow Neovim → graphical Emacs → another app → Neovim, with speech queued
  before each switch; close Neovim while transcription is pending and inspect
  retained audio/text. Check the widget's Neovim label and attention-only failures.

## Automated verification

Baseline at `53231c8`: **425 tests passed in 32.478 s**, with the installed
`~/.local/share/voicekey/venv/bin/python -m unittest`. The literal system
`python -m unittest` initially ran 275 tests with 3 failures/24 errors because
it lacks `evdev` and the sandbox denies test sockets. The valid baseline and
subsequent runs used installed dependencies plus permission for isolated local
sockets. Tests never contacted live editor servers.

The extraction's full suite passed **425 tests in 32.494 s**. The final full suite
passed **454 tests in 35.464 s** (29 additional tests), with no skips or failures.
`git diff --check` also passed. The expanded suite
covers temporary headless Neovim servers, temporary runtime directories, mocked
compositor/process evidence, synthetic audio and stubbed recognizers:

- Resolver selection and actual key-path selection for Neovim, Emacs, ordinary
  terminal IME and another app; tap/hold, F23, stop while processing and cancel.
- Missing/dead/unfocused/wrong-ancestry registration, conflicting instances,
  focus-unconfirmed startup, edited buffers, closed/unloaded/unmodifiable buffers,
  duplicate operations, revoked permits and Unicode/quote-safe transport.
- Mocked RPC failure with journal/audio retention, plus a genuinely blocked
  private Neovim whose queued request expires after its client times out.
- Repeated ordered commits, live and provisional virtual text, preview cleanup,
  real plugin/control-socket pause with no compositor event, follow across
  editor/application types and between instances inside one terminal window,
  background pin policy, old speech delivery after focus loss, and editor exit
  during processing with recovery.
- All pre-existing Emacs, persistent, client-capture, legacy-router and pipeline
  tests remain in the full run.
