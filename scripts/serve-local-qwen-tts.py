"""Serve expressive Qwen3-TTS CustomVoice speech on the local computer.

Call stack:
  main -> verified local BF16 model -> serialized synthesis -> WAV response
"""

import argparse
import gc
import hashlib
import io
import json
import os
import threading
import time
from collections import Counter
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib.metadata import version
from pathlib import Path
from urllib.parse import urlsplit


MODEL_ID = "qwen3-tts-1.7b-customvoice"
REVISION = "0c0e3051f131929182e2c023b9537f8b1c68adfe"
WEIGHTS = {
    "model.safetensors": "38b1d5971bdbd982b561cccec982669a53b0537c3cf5e9bd4778ed07bb2f5137",
    "speech_tokenizer/model.safetensors": "836b7b357f5ea43e889936a3709af68dfe3751881acefe4ecf0dbd30ba571258",
}
PERSONA = (
    "用自然、清亮、温柔的年轻成年女大学生声线说普通话。"
    "像和熟悉的朋友面对面聊天，有自然的停连、重音和语调起伏。"
    "保持轻快亲切，问句有真实的疑问语气，安慰时柔和，开心时带笑意。"
    "不要播音腔，不要平直念稿，不要夸张卖萌。"
)
EMOTIONS = {
    "happy": "语气开心、轻快，带自然笑意。",
    "sad": "语气低落、轻柔，语速稍慢，保持清晰。",
    "angry": "语气认真、略带不满，重音明确，不要大喊。",
    "think": "语气若有所思，停顿自然。",
    "surprised": "语气惊喜，有自然的音高起伏。",
    "awkward": "语气略害羞、带一点不好意思的笑意。",
    "question": "语气疑惑，问句自然上扬。",
    "curious": "语气好奇、有兴趣，轻快地发问。",
    "neutral": "语气自然、平和、亲切。",
}


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


def verify(directory: Path) -> None:
    config = json.loads((directory / "config.json").read_text(encoding="utf-8"))
    if config.get("tts_model_size") != "1b7" or config.get("tts_model_type") != "custom_voice":
        raise ValueError("This service requires the instruction-capable 1.7B CustomVoice checkpoint")
    for name, expected in WEIGHTS.items():
        digest = hashlib.sha256()
        with (directory / name).open("rb") as source:
            for chunk in iter(lambda: source.read(8 * 1024 * 1024), b""):
                digest.update(chunk)
        if digest.hexdigest() != expected:
            raise ValueError(f"Publisher SHA256 mismatch: {name}")


