"""Run secondary controls serially, each in a fresh process."""
import json
from pathlib import Path
import subprocess
import sys

ROOT = Path("/tmp/voicekey-asr-bench")
SCRIPT = Path(__file__).with_name("asr_benchmark.py")

jobs = [
    ("prototype-separate", "parakeet-separate", ["--limit", "2"]),
    ("prototype-shared", "parakeet-shared", ["--limit", "2"]),
    ("prototype-shared-nopack", "parakeet-shared-nopack", ["--limit", "2"]),
    ("native-thread1", "parakeet-native", ["--smoke", "--live-threads", "1"]),
    ("native-thread4", "parakeet-native", ["--smoke", "--live-threads", "4"]),
    ("native-thread8", "parakeet-native", ["--smoke", "--live-threads", "8"]),
    ("paced-baseline", "baseline", ["--smoke", "--paced"]),
    ("paced-parakeet", "parakeet-native", ["--smoke", "--paced"]),
    ("paced-shared", "parakeet-shared-nopack", ["--smoke", "--paced"]),
]

for name, variant, flags in jobs:
    command = [sys.executable, str(SCRIPT), "--variant", variant, "--output", str(ROOT / (name + ".json")), *flags]
    print("Running", name, flush=True)
    with (ROOT / (name + ".log")).open("w") as log:
        process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        for line in process.stdout:
            log.write(line)
            log.flush()
            try:
                row = json.loads(line)
            except ValueError:
                continue
            if "stream_rtf" in row:
                print(json.dumps({"run": name, "clip": row["id"], "stream_rtf": row["stream_rtf"],
                                  "final_seconds": row["final_seconds"]}), flush=True)
        if process.wait() != 0:
            raise RuntimeError(f"{name} failed; see {ROOT / (name + '.log')}")
