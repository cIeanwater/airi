"""Serve audio-native turn completion on loopback CPU without recording user audio.

Call stack: HTTP PCM -> SmartTurn.predict -> Whisper log-mel -> ONNX CPU.
Install: python -m pip install numpy==2.2.6 scipy==1.15.3 onnxruntime==1.23.2
Run: python scripts/serve-smart-turn.py --model PATH/smart-turn-v3.2-cpu.onnx
"""

import argparse
import hashlib
import json
import math
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import numpy as np
import onnxruntime as ort
from scipy.signal import resample_poly


def log_mel(audio):
    """Match the public Smart Turn Whisper Tiny feature contract, 80 x 800."""
    # Whisper uses Slaney mel spacing and area-normalized triangular filters.
    def to_mel(hz):
        hz = np.asarray(hz, dtype=np.float64)
        return np.where(hz >= 1000, 15 + np.log(np.maximum(hz, 1) / 1000) * 27 / np.log(6.4), 3 * hz / 200)

    mels = np.linspace(to_mel(0), to_mel(8000), 82)
    frequencies = np.where(mels >= 15, 1000 * np.exp((mels - 15) * np.log(6.4) / 27), 200 * mels / 3)
    slopes = frequencies[None, :] - np.linspace(0, 8000, 201)[:, None]
    differences = np.diff(frequencies)
    filters = np.maximum(0, np.minimum(-slopes[:, :-2] / differences[:-1], slopes[:, 2:] / differences[1:]))
    filters *= 2 / (frequencies[2:] - frequencies[:-2])
    normalized = (audio - audio.mean()) / np.sqrt(audio.var() + 1e-7)
    frames = np.lib.stride_tricks.sliding_window_view(np.pad(normalized.astype(np.float64), (200, 200), mode="reflect"), 400)[::160]
    power = np.abs(np.fft.rfft(frames * np.hanning(401)[:-1], axis=-1)) ** 2
    result = np.log10(np.maximum(1e-10, filters.T @ power.T))[:, :-1]
    return ((np.maximum(result, result.max() - 8) + 4) / 4).astype(np.float32)


class SmartTurn:
    """Owns a single bounded CPU session. No audio or transcript reaches disk."""

    def __init__(self, model, threads):
        self.sha256 = hashlib.sha256(model.read_bytes()).hexdigest()
        if self.sha256 != "2bb026316b14a660486a75b1733cd3fbab8c2fd0314dc9af7be49f8cca967e4f":
            raise ValueError("Model SHA-256 does not match the supported official Smart Turn v3.2 CPU checkpoint")
        options = ort.SessionOptions()
        options.intra_op_num_threads = threads
        options.inter_op_num_threads = 1
        options.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
        self.session = ort.InferenceSession(str(model), sess_options=options, providers=["CPUExecutionProvider"])
        self.lock = threading.Lock()

    def predict(self, audio, sample_rate):
        started = time.perf_counter()
        if not audio.size or not np.isfinite(audio).all() or np.max(np.abs(audio)) > 1.001:
            raise ValueError("Audio must contain finite normalized float32 samples")
        if audio.size > sample_rate * 8:
            raise ValueError("Send at most eight seconds of the current turn")
        if np.max(np.abs(audio)) < 1e-6:
            return {"complete": False, "probability": 0.0, "reason": "silence", "inference_ms": 0}
        if sample_rate != 16000:
            divisor = math.gcd(sample_rate, 16000)
            audio = resample_poly(audio, 16000 // divisor, sample_rate // divisor).astype(np.float32)
        audio = audio[-128000:]
        audio = np.pad(audio, (128000 - audio.size, 0))
        with self.lock:
            features = log_mel(audio)[None, :, :]
            probability = float(self.session.run(None, {"input_features": features})[0].reshape(-1)[0])
        return {"complete": probability > 0.5, "probability": probability, "inference_ms": round((time.perf_counter() - started) * 1000, 2)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", type=Path, required=True)
    parser.add_argument("--port", type=int, default=18085)
    parser.add_argument("--threads", type=int, default=2)
    args = parser.parse_args()
    model = SmartTurn(args.model, args.threads)

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def allowed(self):
            origin = self.headers.get("Origin")
            if not origin:
                return True
            parsed = urlsplit(origin)
            try:
                port = parsed.port
            except ValueError:
                return False
            return parsed.scheme in ("http", "https") and parsed.hostname in ("localhost", "127.0.0.1", "::1") and not parsed.username and not parsed.password and port != 0

        def send_json(self, status, body):
            data = json.dumps(body).encode()
            self.send_response(status)
            if self.allowed() and self.headers.get("Origin"):
                self.send_header("Access-Control-Allow-Origin", self.headers["Origin"])
                self.send_header("Vary", "Origin")
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)

        def do_OPTIONS(self):
            if not self.allowed():
                return self.send_json(403, {"error": "Only local origins are allowed"})
            self.send_response(204)
            if self.headers.get("Origin"):
                self.send_header("Access-Control-Allow-Origin", self.headers["Origin"])
                self.send_header("Vary", "Origin")
            self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
            self.send_header("Access-Control-Allow-Headers", "Content-Type")
            self.end_headers()

        def do_GET(self):
            if not self.allowed():
                return self.send_json(403, {"error": "Only local origins are allowed"})
            if urlsplit(self.path).path != "/health":
                return self.send_json(404, {"error": "Not found"})
            self.send_json(200, {"status": "ready", "model": "smart-turn-v3.2-cpu", "sha256": model.sha256, "provider": "CPUExecutionProvider", "audio_retention": "none"})

        def do_POST(self):
            if not self.allowed():
                return self.send_json(403, {"error": "Only local origins are allowed"})
            path = urlsplit(self.path)
            if path.path != "/v1/turn":
                return self.send_json(404, {"error": "Not found"})
            try:
                sample_rate = int(parse_qs(path.query).get("sample_rate", ["16000"])[0])
                length = int(self.headers.get("Content-Length", "0"))
                if not 8000 <= sample_rate <= 96000 or not 0 < length <= sample_rate * 8 * 4 or length % 4:
                    raise ValueError("Expected up to eight seconds of float32 mono PCM at 8000-96000 Hz")
                if self.headers.get("Content-Type", "").split(";")[0] != "application/octet-stream":
                    raise ValueError("Expected application/octet-stream float32 little-endian PCM")
                self.connection.settimeout(3)
                data = self.rfile.read(length)
                if len(data) != length:
                    raise ValueError("Incomplete audio body")
                result = model.predict(np.frombuffer(data, dtype="<f4"), sample_rate)
                self.send_json(200, {"model": "smart-turn-v3.2-cpu", **result})
            except (ValueError, TimeoutError) as error:
                self.send_json(400, {"error": str(error)})
            except Exception:
                self.send_json(500, {"error": "Turn inference failed"})

    server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    print(json.dumps({"status": "ready", "port": args.port, "model": "smart-turn-v3.2-cpu", "sha256": model.sha256}), flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
