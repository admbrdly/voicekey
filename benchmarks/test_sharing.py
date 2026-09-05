"""Small invariant checks; run with the optional benchmark dependencies."""
import tempfile
import unittest
from pathlib import Path

import numpy as np
import onnx
from onnx import helper, numpy_helper
import onnxruntime as ort

from share_weights import build
from parakeet_shared import WeightBank


class SharingTests(unittest.TestCase):
    def graph(self, path, weight):
        model = helper.make_model(helper.make_graph(
            [helper.make_node("MatMul", ["X", "W"], ["Y"])], "test",
            [helper.make_tensor_value_info("X", onnx.TensorProto.FLOAT, [1, 16])],
            [helper.make_tensor_value_info("Y", onnx.TensorProto.FLOAT, [1, 128])],
            [numpy_helper.from_array(weight, name="W")]), opset_imports=[helper.make_opsetid("", 17)])
        model.ir_version = 9
        onnx.save_model(model, path)

    def prepare(self, directory, *, different=False):
        root = Path(directory)
        offline, live = root / "offline", root / "live"
        offline.mkdir(); live.mkdir()
        weight = np.arange(2048, dtype=np.float32).reshape(16, 128)
        for name in ("encoder", "decoder", "joiner"):
            self.graph(offline / (name + ".int8.onnx"), weight)
            if name != "encoder":
                self.graph(live / (name + ".int8.onnx"), weight)
        self.graph(live / "encoder.int8.onnx", weight + (1 if different else 0))
        build(offline, live, root / "shared")
        return root / "shared", weight

    def test_identical_weights_have_one_owner_and_keep_outputs(self):
        with tempfile.TemporaryDirectory() as directory:
            root, weight = self.prepare(directory)
            bank = WeightBank(root)
            sessions = []
            for graph in ("offline_encoder", "streaming_encoder"):
                options = ort.SessionOptions()
                options.intra_op_num_threads = 1
                options.add_session_config_entry("session.disable_prepacking", "1")
                bank.lend(options, graph)
                sessions.append(ort.InferenceSession(str(root / (graph + ".onnx")), options))
            self.assertEqual(len(bank.values), 1)
            self.assertEqual(bank.pointers[("offline_encoder", "W")], bank.pointers[("streaming_encoder", "W")])
            expected = np.ones((1, 16), dtype=np.float32) @ weight
            for session in sessions:
                np.testing.assert_array_equal(session.run(None, {"X": np.ones((1, 16), dtype=np.float32)})[0], expected)
            bank.file.close()

    def test_nonidentical_weights_never_alias(self):
        with tempfile.TemporaryDirectory() as directory:
            root, _weight = self.prepare(directory, different=True)
            bank = WeightBank(root)
            bank.lend(ort.SessionOptions(), "offline_encoder")
            bank.lend(ort.SessionOptions(), "streaming_encoder")
            self.assertNotEqual(bank.pointers[("offline_encoder", "W")], bank.pointers[("streaming_encoder", "W")])
            self.assertEqual(bank.index["shared_encoder_bytes"], 0)
            bank.file.close()


if __name__ == "__main__":
    unittest.main()
