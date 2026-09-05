# Parakeet for both passes — measured 2026-09-06

**Decision: retain Nemotron previews and Parakeet final transcription on this
desktop.** At a matching nominal 560 ms streaming latency, stock Parakeet
streaming was about 13.4 times more expensive in wall-clock computation and
could not keep up with real-time speech. Actual shared-weight storage was
demonstrated, but its memory benefit did not produce a performance benefit.
No production model, backend or service configuration was changed.

The user's criterion is performance; all their machines have 32 GB RAM. Saving
half a gigabyte alone does not justify worse dictation latency or accuracy.
The user accepted this decision after reviewing the results. The next step is
several days of ordinary persistent-mode use, followed by changes justified
by observed problems.

## Primary comparison

Hardware: Intel Core i5-13600, 14 cores / 20 logical CPUs, 32 GB RAM, CPU
execution. The existing voicekey service remained running. Each benchmark used
a fresh process, warmed the models, and ran serially with other benchmark
processes. Both native variants used sherpa-onnx 1.13.6 / ONNX Runtime 1.27.1,
two preview threads and four final-pass threads.

Twelve distinct speakers from the public LibriSpeech test-clean split were
selected deterministically before inference. Total audio: 161.745 seconds;
456 reference words after case/punctuation normalization. These are component
benchmarks with 100 ms audio frames and corpus utterance boundaries; VAD,
polish, preview rendering and editor delivery are excluded.

| Measurement | Current: Nemotron + Parakeet | Parakeet streaming + offline, separate instances |
|---|---:|---:|
| Total preview computation | 28.73 s | 385.55 s |
| Preview compute / audio duration | 0.178 | 2.384 |
| Preview CPU time, all process threads | 153.93 s | 1565.17 s |
| Final-pass time, median utterance | 0.616 s | 0.661 s |
| Maximum warm PSS, ASR process | 1.865 GiB | 1.911 GiB |
| Preview word errors | 11 / 456 | 13 / 456 |
| Final word errors | 5 / 456 | 5 / 456 |

A compute/audio ratio above 1 means the preview worker accumulates a backlog.
Final transcripts were identical for every native comparison clip: both
variants already use the same final recognizer. The two-word preview error
difference is too small to establish a general accuracy ranking. Scoring strips
case and punctuation but does not use the official leaderboard normalizer;
compound spelling and written-number formatting can count as word errors.

The cause of the large compute difference is visible in the implementation.
The Parakeet export processes a 6.16-second feature window to advance 0.16
seconds: 5.6 seconds left context, 0.16 seconds center and 0.4 seconds right
context. Past encoder work is repeatedly recomputed. Nemotron's cache-aware
streaming reuses past computation. Weight sharing does not remove that work.
See the [matching sherpa implementation](https://github.com/k2-fsa/sherpa-onnx/blob/v1.13.6/sherpa-onnx/csrc/online-recognizer-transducer-nemo-parakeet-unified-impl.h).

## Actual memory sharing

The two encoder exports contained 609,509,376 bytes of identical large
initializers (581.3 MiB); small constants needed for shape inference remained
inline. Decoder and joiner computation graphs, including their initializers,
were exactly equal across the two modes and could each be instantiated once.

The experimental owner maps one weight bank, constructs one `OrtValue` per
unique tensor, and supplies the same object to both encoder sessions. The
pointer is checked against the underlying array. Two small executable tests
verify shared ownership/output preservation and that unequal tensors never
alias. The prototype does not confuse one checkpoint name with one allocation.

On two corpus clips totaling 29.45 seconds, using the same Python ONNX Runtime
1.28.0 implementation and identical encoder graphs:

| Prototype setting | Maximum warm PSS | Preview compute / audio |
|---|---:|---:|
| Separate encoder initializers; normal packing | 1.743 GiB | 2.523 |
| Shared encoder initializers; normal packing | 2.248 GiB | 2.508 |
| Shared encoder initializers; packing disabled | 1.245 GiB | 2.697 |

Sharing the original weights while retaining independently packed runtime
copies increased RAM in this experiment. Disabling packing realized a roughly
0.50 GiB physical-memory saving against the equivalent unshared prototype,
with about 7% more preview computation. Warm RSS showed a roughly 0.57 GiB
saving. The prototype variants produced identical transcripts to each other.

This memory experiment uses a different runtime stack from native sherpa. One
final transcript differed from native in a proper-name spelling and punctuation;
exact native equivalence is therefore not established. It is a mechanism
prototype, not a production-ready replacement or an accuracy improvement claim.

## Thread and paced-replay checks

A separate 7.435-second packaged clip tested stock Parakeet with 1, 2, 4 and
8 preview threads. Compute/audio ratios were respectively 4.09, 2.33, 2.21 and
2.57. None kept up. Throwing more threads at this configuration did not resolve
the bottleneck, and CPU consumption rose sharply.

Real-time-paced replay of that clip confirmed the unpaced computation results:

| Paced check | First preview after start | Preview finishes after audio ends |
|---|---:|---:|
| Current native pair | 1.40 s | 0.07 s |
| Native Parakeet pair | 3.22 s | 10.87 s |
| Shared, unpacked prototype | 3.66 s | 13.97 s |

These are ASR timings, not end-to-end editor insertion times. The benchmark
retains an unlimited preview backlog for measurement; voicekey's bounded live
decoder would cancel lagging preview work instead of allowing this backlog.

## What a production refactor would require

A bounded recognition-backend/model-loading refactor could provide a common
owner with separate preview and final adapters. The existing capture, queue,
polish and Emacs contracts can use those adapters. Merely pointing two current
recognizers at the same files does not guarantee shared RAM: sherpa creates
separate encoder/decoder/joiner sessions for each recognizer.

Efficient native sharing would need deliberate lifetime management for shared
initializers and the runtime's optimized copies, while keeping each utterance's
decoder state independent. ONNX Runtime's
[C API for prepacked-weight containers](https://onnxruntime.ai/docs/api/c/struct_ort_api.html)
supports reusing prepared weights across sessions where supported. That
container is not exposed by the Python API used in this prototype. It would
need native integration and warm-memory validation, rather than disabling an
optimization in production solely to reduce RAM.

Even that would leave the repeated buffered encoder computation. There is no
performance case here for doing the production refactor now. A substantially
more efficient streaming execution strategy would be the relevant next
experiment if this direction is revisited.

## Scope and reproducibility

One full-corpus run per native variant, smaller prototype controls, a thread
sweep and paced smoke checks establish this large performance difference. They
are not a broad accuracy evaluation or a laptop/GPU benchmark. Other latency
settings, shorter left context, GPU execution and an optimized native shared
container were not benchmarked. No claim is made that every possible Parakeet
implementation is slower.

The [benchmark instructions](../benchmarks/README.md) include acquisition,
verification, execution and collection commands. [Exact results](../benchmarks/results/2026-09-06/summary.json)
include model hashes, the selection manifest, per-utterance transcripts,
preview traces, runtime versions, CPU affinity and RSS/PSS/swap measurements.
All measured ASR processes reported zero swap.
