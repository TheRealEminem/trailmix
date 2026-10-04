"""Silero VAD (https://github.com/snakers4/silero-vad, MIT) in plain numpy: the probability that someone
is speaking in each 32 ms of 16 kHz audio.

The network is tiny (a fixed STFT, four small convolutions, one 128-unit LSTM; 1.2 MB of weights in
assets/, taken from the official ONNX export), so running it with numpy avoids shipping torch or onnxruntime. The convolutions are computed for
every chunk at once; only the LSTM steps through time. About 2 s per hour of audio on an M-series Mac.
"""
from functools import lru_cache
from pathlib import Path

import numpy as np

WEIGHTS = Path(__file__).with_name("assets") / "silero_vad_v6.npz"  # from silero_vad_16k_op15.onnx, v6.2
SR = 16000
CHUNK = 512    # samples per probability (32 ms)
CONTEXT = 64   # samples carried over from the previous chunk


def available() -> bool:
    return WEIGHTS.exists()


@lru_cache(maxsize=1)
def _weights() -> dict[str, np.ndarray]:
    with np.load(WEIGHTS) as data:
        return {name: data[name].astype(np.float32) for name in data.files}


def _conv1d(x: np.ndarray, weight: np.ndarray, bias: np.ndarray, stride: int) -> np.ndarray:
    """x: (batch, length, in); weight: (out, in, 3), padding 1. Returns (batch, out_length, out)."""
    batch, length, channels = x.shape
    padded = np.pad(x, ((0, 0), (1, 1), (0, 0)))
    positions = range(0, length, stride)
    patches = np.stack([padded[:, p:p + 3, :] for p in positions], axis=1)  # (batch, L', 3, in)
    kernel = weight.transpose(2, 1, 0).reshape(3 * channels, -1)            # (3*in, out)
    return patches.reshape(batch, len(positions), 3 * channels) @ kernel + bias


def probabilities(audio: np.ndarray) -> np.ndarray:
    """Speech probability for each 512-sample chunk of mono float32 16 kHz audio."""
    w = _weights()
    audio = np.asarray(audio, dtype=np.float32)
    if len(audio) % CHUNK:
        audio = np.pad(audio, (0, CHUNK - len(audio) % CHUNK))
    n = len(audio) // CHUNK
    if n == 0:
        return np.zeros(0, dtype=np.float32)
    padded = np.pad(audio, (CONTEXT, 0))
    # Each step sees its 512 samples plus the 64 before them, reflect-padded by 64 at the end.
    idx = np.arange(n)[:, None] * CHUNK + np.arange(CONTEXT + CHUNK)[None, :]
    x = padded[idx]                                                        # (n, 576)
    x = np.concatenate([x, x[:, -2:-CONTEXT - 2:-1]], axis=1)              # reflect pad -> (n, 640)

    # STFT as a convolution: 256-sample frames every 128 samples -> 4 frames, magnitude of 129 bins.
    frames = np.stack([x[:, i * 128:i * 128 + 256] for i in range(4)], axis=1)  # (n, 4, 256)
    spec = frames @ w["stft_conv.weight"][:, 0, :].T                       # (n, 4, 258)
    mag = np.sqrt(spec[..., :129] ** 2 + spec[..., 129:] ** 2)

    h = np.maximum(_conv1d(mag, w["conv1.weight"], w["conv1.bias"], 1), 0)
    h = np.maximum(_conv1d(h, w["conv2.weight"], w["conv2.bias"], 2), 0)
    h = np.maximum(_conv1d(h, w["conv3.weight"], w["conv3.bias"], 2), 0)
    h = np.maximum(_conv1d(h, w["conv4.weight"], w["conv4.bias"], 1), 0)   # (n, 1, 128)
    features = h[:, 0, :]

    # LSTM cell (gate order i, f, g, o): the input half for every step at once, then step through time.
    gates_in = features @ w["lstm_cell.weight_ih"].T + w["lstm_cell.bias_ih"] + w["lstm_cell.bias_hh"]
    w_hh = w["lstm_cell.weight_hh"].T
    hidden = np.zeros(128, dtype=np.float32)
    cell = np.zeros(128, dtype=np.float32)
    outputs = np.empty((n, 128), dtype=np.float32)
    for t in range(n):
        g = gates_in[t] + hidden @ w_hh
        i, f, c, o = g[:128], g[128:256], g[256:384], g[384:]
        cell = _sigmoid(f) * cell + _sigmoid(i) * np.tanh(c)
        hidden = _sigmoid(o) * np.tanh(cell)
        outputs[t] = hidden

    logits = np.maximum(outputs, 0) @ w["final_conv.weight"][0, :, 0] + w["final_conv.bias"][0]
    return _sigmoid(logits)


def _sigmoid(x):
    return 1.0 / (1.0 + np.exp(-x))
