"""Archive exact measurements and produce compact, auditable aggregates."""
import argparse
import hashlib
import json
from pathlib import Path
import shutil
import statistics
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from voicekey.backends import MODELS
from voicekey.config import load

RUNS = ["baseline", "parakeet-native", "prototype-separate", "prototype-shared",
        "prototype-shared-nopack", "native-smoke", "native-thread1", "native-thread4",
        "native-thread8", "paced-baseline", "paced-parakeet", "paced-shared"]


def aggregate(report):
    rows = report["results"]
    audio = sum(r["audio_seconds"] for r in rows)
    reference = sum(r["final_wer"]["reference_words"] for r in rows)
    stream = sum(r["stream_compute_seconds"] for r in rows)
    return {"clips": len(rows), "audio_seconds": audio, "stream_compute_seconds": stream,
            "stream_rtf": stream / audio,
            "stream_cpu_seconds": sum(r["stream_cpu_seconds"] for r in rows),
            "first_preview_median_seconds": statistics.median(r["first_preview_seconds"] for r in rows),
            "final_median_seconds": statistics.median(r["final_seconds"] for r in rows),
            "reference_words": reference,
            "preview_errors": sum(r["preview_wer"]["errors"] for r in rows) if reference else None,
            "final_errors": sum(r["final_wer"]["errors"] for r in rows) if reference else None,
            "max_warm_pss_MiB": max(r["memory"]["Pss_MiB"] for r in rows),
            "max_warm_rss_MiB": max(r["memory"]["Rss_MiB"] for r in rows),
            "max_swap_MiB": max(r["memory"]["Swap_MiB"] for r in rows)}


def sha256(path):
    h = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def collect(root, destination):
    destination.mkdir(parents=True, exist_ok=True)
    reports = {}
    for name in RUNS:
        path = root / (name + ".json")
        reports[name] = json.loads(path.read_text())
        shutil.copy2(path, destination / path.name)
    for name in ("manifest.json", "inputs.json"):
        shutil.copy2(root / name, destination / name)
    weights = json.loads((root / "shared/index.json").read_text())
    weight_summary = {k: v for k, v in weights.items() if k not in ("graphs", "bank")}
    (destination / "sharing.json").write_text(json.dumps(weight_summary, indent=2))
    by_id = lambda name: {row["id"]: row for row in reports[name]["results"]}
    native = by_id("parakeet-native")
    baseline = by_id("baseline")
    assert native.keys() == baseline.keys()
    assert all(native[i]["final"] == baseline[i]["final"] for i in native), "native final parity changed"
    separate = by_id("prototype-separate")
    for name in ("prototype-shared", "prototype-shared-nopack"):
        shared = by_id(name)
        assert shared.keys() == separate.keys()
        assert all(shared[i][tier] == separate[i][tier] for i in shared for tier in ("preview", "final"))
    prototype_differences = [{"id": i, "tier": tier} for i in separate for tier in ("preview", "final")
                             if separate[i][tier] != native[i][tier]]
    cfg = load()
    model_hashes = {}
    for folder in (Path(cfg.backend.model_dir), Path(cfg.streaming.model_dir)):
        hashes = {name: sha256(folder / name) for name in MODELS[folder.name]}
        assert hashes == MODELS[folder.name], "installed model differs from the checked model"
        model_hashes[folder.name] = hashes
    summary = {"hardware": {"cpu": "13th Gen Intel(R) Core(TM) i5-13600", "cores": 14,
                             "logical_cpus": 20, "installed_RAM_GB": 32, "provider": "CPU"},
               "runs": {name: aggregate(report) for name, report in reports.items()},
               "native_final_transcripts_identical": True,
               "prototype_sharing_transcripts_identical": True,
               "prototype_vs_native_differences": prototype_differences,
               "model_hashes": model_hashes, "sharing": weight_summary}
    (destination / "summary.json").write_text(json.dumps(summary, indent=2))
    for name, result in summary["runs"].items():
        print(name, json.dumps(result))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, default=Path("/tmp/voicekey-asr-bench"))
    parser.add_argument("--output", type=Path, default=Path("benchmarks/results/2026-09-06"))
    args = parser.parse_args()
    collect(args.root, args.output)
