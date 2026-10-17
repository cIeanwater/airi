"""Serve local MOSS-TTS-Nano ONNX speech as WAV or incremental PCM.

Call stack:
  main -> official OrtCpuRuntime -> frame callback -> codec -> HTTP PCM chunks
"""

import argparse
import io
import json
import os
import select
import socket
import sys
import threading
import time
import uuid
import wave
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib.metadata import version
from pathlib import Path
from urllib.parse import urlsplit


MODEL_ID = "moss-tts-nano-100m"
BUILTIN_VOICES = ("Xiaoyu", "Yuewen", "Lingyu")
ROOT = Path(__file__).resolve().parents[1]


class SpeechCancelled(Exception):
    """The client left before this synthesis finished."""


def allows_origin(origin: str | None) -> bool:
    if origin is None:
        return True
    try:
        parsed = urlsplit(origin)
        parsed.port
    except ValueError:
        return False
    return (
        parsed.scheme in {"http", "https"}
        and parsed.hostname in {"localhost", "127.0.0.1", "::1"}
        and parsed.username is None and parsed.password is None
        and not parsed.path and not parsed.query and not parsed.fragment
        and not any(character.isspace() for character in origin)
    )


def parse_request(body: object) -> dict:
    if not isinstance(body, dict):
        raise ValueError("Expected a JSON object")
    if body.get("model") != MODEL_ID:
        raise ValueError(f"model must be {MODEL_ID}")
    text = body.get("input")
    if not isinstance(text, str) or not text.strip() or len(text) > 240:
        raise ValueError("input must contain 1 to 240 characters")
    if "<|" in text or "|>" in text:
        raise ValueError("Strip model control tokens before speech synthesis")
    voice = body.get("voice", "xiaoyu")
    if not isinstance(voice, str) or voice.lower() not in {item.lower() for item in BUILTIN_VOICES}:
        raise ValueError("voice must be xiaoyu, yuewen, or lingyu")
    stream = body.get("stream", False)
    if not isinstance(stream, bool):
        raise ValueError("stream must be a boolean")
    format_name = body.get("response_format", "pcm" if stream else "wav")
    if format_name not in {"wav", "pcm"} or (stream and format_name != "pcm"):
        raise ValueError("Use WAV for complete audio, or stream=true with response_format=pcm")
    if body.get("speed", 1) != 1:
        raise ValueError("This model does not support a speed parameter")
    if body.get("instructions"):
        raise ValueError("This model uses its built-in voice and text. It does not support style instructions")
    return {"text": text.strip(), "voice": voice.lower(), "stream": stream, "format": format_name}


def check_files(runtime_dir: Path, model_dir: Path) -> dict:
    source = runtime_dir / "MOSS-TTS-Nano-main/ort_cpu_runtime.py"
    if not source.is_file():
        raise FileNotFoundError(f"Official OrtCpuRuntime source is missing: {source}")
    manifest_path = model_dir / "MOSS-TTS-Nano-100M-ONNX/browser_poc_manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    required = [manifest_path]
    for key in ("tts_meta", "codec_meta", "tokenizer_model"):
        required.append((manifest_path.parent / manifest["model_files"][key]).resolve())
    for meta_path in required[1:3]:
        metadata = json.loads(meta_path.read_text(encoding="utf-8"))
        required.extend(meta_path.parent / name for name in metadata["files"].values() if isinstance(name, str))
    required.extend(manifest_path.parent / name for name in ("moss_tts_global_shared.data", "moss_tts_local_shared.data"))
    required.extend(model_dir / "MOSS-Audio-Tokenizer-Nano-ONNX" / name for name in ("moss_audio_tokenizer_decode_shared.data", "moss_audio_tokenizer_encode.data"))
    for path in required:
        if not path.is_file() or path.stat().st_size == 0:
            raise FileNotFoundError(f"A required local model file is missing: {path}")
    versions = {package: version(package) for package in ("numpy", "onnxruntime", "sentencepiece")}
    return {"model": MODEL_ID, "runtime_versions": versions, "required_files": len(required)}


