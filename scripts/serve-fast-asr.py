"""Serve a persistent CPU Zipformer recognizer with live PCM16 input and a file API.

Call stack: local WebSocket or WAV -> one utterance stream -> shared ONNX CPU model.
The service keeps no audio files, transcripts, or user request logs.
"""

import argparse
import hashlib
import io
import json
import re
import threading
import time
from email import policy
from email.parser import BytesParser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

import numpy as np
import sherpa_onnx
import soundfile as sf
from websockets.exceptions import ConnectionClosed
from websockets.sync.server import serve


MODEL_ID = "zipformer-small-zh-en-int8"
MODEL_HASHES = {
    "encoder-epoch-99-avg-1.int8.onnx": "db6f51551762e40e549166fe041ea3e45464370b595e9ad23f06478ec3794fbb",
    "decoder-epoch-99-avg-1.onnx": "89be509a83175261695bdef5fd1c7b9ab1129a663d1284e7ba9f8507b21e0906",
    "joiner-epoch-99-avg-1.int8.onnx": "bdda356d6f9b8c2d7cee9ee0e26075fa537490f7fd06520be408d287073667b9",
    "tokens.txt": "a8e0e4ec53810e433789b54a5c0134a7eaa2ffca595a6334d54c00da858841d3",
}


def local_origin(origin):
    if not origin:
        return True
    try:
        parsed = urlsplit(origin)
        return parsed.scheme in ("http", "https") and parsed.hostname in ("127.0.0.1", "localhost", "::1") and parsed.port != 0 and not parsed.username and not parsed.password
    except ValueError:
        return False


