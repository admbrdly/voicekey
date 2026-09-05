# Cleanup policy and model simplification — 2026-09-06

The first live persistent test produced 13 ordered utterances. S1-mini ran on
six; changes were mainly punctuation. An isolated filler bypassed polish under
the shared eight-word threshold, and the final stop was misreported as capture
failure. These observations support the following implemented changes:

- F11 uses `persistent.polish_min_words = 0`: short meaningful utterances
  receive cleanup. F9 retains its separate eight-word threshold; F10 continues
  to dispatch raw transcripts to the agent.
- `persistent.drop_filler_only = true` omits whole utterances of recognized
  hesitation/noise interjections. Repeated-letter normalization covers the
  user's examples er, ach, um, errrr, uhhhh, ugh and gah. This is a narrow text
  heuristic, not semantic classification. It preserves mixed text, quotes,
  numbers, non-English text and hyphenated yes/no responses. Disable the option
  for literal interjections. Raw text and the drop reason remain journalled.
- Meaningful input still cannot be deleted by an empty LLM reply. Deliberate
  filler drops traverse the ordinary ordered delivery queue without an
  insertion attempt; finalized filler is omitted from pending previews.
- Recorder shutdown distinguishes a stop signal we sent from an already
  failed process. A requested SIGINT may cause pw-record to exit nonzero before
  finalization reaches it. That alone is not a capture failure.

## RAM and locality

All four dictation models are local: Silero, Nemotron, Parakeet and S1-mini.
S1-mini is itself an LLM, running in a local llama-server child. The `openai`
backend name denotes its compatible API format, not a cloud destination.
An agent reached through F10 can independently use external services.

The live daemon and cleanup process used approximately 2.9 GiB combined RSS
after the first test, with no swap attributed to those processes. The desktop
has about 31.1 GiB usable RAM (32 GB installed), with about 21.6 GiB available
at this review. The current cost is affordable on this desktop, while still
large enough to justify optimization. Models remain loaded when capture is
off to avoid repeated loading; they do not all perform inference continuously.

## One speech model for preview and final transcription

This is a real prospect. NVIDIA's
[Parakeet Unified model card](https://huggingface.co/nvidia/parakeet-unified-en-0.6b)
describes jointly trained streaming and offline modes sharing all model
parameters. Streaming uses limited audio context; offline decoding gets full
utterance context. It is already the checkpoint behind voicekey's final pass.

Our current runtime still loads an offline Parakeet export and a separate
cache-aware Nemotron model. Sherpa's
[Parakeet streaming implementation](https://github.com/k2-fsa/sherpa-onnx/commit/ae2bc66)
has a separate export with context settings chosen at export time. Merely
loading two exports from the same checkpoint would not establish shared RAM.
NVIDIA also documents that Unified's buffered streaming recomputes preceding
context, so its CPU/latency cost needs comparison with Nemotron's caching.

The next useful experiment is one runtime/model instance serving preview and
final decoding, measuring actual memory, CPU, boundary quality and latency.
Do not replace the working path solely because both exports say Unified.

**Follow-up completed:** the [CPU and sharing benchmark](asr-benchmark-2026-09-06.md)
found stock Parakeet streaming about 13.4 times more expensive than the current
Nemotron preview. Shared initializers reduced RAM only when runtime packing
was disabled in the prototype, with an additional performance penalty. Keep
the current speech models; a production sharing refactor is not justified by
these measurements. All the user's machines now have 32 GB RAM, so performance
takes priority over reducing the model count.

## S1-mini's editing strength

The [S1-mini model card](https://huggingface.co/superwhisper/s1-mini) identifies
a 0.6B Qwen3-derived language model specialized for transcript normalization.
Its supported controls concern register, prose/lists and general/email layout.
Formal styling mainly adds contraction expansion. The model was trained with
thinking disabled; that flag is not an editing-strength control, and arbitrary
chat instructions are outside its intended interface.

An isolated local server compared semi-formal and formal on ten transcripts
(20 requests), without changing the live server or sending text externally.
Both styles returned empty output for the filler-only examples and normalized
the short colloquial question that quick-mode bypass had previously left raw.
Formal expanded contractions but still retained the conversational fillers
and unresolved self-correction in the harder cases. Both retained the ASR error
in the poetry example. Meaningful short negatives and qualifications survived.
This small diagnostic is not a general quality benchmark.

Keep semi-formal for now. A more capable instruction-following local LLM is a
reasonable candidate to replace S1-mini for substantive cleanup, but should
first be evaluated on prose, corrections, negations, terminology and formulas.
Context across utterances is a separate extension; the current pass still
cleans one utterance at a time. No ASR model or cleanup model was changed by
this review, and no cloud processing was enabled.