class SpeechRuntime:
    """One CPU model owns RNG and codec state. The server serializes its requests."""

    def __init__(self, runtime_dir: Path, model_dir: Path, threads: int):
        started = time.perf_counter()
        import numpy as np
        import sentencepiece as spm

        sys.path.insert(0, str(runtime_dir / "MOSS-TTS-Nano-main"))
        from ort_cpu_runtime import OrtCpuRuntime

        self.np = np
        self.model = OrtCpuRuntime(model_dir, thread_count=threads, max_new_frames=750, execution_provider="cpu")
        tokenizer = self.model.resolve_manifest_relative_path(self.model.manifest["model_files"]["tokenizer_model"])
        self.tokenizer = spm.SentencePieceProcessor(model_file=str(tokenizer))
        self.voices = {item["voice"].lower(): item for item in self.model.list_builtin_voices() if item["voice"] in BUILTIN_VOICES}
        if len(self.voices) != len(BUILTIN_VOICES):
            raise ValueError("The official model manifest does not contain the configured voices")
        self.sample_rate = int(self.model.codec_meta["codec_config"]["sample_rate"])
        self.channels = int(self.model.codec_meta["codec_config"]["channels"])
        self.threads = threads
        self.load_seconds = time.perf_counter() - started

    def describe(self) -> dict:
        return {"pid": os.getpid(), "device": "cpu", "execution_providers": self.model.sessions["prefill"].get_providers(), "threads": self.threads, "sample_rate": self.sample_rate, "channels": self.channels, "load_seconds": round(self.load_seconds, 3), "torch_loaded": "torch" in sys.modules, "instruction_control": False, "streaming": True}

    def synthesize(self, request: dict, emit, check_cancelled) -> dict:
        started = time.perf_counter()
        tokens = [int(token) for token in self.tokenizer.encode(request["text"], out_type=int)]
        rows = self.model.build_voice_clone_request_rows(self.voices[request["voice"]]["prompt_audio_codes"], tokens)
        pending = []
        first_pcm = None
        sample_count = 0
        chunks = 0
        self.model.codec_streaming_session.reset()

        def decode(force=False):
            nonlocal first_pcm, sample_count, chunks
            budget = 1 if first_pcm is None else (2 if sample_count < self.sample_rate * 0.64 else 8)
            if not pending or (not force and len(pending) < budget):
                return
            check_cancelled()
            frame_rows = pending[:] if force else pending[:budget]
            del pending[:len(frame_rows)]
            result = self.model.codec_streaming_session.run_frames(frame_rows)
            if result is None:
                return
            audio, length = result
            if length <= 0:
                return
            samples = audio[0, :, :length].T
            if not self.np.isfinite(samples).all():
                raise RuntimeError("The codec returned non-finite audio")
            pcm = (self.np.clip(samples, -1, 1) * 32767).astype("<i2").tobytes()
            if first_pcm is None:
                first_pcm = time.perf_counter() - started
            emit(pcm)
            chunks += 1
            sample_count += length

        def on_frame(_all, _index, frame):
            check_cancelled()
            if time.perf_counter() - started > 60:
                raise TimeoutError("Speech synthesis exceeded 60 seconds")
            pending.append(list(frame))
            decode()

        try:
            self.model.generate_audio_frames(rows, on_frame=on_frame)
            decode(True)
            if sample_count == 0:
                raise RuntimeError("Synthesis returned no audio")
            elapsed = time.perf_counter() - started
            duration = sample_count / self.sample_rate
            return {"first_pcm_seconds": round(first_pcm, 4), "elapsed_seconds": round(elapsed, 4), "audio_seconds": round(duration, 4), "rtf": round(elapsed / duration, 4), "chunks": chunks}
        finally:
            self.model.codec_streaming_session.reset()


class SpeechServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address: tuple, runtime):
        self.runtime = runtime
        self.generation_lock = threading.Lock()
        self.counts = {"completed": 0, "cancelled": 0, "failed": 0, "busy": 0}
        self.last_result = None
        super().__init__(address, SpeechHandler)


class SpeechHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *_args):
        pass

    def begin(self, status: int, content_type: str, headers: dict):
        self.send_response(status)
        origin = self.headers.get("Origin")
        if origin and allows_origin(origin):
            self.send_header("Access-Control-Allow-Origin", origin)
            self.send_header("Vary", "Origin")
        self.send_header("Access-Control-Expose-Headers", "X-Audio-Sample-Rate, X-Audio-Channels, X-Audio-Sample-Format, X-Request-Id")
        self.send_header("Content-Type", content_type)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "close")
        self.close_connection = True
        for name, value in headers.items():
            self.send_header(name, str(value))
        self.end_headers()

    def json(self, status: int, value: dict):
        payload = json.dumps(value, ensure_ascii=False, allow_nan=False).encode()
        self.begin(status, "application/json; charset=utf-8", {"Content-Length": len(payload)})
        self.wfile.write(payload)

    def allowed(self) -> bool:
        if allows_origin(self.headers.get("Origin")):
            return True
        self.json(403, {"error": {"message": "Only loopback browser origins are allowed"}})
        return False

    def do_OPTIONS(self):
        if self.allowed():
            self.begin(204, "text/plain", {"Content-Length": 0, "Access-Control-Allow-Methods": "GET, POST, OPTIONS", "Access-Control-Allow-Headers": "Authorization, Content-Type"})

    def do_GET(self):
        if not self.allowed():
            return
        path = urlsplit(self.path).path
        if path == "/health":
            self.json(200, {"status": "ready", "model": MODEL_ID, **self.server.runtime.describe(), "requests": dict(self.server.counts), "last_result": self.server.last_result})
        elif path == "/v1/models":
            self.json(200, {"object": "list", "data": [{"id": MODEL_ID, "object": "model", "owned_by": "OpenMOSS"}]})
        elif path == "/v1/audio/voices":
            self.json(200, {"voices": [{"id": name.lower(), "name": name, "language": "Chinese"} for name in BUILTIN_VOICES]})
        else:
            self.json(404, {"error": {"message": "Unknown endpoint"}})

    def check_cancelled(self):
        try:
            readable, _, _ = select.select([self.connection], [], [], 0)
            if readable and self.connection.recv(1, socket.MSG_PEEK) == b"":
                raise SpeechCancelled()
        except OSError as error:
            raise SpeechCancelled() from error

    def do_POST(self):
        if not self.allowed():
            return
        if urlsplit(self.path).path != "/v1/audio/speech":
            self.json(404, {"error": {"message": "Unknown endpoint"}})
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if not 0 < length <= 16384:
                raise ValueError("JSON body must contain 1 to 16384 bytes")
            self.connection.settimeout(8)
            request = parse_request(json.loads(self.rfile.read(length)))
        except (ValueError, TypeError) as error:
            self.json(400, {"error": {"message": str(error)}})
            return
        if not self.server.generation_lock.acquire(blocking=False):
            self.server.counts["busy"] += 1
            self.json(429, {"error": {"message": "Speech synthesis is busy. Cancel the previous stream before starting another"}})
            return
        request_id = uuid.uuid4().hex
        started = time.perf_counter()
        sent_headers = False
        output = io.BytesIO()
        runtime = self.server.runtime

        def emit(pcm: bytes):
            nonlocal sent_headers
            self.check_cancelled()
            if not request["stream"]:
                output.write(pcm)
                return
            if not sent_headers:
                self.begin(200, "audio/pcm", {"Transfer-Encoding": "chunked", "X-Audio-Sample-Rate": runtime.sample_rate, "X-Audio-Channels": runtime.channels, "X-Audio-Sample-Format": "pcm_s16le", "X-Request-Id": request_id})
                sent_headers = True
            self.wfile.write(f"{len(pcm):X}\r\n".encode() + pcm + b"\r\n")
            self.wfile.flush()

        try:
            self.check_cancelled()
            result = runtime.synthesize(request, emit, self.check_cancelled)
            if request["stream"]:
                self.wfile.write(b"0\r\n\r\n")
                self.wfile.flush()
            else:
                pcm = output.getvalue()
                if request["format"] == "wav":
                    output = io.BytesIO()
                    with wave.open(output, "wb") as wav:
                        wav.setnchannels(runtime.channels)
                        wav.setsampwidth(2)
                        wav.setframerate(runtime.sample_rate)
                        wav.writeframes(pcm)
                    payload = output.getvalue()
                    content_type = "audio/wav"
                else:
                    payload, content_type = pcm, "audio/pcm"
                self.begin(200, content_type, {"Content-Length": len(payload), "X-Audio-Sample-Rate": runtime.sample_rate, "X-Audio-Channels": runtime.channels, "X-Request-Id": request_id})
                sent_headers = True
                self.wfile.write(payload)
            self.server.counts["completed"] += 1
            self.server.last_result = {"request_id": request_id, "voice": request["voice"], "characters": len(request["text"]), "stream": request["stream"], **result}
            print(json.dumps({"speech_completed": self.server.counts["completed"], **self.server.last_result}), flush=True)
        except (SpeechCancelled, BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            self.server.counts["cancelled"] += 1
            print(json.dumps({"speech_cancelled": request_id, "elapsed_seconds": round(time.perf_counter() - started, 4)}), flush=True)
        except Exception as error:
            self.server.counts["failed"] += 1
            print(json.dumps({"speech_error": type(error).__name__, "request_id": request_id}), flush=True)
            if not sent_headers:
                self.json(500, {"error": {"message": f"Local speech synthesis failed ({type(error).__name__})"}})
        finally:
            self.server.generation_lock.release()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runtime-dir", type=Path, default=ROOT.parent / ".tools/moss-tts-nano")
    parser.add_argument("--model-dir", type=Path)
    parser.add_argument("--port", type=int, default=18084)
    parser.add_argument("--threads", type=int, default=2)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    if not 1 <= args.port <= 65535 or not 1 <= args.threads <= 16:
        parser.error("Use port 1 to 65535 and threads 1 to 16")
    runtime_dir = args.runtime_dir.resolve()
    model_dir = (args.model_dir or runtime_dir / "models").resolve()
    checked = check_files(runtime_dir, model_dir)
    if args.check:
        print(json.dumps({"verified": True, **checked}), flush=True)
        return 0
    runtime = SpeechRuntime(runtime_dir, model_dir, args.threads)
    server = SpeechServer(("127.0.0.1", args.port), runtime)
    print(json.dumps({"listening": f"http://127.0.0.1:{args.port}", "model": MODEL_ID, **runtime.describe()}), flush=True)
    server.serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
