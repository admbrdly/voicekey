"""Download isolated ASR benchmark inputs; never modify the live installation."""
import argparse
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
from pathlib import Path
import random
import shutil
import subprocess
import tarfile
import urllib.request
import wave

MODEL_NAME = "sherpa-onnx-nemo-parakeet-unified-en-0.6b-int8-streaming-560ms"
MODEL_URL = "https://github.com/k2-fsa/sherpa-onnx/releases/download/asr-models/" + MODEL_NAME + ".tar.bz2"
MODEL_SHA256 = "dd2c2698f102eafbf0ee54bdfd7cd842ec00fa6cf2475cbbb048887f794ff52e"
CORPUS_BASE = "https://www.openslr.org/resources/12/"


def digest(path, algorithm="sha256"):
    h = hashlib.new(algorithm)
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def download(url, path):
    if path.exists():
        return
    print("Downloading", path.name, flush=True)
    temporary = path.with_suffix(path.suffix + ".partial")
    with urllib.request.urlopen(url, timeout=45) as response, temporary.open("wb") as output:
        shutil.copyfileobj(response, output, 1024 * 1024)
    temporary.replace(path)
    print("Downloaded", path.name, path.stat().st_size, flush=True)


def prepare(root):
    root.mkdir(parents=True, exist_ok=True)
    model = root / (MODEL_NAME + ".tar.bz2")
    corpus = root / "test-clean.tar.gz"
    with ThreadPoolExecutor(max_workers=2) as pool:
        tasks = [pool.submit(download, MODEL_URL, model),
                 pool.submit(download, CORPUS_BASE + corpus.name, corpus)]
        for task in tasks:
            task.result()
    assert digest(model) == MODEL_SHA256, "model checksum mismatch"
    with urllib.request.urlopen(CORPUS_BASE + "md5sum.txt", timeout=30) as response:
        checksums = response.read().decode()
    expected = next(line.split()[0] for line in checksums.splitlines() if line.endswith(corpus.name))
    assert digest(corpus, "md5") == expected, "corpus checksum mismatch"
    if not (root / MODEL_NAME).exists():
        print("Extracting streaming model", flush=True)
        with tarfile.open(model) as archive:
            archive.extractall(root, filter="data")

    # Twelve distinct speakers, with alternating shorter/longer reference text.
    # Selection depends only on the published references, never model outputs.
    references = {}
    with tarfile.open(corpus) as archive:
        members = archive.getmembers()
        for member in members:
            if member.name.endswith(".trans.txt"):
                for line in archive.extractfile(member).read().decode().splitlines():
                    identity, text = line.split(" ", 1)
                    references[identity] = text
        speakers = sorted({identity.split("-")[0] for identity in references})
        rng = random.Random(20260906)
        rng.shuffle(speakers)
        selected = []
        for speaker in speakers:
            low, high = (20, 35) if len(selected) % 2 == 0 else (45, 70)
            choices = sorted(identity for identity, text in references.items()
                             if identity.split("-")[0] == speaker and low <= len(text.split()) <= high)
            if choices:
                selected.append(rng.choice(choices))
            if len(selected) == 12:
                break
        output = root / "corpus"
        output.mkdir(exist_ok=True)
        manifest = []
        for identity in selected:
            speaker, chapter, _ = identity.split("-")
            member = archive.getmember(f"LibriSpeech/test-clean/{speaker}/{chapter}/{identity}.flac")
            flac = output / (identity + ".flac")
            flac.write_bytes(archive.extractfile(member).read())
            wav = flac.with_suffix(".wav")
            subprocess.run(["ffmpeg", "-nostdin", "-loglevel", "error", "-y", "-i", str(flac),
                            "-ar", "16000", "-ac", "1", str(wav)], check=True)
            with wave.open(str(wav)) as audio:
                seconds = audio.getnframes() / audio.getframerate()
            manifest.append({"id": identity, "speaker": speaker, "path": str(wav),
                             "reference": references[identity], "seconds": seconds, "sha256": digest(wav)})
        (root / "manifest.json").write_text(json.dumps(manifest, indent=2))
        print("Corpus:", len(manifest), "clips;", round(sum(x["seconds"] for x in manifest), 2), "seconds", flush=True)

    # Source references used to validate the prototype against the installed release.
    paths = ["sherpa-onnx/csrc/online-recognizer-transducer-nemo-parakeet-unified-impl.h",
             "sherpa-onnx/csrc/online-transducer-greedy-search-nemo-parakeet-unified-decoder.cc",
             "sherpa-onnx/csrc/offline-transducer-greedy-search-nemo-decoder.cc",
             "sherpa-onnx/csrc/offline-recognizer-transducer-nemo-impl.h",
             "sherpa-onnx/csrc/features.cc", "sherpa-onnx/csrc/features.h",
             "sherpa-onnx/csrc/math.cc", "sherpa-onnx/csrc/session.cc",
             "scripts/nemo/parakeet-unified-en-0.6b/test_onnx_streaming.py"]
    source = root / "source"
    source.mkdir(exist_ok=True)
    for path in paths:
        download("https://raw.githubusercontent.com/k2-fsa/sherpa-onnx/v1.13.6/" + path, source / Path(path).name)
    (root / "inputs.json").write_text(json.dumps({"model_url": MODEL_URL, "model_sha256": MODEL_SHA256,
        "corpus_url": CORPUS_BASE + corpus.name, "corpus_md5": expected,
        "corpus_sha256": digest(corpus), "selection_seed": 20260906}, indent=2))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, default=Path("/tmp/voicekey-asr-bench"))
    prepare(parser.parse_args().root)
