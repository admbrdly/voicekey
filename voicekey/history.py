"""Read the existing journal without loading models or retrying delivery."""
from __future__ import annotations

from .recovery import Journal


def latest(journal: Journal) -> dict:
    """Latest prepared, nonempty dictation in capture order (agent prompts excluded)."""
    found = None
    for path in journal.directory.glob("*.jsonl"):
        try:
            records, damaged = journal._recovery_records(path)
        except FileNotFoundError:
            continue  # retention may remove an old entry during this read
        merged = {}
        for record in records:
            merged.update(record)
        if (merged.get("draft_part") or merged.get("action", "dictate") != "dictate"
                or any(r["event"] == "agent-attempt" for r in records)
                or not isinstance(merged.get("final"), str) or not merged["final"].strip()):
            continue
        captured = next((r.get("time", 0) for r in records if r["event"] == "captured"), 0)
        if merged.get('draft'):
            captured = max((r.get('time', 0) for r in records if r['event'] in ('draft-update', 'final')), default=0)
        candidate = {**merged, "captured": captured, "path": str(path), "damaged": damaged}
        if found is None or captured > found["captured"]:
            found = candidate
    if found is None:
        raise LookupError("no prepared dictation in the retained journal")
    return found


def explain(record: dict) -> str:
    labels = (
        ("id", "Dictation"), ("backend", "Backend"), ("model", "Model"),
        ("live", "Live preview"), ("raw", "Raw transcript"),
        ("polished", "After polish"), ("polish_result", "Polish"),
        ("polish_style", "Style"), ("overridden", "After word overrides"),
        ("hook_result", "Hook"), ("final", "Prepared text"),
        ("failure", "Recognition/capture warning"),
        ("outcome", "Delivery"), ("reason", "Delivery reason"), ("path", "Journal"),
    )
    result = [f"{label}: {record[key]}" for key, label in labels if record.get(key)]
    if not record.get("outcome"):
        result.append("Delivery: pending or interrupted; no recorded outcome")
    if record.get("damaged"):
        result.append("Warning: journal has an incomplete or damaged record; inspect before repeating delivery")
    return "\n".join(result)


def show(*, copy=False, diagnostic=False) -> int:
    import sys
    import subprocess
    from .inject import InjectError

    try:
        record = latest(Journal())
        if record.get("damaged"):
            print("voicekey: journal incomplete; using the last complete prepared text", file=sys.stderr)
        if copy:
            from .inject import copy as copy_text
            copy_text(record["final"])
        elif diagnostic:
            print(explain(record))
        else:
            sys.stdout.write(record["final"])
        return 0
    except (OSError, LookupError, RuntimeError, InjectError, subprocess.TimeoutExpired) as exc:
        print(f"voicekey: {exc}", file=sys.stderr)
        return 1
