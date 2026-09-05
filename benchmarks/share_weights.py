"""Make the two Parakeet computation graphs refer to one weight bank.

This is a benchmark artifact builder, not a model conversion for production.
It hashes the exact initializer bytes, dtype and shape; nonidentical tensors
never alias. The runtime can then lend the same OrtValue to both sessions.
"""
import argparse
import hashlib
import json
from pathlib import Path

import onnx
from onnx import numpy_helper, external_data_helper


def build(offline, streaming, destination):
    destination.mkdir(parents=True, exist_ok=True)
    for name in ("decoder.int8.onnx", "joiner.int8.onnx"):
        a, b = onnx.load(offline / name), onnx.load(streaming / name)
        if a.graph.SerializeToString() != b.graph.SerializeToString():
            raise ValueError(f"{name} differs between the two modes; cannot reuse one session")
        del a, b
    bank, graphs = {}, {}
    original_bytes = 0
    inline_bytes = 0
    with (destination / "weights.bin").open("wb") as weights:
        for label, path in (("offline_encoder", offline / "encoder.int8.onnx"),
                            ("streaming_encoder", streaming / "encoder.int8.onnx"),
                            ("decoder", offline / "decoder.int8.onnx"),
                            ("joiner", offline / "joiner.int8.onnx")):
            print("Reading", label, flush=True)
            model = onnx.load(path)
            initializers = []
            for tensor in model.graph.initializer:
                array = numpy_helper.to_array(tensor)
                data = array.tobytes()
                original_bytes += len(data)
                # ONNX shape inference needs small shape/axis constants inline.
                # Keeping these also avoids hundreds of negligible overrides.
                if len(data) <= 4096:
                    inline_bytes += len(data)
                    continue
                key = hashlib.sha256(str((array.dtype.str, array.shape)).encode() + data).hexdigest()
                if key not in bank:
                    padding = (-weights.tell()) % 64
                    weights.write(bytes(padding))
                    bank[key] = {"offset": weights.tell(), "length": len(data),
                                 "dtype": array.dtype.str, "shape": list(array.shape), "users": []}
                    weights.write(data)
                item = bank[key]
                item["users"].append(label)
                initializers.append({"name": tensor.name, "key": key})
                tensor.raw_data = data
                external_data_helper.set_external_data(tensor, "weights.bin", item["offset"], item["length"])
                for field in ("raw_data", "float_data", "int32_data", "int64_data", "double_data", "uint64_data", "string_data"):
                    tensor.ClearField(field)
            graph_path = destination / (label + ".onnx")
            onnx.save_model(model, graph_path)
            graphs[label] = {"path": graph_path.name, "initializers": initializers}
            del model, array, data
    shared = sum(item["length"] for item in bank.values()
                 if "offline_encoder" in item["users"] and "streaming_encoder" in item["users"])
    report = {"graphs": graphs, "bank": bank, "original_initializer_bytes": original_bytes,
              "unique_initializer_bytes": sum(item["length"] for item in bank.values()),
              "shared_encoder_bytes": shared, "unshared_inline_bytes": inline_bytes}
    (destination / "index.json").write_text(json.dumps(report, indent=2))
    print(json.dumps({k: v for k, v in report.items() if k not in ("graphs", "bank")}), flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--offline", type=Path, required=True)
    parser.add_argument("--streaming", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    build(args.offline, args.streaming, args.output)