class Recognizer:
    """The model is loaded once. Utterance decoder state belongs to each new stream."""

    def __init__(self, directory, threads):
        self.hashes = {name: hashlib.sha256((directory / name).read_bytes()).hexdigest() for name in MODEL_HASHES}
        if self.hashes != MODEL_HASHES:
            raise ValueError("ASR checkpoint SHA-256 does not match the pinned official model")
        self.model = sherpa_onnx.OnlineRecognizer.from_transducer(
            tokens=str(directory / "tokens.txt"),
            encoder=str(directory / "encoder-epoch-99-avg-1.int8.onnx"),
            decoder=str(directory / "decoder-epoch-99-avg-1.onnx"),
            joiner=str(directory / "joiner-epoch-99-avg-1.int8.onnx"),
            num_threads=threads, provider="cpu", sample_rate=16000, feature_dim=80,
            decoding_method="greedy_search", enable_endpoint_detection=False,
        )
        self.lock = threading.Lock()

    def decode(self, stream, samples, rate=16000, final=False):
        started = time.perf_counter()
        with self.lock:
            stream.accept_waveform(rate, samples)
            if final:
                stream.accept_waveform(rate, np.zeros(round(rate * 0.3), dtype=np.float32))
                stream.input_finished()
            while self.model.is_ready(stream):
                self.model.decode_stream(stream)
            text = self.model.get_result(stream)
        return text, round((time.perf_counter() - started) * 1000, 3)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model-dir", type=Path, required=True)
    parser.add_argument("--port", type=int, default=18086)
    parser.add_argument("--ws-port", type=int, default=18087)
    parser.add_argument("--threads", type=int, default=2)
    args = parser.parse_args()
    runtime = Recognizer(args.model_dir, args.threads)

    def websocket(connection):
        if connection.request.path != "/v1/stream" or not local_origin(connection.request.headers.get("Origin")):
            connection.close(1008, "Local voice input endpoint required")
            return
        try:
            start = json.loads(connection.recv(timeout=5))
            if start != {"type": "start", "model": MODEL_ID, "sample_rate": 16000}:
                raise ValueError("Expected start control with the selected model and 16000 Hz PCM16")
            stream = runtime.model.create_stream()
            previous = ""
            total = 0
            inference_ms = 0
            for event in connection:
                if isinstance(event, bytes):
                    if not event or len(event) % 2:
                        raise ValueError("PCM16 chunks require a non-empty even byte count")
                    total += len(event)
                    if total > 16000 * 2 * 120:
                        raise ValueError("Voice input exceeds the 120-second turn limit")
                    samples = np.frombuffer(event, dtype="<i2").astype(np.float32) / 32768
                    text, elapsed = runtime.decode(stream, samples)
                    inference_ms += elapsed
                    if text != previous:
                        connection.send(json.dumps({"type": "partial", "text": text}))
                        previous = text
                elif json.loads(event) == {"type": "finish"}:
                    text, elapsed = runtime.decode(stream, np.empty(0, dtype=np.float32), final=True)
                    connection.send(json.dumps({"type": "final", "text": text, "audio_seconds": total / 32000, "inference_ms": round(inference_ms + elapsed, 3), "finalize_ms": elapsed}))
                    return
                else:
                    raise ValueError("Expected PCM16 data or finish control")
        except ConnectionClosed:
            return
        except (ValueError, TimeoutError):
            connection.send(json.dumps({"type": "error", "message": "Invalid local streaming transcription request"}))

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def respond(self, status, body):
            data = json.dumps(body).encode()
            self.send_response(status)
            origin = self.headers.get("Origin")
            if origin and local_origin(origin):
                self.send_header("Access-Control-Allow-Origin", origin)
                self.send_header("Vary", "Origin")
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)

        def do_OPTIONS(self):
            if not local_origin(self.headers.get("Origin")):
                return self.respond(403, {"error": "Only local origins are allowed"})
            self.send_response(204)
            if self.headers.get("Origin"):
                self.send_header("Access-Control-Allow-Origin", self.headers["Origin"])
                self.send_header("Vary", "Origin")
            self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
            self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization")
            self.end_headers()

        def do_GET(self):
            if not local_origin(self.headers.get("Origin")):
                return self.respond(403, {"error": "Only local origins are allowed"})
            if self.path == "/health":
                return self.respond(200, {"status": "ready", "model": MODEL_ID, "provider": "cpu", "streaming": True, "ws_port": args.ws_port, "hashes": runtime.hashes, "audio_retention": "none"})
            if self.path == "/v1/models":
                return self.respond(200, {"object": "list", "data": [{"id": MODEL_ID, "object": "model", "owned_by": "local"}]})
            self.respond(404, {"error": "Not found"})

        def do_POST(self):
            if not local_origin(self.headers.get("Origin")):
                return self.respond(403, {"error": "Only local origins are allowed"})
            if self.path != "/v1/audio/transcriptions":
                return self.respond(404, {"error": "Not found"})
            try:
                size = int(self.headers.get("Content-Length", "0"))
                if not 0 < size <= 8 * 1024 * 1024:
                    raise ValueError("Invalid audio size")
                self.connection.settimeout(5)
                content = f'Content-Type: {self.headers.get("Content-Type", "")}\r\nMIME-Version: 1.0\r\n\r\n'.encode() + self.rfile.read(size)
                message = BytesParser(policy=policy.default).parsebytes(content)
                parts = {part.get_param("name", header="content-disposition"): part.get_payload(decode=True) for part in message.iter_parts()}
                if parts.get("model", b"").decode() != MODEL_ID or "file" not in parts:
                    raise ValueError("Expected the selected model and audio file")
                samples, rate = sf.read(io.BytesIO(parts["file"]), dtype="float32", always_2d=True)
                if samples.shape[0] > rate * 120 or not np.isfinite(samples).all():
                    raise ValueError("Invalid audio duration or samples")
                text, elapsed = runtime.decode(runtime.model.create_stream(), samples.mean(axis=1), rate, final=True)
                self.respond(200, {"text": text, "model": MODEL_ID, "inference_ms": elapsed})
            except Exception:
                self.respond(400, {"error": "Invalid local transcription request"})

    origins = [None, re.compile(r"^https?://(?:localhost|127\.0\.0\.1|\[::1\])(?::[0-9]{1,5})?$")]
    with serve(websocket, "127.0.0.1", args.ws_port, origins=origins, compression=None, max_size=262144) as socket_server:
        threading.Thread(target=socket_server.serve_forever, daemon=True).start()
        print(json.dumps({"status": "ready", "model": MODEL_ID, "http_port": args.port, "ws_port": args.ws_port, "provider": "cpu"}), flush=True)
        ThreadingHTTPServer(("127.0.0.1", args.port), Handler).serve_forever()


if __name__ == "__main__":
    main()
