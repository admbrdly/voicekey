# Persistent mode: architecture and repaired foundation

Updated 2026-09-05 after [the architecture audit](audit-2026-09-05.md).
This replaces the earlier architecture's proposed contracts. Persistent
listening is **not implemented**. The hold-to-talk repairs below are built;
segmentation and continuous sessions remain to be implemented. The earlier
feasibility study is historical context.

**Preview decision (2026-09-05).** Use native Wayland preedit in Emacs as well
as other supported applications. Keep the shared path as the default for
persistent mode. The initial writing workflow assumes that pending text can
finish before point moves; buffer-attached overlays, per-utterance anchors and
editing committed regions are optional later work, justified by actual use.

## Handoff for the next session

This is the current implementation plan. Start with **continuous capture and
pause segmentation**, then follow [Next work](#next-work) below. The older
`persistent-mode.md` and the audit's original work order are historical context.

The last verified state (2026-09-05) passes **199 Python tests**, including a
batch run of **15 Emacs editor tests** and a private-server binding test:

```sh
~/.local/share/voicekey/venv/bin/python -m unittest discover -q
```

The suite needs localhost sockets. Its editor checks use batch/private Emacs,
not the user's live server. The daemon and local polish server were restarted
successfully after restoring shared Wayland previews. Hold/toggle dictation
works through the repaired pipeline; neither existing toggle key is persistent
listening. Polish skips dictations under eight words by default.

The main entry points and remaining one-recording assumptions are:

- `recorder.py`: buffers until `stop()`; add sample-indexed cuts while the
  capture process continues, with bounded retained audio and silence handling.
- `capture.py`: `Session` currently owns one gesture's live decoder; arrange
  decoder rollover and suffix replay at utterance boundaries.
- `pipeline.py`: `_finalize` currently stops the recorder for every job; allow
  an already-cut utterance to enter the same downstream pipeline.
- `ledger.py`: lifecycle and admission are built; session grouping and a
  combined rendering of all pending utterances are still missing.
- `target.py` and `voicekey.el`: targets/previews close after one delivery,
  and Emacs consumes its pin on insertion. Separate the continuous session's
  binding/preview lifetime from each utterance's delivery attempt. This does
  not require overlays or per-utterance cursor markers.
- `daemon.py` and `config.py`: add the dedicated persistent binding, controller
  states, visible microphone status, focus/silence policy and bounded drain.

The first acceptance milestone is a paced WAV containing several utterances:
capture stays on across cuts, audio ownership is accounted for, live text keeps
up while earlier text finishes, commits stay ordered, and stopping preserves
pending work. Then test actual dictation before tuning pause thresholds. Keep
hold/toggle behavior covered throughout.

## What is built

`daemon.py` handles keys, capture ownership and resource lifetime.
`capture.py` owns one optional live decoder. `ledger.py` owns an admitted
utterance's lifecycle, revision, audio reservation, gate ownership and delivery
attempt. `pipeline.py` runs bounded FIFO stages, journals before side effects,
and supervises fallible calls through `work.py`. The four delivery targets
share explicit capabilities and an outcome enum. Speech and polish adapters
remain independent of mode control.

Hold-to-talk and the existing toggle keys use the same path:

    admit → capturing → finalizing → transcribing → polishing → ready
                                                        → delivering → terminal

An ID is unique to the recording; in hold-to-talk, one session is one
utterance. Admission is bounded across capture, transcription and dictation;
agent dispatch has a separate bounded backlog so it cannot exhaust dictation
slots. Every dictation traverses the polish queue, including ones that skip
the model, so a short dictation cannot overtake an earlier long one. The default
polish threshold is eight words; `min_words = 0` disables that threshold.

The ledger does no I/O. It returns immutable snapshots under a short lock;
workers perform journal and target operations outside that lock. A delivery
operation is reserved exactly once before its intent is journalled. Late
results cannot transition a terminal record. Completed in-memory history is
bounded. Audio is released after its last consumer, and a timed-out native
call retains one bounded slot rather than creating replacement threads.

## Delivery evidence and destination identity

| Target | Identity | Evidence after a successful call |
|---|---|---|
| Emacs | A promptly acknowledged buffer pin | Editor confirmed the operation ID |
| IME | One activation generation | Requests flushed to the compositor |
| wtype | A checked window | The typing process completed |
| Clipboard | No insertion destination | Clipboard command completed |

An IME activation is not a field handle that survives deactivation. A new
activation in the same window may belong to another field. Generic delivery
therefore refuses a changed activation and preserves/copies final text.
It does not infer that missing provisional text was dropped, or automatically
delete surrounding text after focus returns. The low-level replacement helper
requires the exact checked generation and surrounding-text revision and
refuses a mismatch at execution.

Bound, submitted, confirmed and saved are different facts. Outcomes are
`refused`, `submitted`, `confirmed`, `unknown`, `copied`, `saved` and `dropped`;
unexpected infrastructure failures are reported separately. In particular,
submitting a generic IME request does not acknowledge application insertion.
A timeout or partial failure is saved as uncertain, with no automatic retry or
clipboard fallback. The journal is recovery evidence, not proof that an
outstanding application operation cannot still execute.

The generic window check cannot atomically prevent focus changing during
`wtype`. Buffer pin acquisition is also asynchronous: without editor tracking,
it names the selected buffer when Emacs handles a request within 250 ms.
Voicekey does not claim those mechanisms guarantee an atomic key-down snapshot.

## Emacs transaction groundwork

`voicekey/voicekey.el` is packaged and loaded on demand. Loading it installs
no hooks. Pinning must receive a timely acknowledgement; failed/late pins do
not authorize insertion. Insertion carries a unique operation ID, an expiry
checked inside Emacs, and a revocable permission file. Repeated operations
return the previous result. Definite pre-mutation refusals differ from errors
after mutation began. Buffer edits are atomic; terminal writes cannot be.
Spacing is computed at the actual gesture position. Hold-to-talk follows point
within the pinned buffer, retaining its existing gesture semantics.

Emacs uses the shared Wayland preview, falling back to notifications when no
usable activation is available. Its buffer pin starts before waiting for the
IME binding. Before final insertion, its preview is closed to new updates and
the clear request is flushed within the remaining delivery budget. A stale
activation or a newer preview owner is left alone. Failed cleanup cannot
start an editor insertion. These are ordered local submissions through two
channels, not an atomic application transaction. The optional
`voicekey-tracking-mode`, enabled explicitly by the user, records the last
command-loop buffer and marker. A private-server test verifies that an
`emacsclient` evaluation which switches buffers does not run `post-command-hook`
and therefore does not redirect this tracking state. Batch tests exercise
normal, insert and visual state, operator/block refusal, narrowing, killed and
read-only buffers, cancellation, expiry, duplicate operations and hook failure.

The initial persistent mode will keep a buffer pin and insert at point when
each commit executes, as hold-to-talk does. The user normally waits for pending
text before moving point. Per-utterance markers are not a prerequisite.
If later editing needs them, a marker planted by a helper records point when
that helper executes, not at an earlier audio cut. The existing marker spike
verifies that insertion-type `t` preserves order at a shared position. Exact
region replacement would also need fixed start/advancing end markers and
validation against intervening edits.

## Recovery, deadlines and stopping

A unique `.wav` is written before transcription. JSONL and readable text
records retain known tiers and delivery intent before insertion or copying.
Atomic audio publication and private files protect completed writes against a
process crash. Capturing audio still in memory, and filesystem writes not
flushed to durable storage before power loss, are outside that guarantee.

Successful audio is removed after the final text and outcome are recorded.
Unresolved audio/text remain until manually recovered or removed. Successful
text is retained up to `history_days`, subject to the quota. If preservation
cannot proceed, new capture is disabled with an error; the pipeline does not
silently continue on an assumption that recovery succeeded.

Hold-to-talk uses an absolute insertion deadline from key release, including
finalization and all queues. Transcription and polish additionally have their
own bounds. The polish deadline starts at transcription completion. A
supervisor adopts raw text at expiry while a stuck request retains its slot;
late results are discarded. Clipboard, IME, editor and agent operations have
bounded waits. A slow side effect is classified as uncertain rather than
assumed not to have happened.

The utterance retains gate ownership through finalization and delivery or
recovery. Admission reserves count and audio capacity before the microphone
starts. A contested advisory lock is retried without blocking capture. Agent
work releases its desktop gate after text is queued for dispatch, while still
counting against its separate backlog limit.

Shutdown stops accepting recordings, signals capture to stop, and drains
within the configured period. Remaining sources are stopped, recovery is
preserved, pending target permissions are revoked, and owned resources are
closed with bounded cleanup. New sessions get new IDs. Old uncertain attempts
are never automatically replayed. Agent commands also check the operation's
cancellation/deadline before each external command.

## Persistent mode contract, still to implement

The user explicitly enables a continuous session with a dedicated key.
VAD-driven pauses create utterances that use the same lifecycle and delivery
path as hold-to-talk. VAD remains independent of the optional streaming model.
Commit once in order after finalization/polish; an editor with verified region
handles may support later replacement as a separate operation.

The additional contracts are:

1. **Session identity and authority.** Every event carries session ID,
   utterance ID and revision. Stop/drain revokes authority for later updates.
   Mode state is separate from target availability and stage progress. A
   pinned Emacs buffer remains available when another application gains focus.
2. **Sample ownership.** Cuts use absolute sample indices. Account for samples
   assigned to utterances and explicitly discarded no-speech silence. A
   retrospective pause cut needs retained suffix frames so the new live
   decoder receives samples already seen by the old decoder. Forced maximum
   cuts can split speech; they must not claim to occur only in silence.
3. **Rendering versus operations.** A preview snapshot contains every pending
   utterance at its best-known tier, including a final utterance waiting for
   its commit. Preview updates may be coalesced; insertion commands may not.
   Reserve an attempt before posting it. IME commit can carry the current
   provisional tail in the same request, preserving newer live text.
4. **Journal ordering.** Ordered events are written outside ledger locks.
   Delivery waits for the relevant journal acknowledgement. Bound outstanding
   journal events, pending text and retained audio. Failed storage or sustained
   overload pauses capture and preserves what is available.
5. **Unavailable targets.** Hold pending text while useful. Generic
   reactivation requires deliberate rebinding; it does not prove the old field
   or its provisional text survived. Unknown preedit retention remains unknown.
   A verified editor anchor supports continuing while focus moves elsewhere.
6. **Microphone policy.** Distinguish armed/capturing, holding, paused and off.
   Automatic resume is permitted only while the user has left the session
   armed, and is announced. Explicit off and daemon restart never turn the
   microphone on. Absence and no-speech limits must be specified independently.
7. **Voice commands.** Deterministic matching precedes polish. Newline and
   paragraph insertion can use ordinary text. `scratch that` requires verified
   replacement capability; unsupported targets must report that limitation
   without deleting guessed text.
8. **Bounds and drain.** Set utterance length, pending count, audio seconds,
   pending text, silence/absence limits and drain deadline. A persistent session
   may choose a different insertion-age policy, but worker and memory limits
   remain finite. Preserve pending work at stop; saved and uncertain are not
   interchangeable outcomes.

## Next work

1. Build continuous capture and sample-indexed segmentation against paced WAV
   fixtures and a fake VAD. Assert coverage, retrospective suffix replay and
   hard cuts under decode lag.
2. Extend the current per-recording ledger and preview ownership into one
   continuous session with several utterances. Render pending raw/final text
   and current live text together through the shared Wayland preview. Keep
   ordered commits and recovery while the next utterance records. Emacs keeps
   its pinned final insertion; test preview clearing and tail updates across
   that handoff.
3. Add the persistent controller with explicit armed/pause/off policy and
   target availability. Test overload, lost keyboards, failed capture, dead
   targets, stale results and stop during drain.
4. Exercise the complete mode with paced WAV fixtures, then measure real
   latency, CPU, memory, silence cuts and paper-dictation quality on both
   machines. This is the first working persistent-mode milestone.
5. Revisit editor overlays, per-utterance anchors, voice editing commands,
   additional providers or paragraph rewriting only as use demonstrates need.

The existing deterministic tests cover the repaired lifecycle and a full WAV
capture-to-fake-target run. CI runs the Python suite and batch/private-server
Emacs tests. Real compositor/application behavior and speech-model accuracy
remain integration measurements, not conclusions inferred from mocked tests.
