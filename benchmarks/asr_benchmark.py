"""Isolated ASR comparison. No microphone, compositor, editor or polish calls."""
import argparse
import ctypes
import gc
import json
import os
from pathlib import Path
import re
import resource
import sys
import time
import wave

import numpy as np
import sherpa_onnx
import onnxruntime

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from voicekey.backends import ParakeetBackend, StreamingBackend, StreamSession
from voicekey.config import load
from parakeet_shared import ParakeetPair


def read_audio(path):
    with wave.open(str(path), "rb") as wav:
        assert (wav.getframerate(), wav.getnchannels(), wav.getsampwidth()) == (16000, 1, 2)
        return np.frombuffer(wav.readframes(wav.getnframes()), dtype="<i2").astype(np.float32) / 32768


def memory():
    data = {}
    for line in Path("/proc/self/smaps_rollup").read_text().splitlines():
        if line.startswith(("Rss:", "Pss:", "Private_Dirty:", "Shared_Clean:", "Swap:")):
            key, value, _unit = line.split()
            data[key.rstrip(":") + "_MiB"] = int(value) / 1024
    data["peak_Rss_MiB"] = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024
    return data


def tokens(text):
    return re.findall(r"[a-z0-9]+(?:'[a-z]+)?", text.lower().replace("’", "'"))


def errors(reference, hypothesis):
    ref, hyp = tokens(reference), tokens(hypothesis)
    row = list(range(len(hyp) + 1))
    for i, left in enumerate(ref, 1):
        current = [i]
        for j, right in enumerate(hyp, 1):
            current.append(min(row[j] + 1, current[j-1] + 1, row[j-1] + (left != right)))
        row = current
    return {"errors": row[-1], "reference_words": len(ref)}


class NativePair:
    def __init__(self, cfg, root, *, parakeet, live_threads=2):
        self.final = ParakeetBackend(cfg.backend, cfg.language)
        if parakeet:
            folder = root / "sherpa-onnx-nemo-parakeet-unified-en-0.6b-int8-streaming-560ms"
            self.live = sherpa_onnx.OnlineRecognizer.from_transducer(
                encoder=str(folder / "encoder.int8.onnx"), decoder=str(folder / "decoder.int8.onnx"),
                joiner=str(folder / "joiner.int8.onnx"), tokens=str(folder / "tokens.txt"), num_threads=live_threads)
        else:
            self.live = StreamingBackend(cfg.streaming).recognizer

    def session(self):
        return StreamSession(self.live)

    def transcribe(self, samples):
        return self.final.transcribe(samples)


def run_clip(model, audio, *, paced=False):
    duration = len(audio) / 16000
    live = model.session()
    clock = time.perf_counter()
    cpu = time.process_time()
    virtual = 0.0
    compute = 0.0
    updates = []
    previous = ""
    lag = 0.0
    for start in range(0, len(audio), 1600):
        end = min(start + 1600, len(audio))
        available = end / 16000
        if paced:
            time.sleep(max(0, clock + available - time.perf_counter()))
        before = time.perf_counter()
        text = live.feed(audio[start:end])
        elapsed = time.perf_counter() - before
        compute += elapsed
        virtual = (time.perf_counter() - clock if paced else max(virtual, available) + elapsed)
        lag = max(lag, virtual - available)
        if text and text != previous:
            updates.append({"audio_seconds": available, "seen_seconds": virtual, "text": text})
            previous = text
    before = time.perf_counter()
    text = live.finish()
    elapsed = time.perf_counter() - before
    compute += elapsed
    virtual = (time.perf_counter() - clock if paced else max(virtual, duration) + elapsed)
    stream_cpu = time.process_time() - cpu
    before, cpu = time.perf_counter(), time.process_time()
    final = model.transcribe(audio)
    final_elapsed, final_cpu = time.perf_counter() - before, time.process_time() - cpu
    return {"audio_seconds": duration, "stream_compute_seconds": compute, "stream_cpu_seconds": stream_cpu,
            "stream_rtf": compute / duration, "first_preview_seconds": updates[0]["seen_seconds"] if updates else None,
            "max_preview_lag_seconds": lag, "preview_finished_after_audio_seconds": virtual - duration,
            "preview": text, "updates": updates, "final": final,
            "final_seconds": final_elapsed, "final_cpu_seconds": final_cpu,
            "memory": memory()}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, default=Path("/tmp/voicekey-asr-bench"))
    parser.add_argument("--variant", choices=("baseline", "parakeet-native", "parakeet-separate", "parakeet-shared", "parakeet-shared-nopack"), required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--limit", type=int, default=12)
    parser.add_argument("--repeat", type=int, default=1)
    parser.add_argument("--paced", action="store_true")
    parser.add_argument("--smoke", action="store_true")
    parser.add_argument("--live-threads", type=int, default=2)
    args = parser.parse_args()
    if args.variant == "baseline" and args.live_threads != 2:
        parser.error("baseline uses the production two-thread preview configuration")
    cfg = load()
    initial = memory()
    began = time.perf_counter()
    if args.variant in ("baseline", "parakeet-native"):
        model = NativePair(cfg, args.root, parakeet=args.variant == "parakeet-native", live_threads=args.live_threads)
    else:
        model = ParakeetPair(args.root / "shared", Path(cfg.backend.model_dir) / "tokens.txt",
                             shared=args.variant != "parakeet-separate", prepacking=args.variant != "parakeet-shared-nopack",
                             live_threads=args.live_threads)
    loaded_seconds = time.perf_counter() - began
    gc.collect()
    ctypes.CDLL(None).malloc_trim(0)
    after_load = memory()
    manifest = json.loads((args.root / "manifest.json").read_text())[:args.limit]
    if args.smoke:
        manifest = [{"id": "packaged-parakeet", "path": str(Path(cfg.backend.model_dir) / "test_wavs/0.wav"), "reference": ""}]
    print(json.dumps({"variant": args.variant, "load_seconds": loaded_seconds, "memory": after_load}), flush=True)
    # Warm all three neural stages before measuring any scored clip.
    warm = read_audio(manifest[0]["path"])
    run_clip(model, warm[:min(len(warm), 32000)])
    time.sleep(.15)
    results = []
    for repeat in range(args.repeat):
        for row in manifest:
            result = run_clip(model, read_audio(row["path"]), paced=args.paced)
            result.update(id=row["id"], repeat=repeat, reference=row["reference"],
                          preview_wer=errors(row["reference"], result["preview"]),
                          final_wer=errors(row["reference"], result["final"]))
            results.append(result)
            print(json.dumps({k:v for k,v in result.items() if k not in ("updates", "memory")}), flush=True)
    report = {"variant": args.variant, "paced": args.paced, "smoke": args.smoke,
              "live_threads": args.live_threads, "load_seconds": loaded_seconds,
              "initial_memory": initial, "loaded_memory": after_load, "results": results,
              "versions": {"python": sys.version.split()[0], "sherpa": sherpa_onnx.__version__, "numpy": np.__version__,
                           "native_ort": sherpa_onnx.lib._sherpa_onnx.onnxruntime_version,
                           "python_ort": onnxruntime.__version__},
              "affinity": sorted(os.sched_getaffinity(0))}
    if getattr(model, "bank", None):
        report["shared_initializer_bytes"] = model.bank.index["shared_encoder_bytes"]
        report["borrowed_weight_buffers"] = len(model.bank.values)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