def parse_request(body: object, speakers: set[str]) -> dict:
    if not isinstance(body, dict):
        raise ValueError("Expected a JSON object")
    if body.get("model") != MODEL_ID:
        raise ValueError(f"model must be {MODEL_ID}")
    text = body.get("input")
    if not isinstance(text, str) or not text.strip() or len(text) > 400:
        raise ValueError("input must contain 1 to 400 characters")
    if "<|" in text or "|>" in text:
        raise ValueError("Strip model control tokens before speech synthesis")
    speaker = body.get("voice", "serena")
    if not isinstance(speaker, str) or speaker.lower() not in speakers:
        raise ValueError("voice must identify a supported Qwen speaker")
    if body.get("response_format", "wav") != "wav":
        raise ValueError("This service returns WAV. Set response_format to wav")
    if body.get("speed", 1) != 1:
        raise ValueError("Use natural-language instructions to control speaking speed")
    extra = body.get("extra_body", {})
    if not isinstance(extra, dict):
        raise ValueError("extra_body must be an object")
    instructions = body.get("instructions", extra.get("instructions", ""))
    emotion = body.get("emotion", extra.get("emotion", "neutral"))
    if not isinstance(instructions, str) or len(instructions) > 1000:
        raise ValueError("instructions must be text with at most 1000 characters")
    if emotion not in EMOTIONS:
        raise ValueError("Unsupported emotion")
    return {"text": text.strip(), "speaker": speaker.lower(), "instruct": PERSONA + EMOTIONS[emotion] + instructions, "emotion": emotion}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model-dir", type=Path, default=Path(__file__).resolve().parents[1] / "local-models/qwen3-tts-1.7b-customvoice")
    parser.add_argument("--port", type=int, default=18084)
    parser.add_argument("--verify-only", "--check", action="store_true")
    args = parser.parse_args()
    if not 1 <= args.port <= 65535:
        parser.error("port must be between 1 and 65535")
    directory = args.model_dir.resolve()
    verify(directory)
    runtime_versions = {package: version(package) for package in ("qwen-tts", "torch", "torchaudio", "transformers")}
    if args.verify_only:
        print(json.dumps({"verified": True, "model": MODEL_ID, "revision": REVISION, "runtime_versions": runtime_versions}), flush=True)
        return 0
    os.environ.update({"HF_HUB_OFFLINE": "1", "TRANSFORMERS_OFFLINE": "1", "TOKENIZERS_PARALLELISM": "false"})
    import numpy as np
    import soundfile as sf
    import torch
    from qwen_tts import Qwen3TTSModel

    if not torch.cuda.is_available() or not torch.cuda.is_bf16_supported():
        raise RuntimeError("A CUDA GPU with native BF16 support is required")
    torch.set_num_threads(4)
    started = time.monotonic()
    model = Qwen3TTSModel.from_pretrained(str(directory), device_map="cuda:0", dtype=torch.bfloat16, attn_implementation="sdpa", local_files_only=True)
    speakers = set(model.get_supported_speakers())
    parameter_dtypes = dict(Counter(str(parameter.dtype) for parameter in model.model.parameters()))
    generation_lock = threading.Lock()
    counts = {"completed": 0, "failed": 0}
    load_seconds = time.monotonic() - started

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *_args):
            # Request bodies and user text stay out of the service console.
            pass

        def respond(self, status: int, payload: bytes, content_type: str, headers: dict | None = None):
            self.send_response(status)
            origin = self.headers.get("Origin")
            if origin and allows_origin(origin):
                self.send_header("Access-Control-Allow-Origin", origin)
                self.send_header("Vary", "Origin")
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(payload)))
            self.send_header("Cache-Control", "no-store")
            for name, value in (headers or {}).items():
                self.send_header(name, str(value))
            self.end_headers()
            self.wfile.write(payload)

        def json(self, status: int, value: dict):
            self.respond(status, json.dumps(value, ensure_ascii=False).encode(), "application/json; charset=utf-8")

        def allowed(self) -> bool:
            if allows_origin(self.headers.get("Origin")):
                return True
            self.json(403, {"error": {"message": "Only loopback browser origins are allowed"}})
            return False

        def do_OPTIONS(self):
            if self.allowed():
                self.respond(204, b"", "text/plain", {"Access-Control-Allow-Methods": "GET, POST, OPTIONS", "Access-Control-Allow-Headers": "Authorization, Content-Type"})

        def do_GET(self):
            if not self.allowed():
                return
            path = urlsplit(self.path).path
            if path == "/health":
                self.json(200, {"status": "ready", "pid": os.getpid(), "model": MODEL_ID, "revision": REVISION, "dtype": "bfloat16", "parameter_dtypes": parameter_dtypes, "runtime_versions": runtime_versions, "device": str(model.device), "attention": "sdpa", "load_seconds": round(load_seconds, 3), "cuda_allocated_bytes": torch.cuda.memory_allocated(), "cuda_reserved_bytes": torch.cuda.memory_reserved(), "speakers": sorted(speakers), "requests": dict(counts)})
            elif path == "/v1/models":
                self.json(200, {"object": "list", "data": [{"id": MODEL_ID, "object": "model", "owned_by": "Qwen"}]})
            elif path == "/v1/audio/voices":
                self.json(200, {"voices": [{"id": voice, "name": voice, "language": "Chinese"} for voice in sorted(speakers)]})
            else:
                self.json(404, {"error": {"message": "Unknown endpoint"}})

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
                request = parse_request(json.loads(self.rfile.read(length)), speakers)
            except (ValueError, TypeError) as error:
                self.json(400, {"error": {"message": str(error)}})
                return
            if not generation_lock.acquire(timeout=120):
                self.json(429, {"error": {"message": "Speech synthesis queue exceeded 120 seconds"}})
                return
            started = time.monotonic()
            try:
                with torch.inference_mode():
                    waves, rate = model.generate_custom_voice(text=request["text"], language="Chinese", speaker=request["speaker"], instruct=request["instruct"], max_new_tokens=768, non_streaming_mode=False)
                samples = np.asarray(waves[0], dtype=np.float32)
                if samples.size == 0 or not np.isfinite(samples).all():
                    raise RuntimeError("Synthesis returned invalid audio")
                audio = io.BytesIO()
                sf.write(audio, samples, rate, format="WAV", subtype="PCM_16")
                elapsed = time.monotonic() - started
                counts["completed"] += 1
                print(json.dumps({"speech_completed": counts["completed"], "characters": len(request["text"]), "speaker": request["speaker"], "emotion": request["emotion"], "audio_seconds": round(samples.size / rate, 3), "elapsed_seconds": round(elapsed, 3)}, ensure_ascii=False), flush=True)
                self.respond(200, audio.getvalue(), "audio/wav", {"X-Synthesis-Seconds": round(elapsed, 3), "X-Audio-Seconds": round(samples.size / rate, 3)})
            except (BrokenPipeError, ConnectionResetError):
                pass
            except Exception as error:
                counts["failed"] += 1
                print(json.dumps({"speech_error": type(error).__name__}), flush=True)
                self.json(500, {"error": {"message": f"Local speech synthesis failed ({type(error).__name__})"}})
            finally:
                gc.collect()
                torch.cuda.empty_cache()
                generation_lock.release()

    print(json.dumps({"listening": f"http://127.0.0.1:{args.port}", "model": MODEL_ID, "dtype": "bfloat16", "load_seconds": round(load_seconds, 3)}, ensure_ascii=False), flush=True)
    ThreadingHTTPServer(("127.0.0.1", args.port), Handler).serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
