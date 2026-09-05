"""Experimental Parakeet runtime for measuring real initializer sharing.

The two encoders keep distinct computation graphs and per-stream state, but
borrow byte-identical weights from one explicitly owned bank. Decoder and
joiner sessions are each instantiated once. This is not the production backend.
"""
import json
import mmap
from pathlib import Path

import kaldi_native_fbank as knf
import numpy as np
import onnxruntime as ort


def normalize(features):
    mean = features.mean(axis=0, keepdims=True)
    centered = features - mean
    variance = (centered * centered).mean(axis=0, keepdims=True)
    return centered * (1.0 / (np.sqrt(variance) + np.float32(1e-5)))


def fbank(*, streaming):
    options = knf.FbankOptions()
    options.frame_opts.dither = 0
    options.frame_opts.snip_edges = False
    options.frame_opts.remove_dc_offset = False
    options.frame_opts.window_type = "hann" if streaming else "povey"
    options.mel_opts.low_freq = 0
    options.mel_opts.high_freq = 8000 if streaming else -400
    options.mel_opts.num_bins = 128
    options.mel_opts.is_librosa = True
    return knf.OnlineFbank(options)


class WeightBank:
    def __init__(self, root):
        self.root = Path(root)
        self.index = json.loads((self.root / "index.json").read_text())
        self.file = (self.root / "weights.bin").open("rb")
        self.mapping = mmap.mmap(self.file.fileno(), 0, access=mmap.ACCESS_READ)
        self.values = {}
        self.pointers = {}

    def lend(self, options, graph):
        for entry in self.index["graphs"][graph]["initializers"]:
            key = entry["key"]
            if key not in self.values:
                item = self.index["bank"][key]
                array = np.ndarray(item["shape"], dtype=np.dtype(item["dtype"]),
                                   buffer=self.mapping, offset=item["offset"])
                value = ort.OrtValue.ortvalue_from_numpy(array)
                assert value.data_ptr() == array.ctypes.data, "weight buffer was copied"
                self.values[key] = value
            value = self.values[key]
            options.add_initializer(entry["name"], value)
            self.pointers[(graph, entry["name"])] = value.data_ptr()


class ParakeetPair:
    def __init__(self, root, tokens, *, shared=True, prepacking=True, live_threads=2, final_threads=4):
        root = Path(root)
        self.bank = WeightBank(root) if shared else None
        self.sessions = {}
        for name in ("offline_encoder", "streaming_encoder", "decoder", "joiner"):
            options = ort.SessionOptions()
            options.intra_op_num_threads = final_threads if name == "offline_encoder" else live_threads
            options.inter_op_num_threads = options.intra_op_num_threads
            options.log_severity_level = 3
            if not prepacking:
                options.add_session_config_entry("session.disable_prepacking", "1")
            if self.bank:
                self.bank.lend(options, name)
            self.sessions[name] = ort.InferenceSession(str(root / (name + ".onnx")), options,
                                                      providers=["CPUExecutionProvider"])
        self.offline_encoder = self.sessions["offline_encoder"]
        self.streaming_encoder = self.sessions["streaming_encoder"]
        self.decoder = self.sessions["decoder"]
        self.joiner = self.sessions["joiner"]
        self.names = {k: [i.name for i in s.get_inputs()] for k, s in self.sessions.items()}
        self.meta = self.streaming_encoder.get_modelmeta().custom_metadata_map
        self.tokens = {}
        for line in Path(tokens).read_text().splitlines():
            token, identity = line.rsplit(" ", 1)
            self.tokens[int(identity)] = token
        self.blank = max(self.tokens)

    def run(self, name, *inputs):
        return self.sessions[name].run(None, dict(zip(self.names[name], inputs)))

    def encode(self, features, *, streaming):
        features = np.ascontiguousarray(normalize(features).T[None, :, :])
        length = np.array([features.shape[2]], dtype=np.int64)
        return self.run("streaming_encoder" if streaming else "offline_encoder", features, length)

    def text(self, tokens):
        return "".join(self.tokens[i] for i in tokens).replace("▁", " ").strip()

    def transcribe(self, samples):
        features = fbank(streaming=False)
        features.accept_waveform(16000, samples)
        features.input_finished()
        matrix = np.stack([features.get_frame(i) for i in range(features.num_frames_ready)])
        encoded, length = self.encode(matrix, streaming=False)
        decoder = Decoder(self)
        decoder.feed(encoded[:, :, :int(length[0])])
        return self.text(decoder.tokens)

    def session(self):
        return Stream(self)


class Decoder:
    def __init__(self, model):
        self.model = model
        self.tokens = []
        shape = (int(model.meta["pred_rnn_layers"]), 1, int(model.meta["pred_hidden"]))
        self.states = [np.zeros(shape, dtype=np.float32), np.zeros(shape, dtype=np.float32)]
        self._token(model.blank)

    def _token(self, token):
        output = self.model.run("decoder", np.array([[token]], dtype=np.int32),
                                np.array([1], dtype=np.int32), *self.states)
        self.value, self.states = output[0], output[2:]

    def feed(self, encoded):
        for t in range(encoded.shape[2]):
            frame = np.ascontiguousarray(encoded[:, :, t:t + 1])
            for _ in range(10):
                logits = self.model.run("joiner", frame, self.value)[0]
                token = int(np.argmax(logits))
                if token == self.model.blank:
                    break
                self.tokens.append(token)
                self._token(token)


class Stream:
    def __init__(self, model):
        self.model = model
        self.features = fbank(streaming=True)
        self.decoder = Decoder(model)
        self.processed = 0
        self.left = int(model.meta["left_feature_frames"])
        self.chunk = int(model.meta["chunk_feature_frames"])
        self.right = int(model.meta["right_feature_frames"])
        self.left_encoded = int(model.meta["left_encoder_frames"])
        self.factor = int(model.meta["subsampling_factor"])

    def feed(self, samples):
        self.features.accept_waveform(16000, samples)
        return self._decode(False)

    def finish(self):
        self.features.input_finished()
        return self._decode(True)

    def _decode(self, finished):
        ready = self.features.num_frames_ready
        while self.processed + self.chunk + self.right <= ready or finished and self.processed < ready:
            start = self.processed - self.left
            end = self.processed + self.chunk + self.right
            matrix = np.zeros((self.left + self.chunk + self.right, 128), dtype=np.float32)
            for i in range(max(0, start), min(ready, end)):
                matrix[i - start] = self.features.get_frame(i)
            encoded = self.model.encode(matrix, streaming=True)[0]
            valid = min(self.chunk, ready - self.processed)
            count = (valid + self.factor - 1) // self.factor
            self.decoder.feed(encoded[:, :, self.left_encoded:self.left_encoded + count])
            self.processed += valid
        return self.model.text(self.decoder.tokens)
