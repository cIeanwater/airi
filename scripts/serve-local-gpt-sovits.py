"""Serve a compact GPT-SoVITS voice with CPU features and FP16 CUDA synthesis."""

import argparse
import hashlib
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
from contextlib import redirect_stderr, redirect_stdout
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit


ROOT = Path(__file__).resolve().parents[1]
MODEL_ID = "gpt-sovits-v2proplus"
VOICE_ID = "student-bright"
REFERENCE_TEXT = "诶，真的吗？这个想法好有意思！你是怎么想到的呀？"
SOURCE_REVISION = "1b47ca07ef43a284e0b8bd7f29d3e1e3ff76082a"


class SpeechCancelled(BaseException):
    """Disconnects bypass the upstream exception handler that reloads both models."""


def allows_origin(origin):
    if origin is None:
        return True
    try:
        parsed = urlsplit(origin)
        parsed.port
    except ValueError:
        return False
    return (parsed.scheme in {"http", "https"} and parsed.hostname in {"localhost", "127.0.0.1", "::1"}
            and parsed.username is None and parsed.password is None and not parsed.path
            and not parsed.query and not parsed.fragment and not any(c.isspace() for c in origin))


def parse_request(body):
    if not isinstance(body, dict):
        raise ValueError("Expected a JSON object")
    if body.get("model") != MODEL_ID or body.get("voice", VOICE_ID) != VOICE_ID:
        raise ValueError(f"Use model={MODEL_ID} and voice={VOICE_ID}")
    text = body.get("input")
    if not isinstance(text, str) or not text.strip() or len(text) > 240:
        raise ValueError("input must contain 1 to 240 characters")
    if "<|" in text or "|>" in text:
        raise ValueError("Strip model control tokens before speech synthesis")
    stream = body.get("stream", False)
    if not isinstance(stream, bool):
        raise ValueError("stream must be a boolean")
    output_format = body.get("response_format", "pcm" if stream else "wav")
    if output_format not in {"wav", "pcm"} or (stream and output_format != "pcm"):
        raise ValueError("Use WAV for complete audio, or stream=true with response_format=pcm")
    if body.get("instructions") or body.get("speed", 1) != 1:
        raise ValueError("This voice uses its reference prosody. Instructions and speed changes are unsupported")
    return {"text": text.strip(), "stream": stream, "format": output_format}


def check_files(runtime_dir, reference):
    models = runtime_dir / "GPT_SoVITS/pretrained_models"
    required = [runtime_dir / "GPT_SoVITS/TTS_infer_pack/TTS.py", models / "s1v3.ckpt",
                models / "v2Pro/s2Gv2ProPlus.pth", models / "sv/pretrained_eres2netv2w24s4ep4.ckpt",
                models / "chinese-roberta-wwm-ext-large/pytorch_model.bin",
                models / "chinese-hubert-base/pytorch_model.bin", reference,
                models / "chinese-roberta-wwm-ext-large/config.json", models / "chinese-roberta-wwm-ext-large/tokenizer.json",
                models / "chinese-hubert-base/config.json", models / "chinese-hubert-base/preprocessor_config.json",
                models / "fast_langdetect/lid.176.bin"]
    g2pw = runtime_dir / "GPT_SoVITS/text/G2PWModel"
    required.extend(g2pw / name for name in ("g2pW.onnx", "config.py", "POLYPHONIC_CHARS.txt", "MONOPHONIC_CHARS.txt",
                                          "char_bopomofo_dict.json", "bopomofo_to_pinyin_wo_tune_dict.json"))
    for path in required:
        if not path.is_file() or not path.stat().st_size:
            raise FileNotFoundError(f"A required local speech file is missing: {path}")
    manifest = json.loads((runtime_dir / "publisher-manifest.json").read_text(encoding="utf-8"))
    if manifest["source_revision"] != SOURCE_REVISION:
        raise ValueError("The GPT-SoVITS source revision differs from the reviewed revision")
    for patch in manifest["source_patches"]:
        if hashlib.sha256((runtime_dir / patch["path"]).read_bytes()).hexdigest() != patch["sha256"]:
            raise ValueError("A prepared runtime source file changed: " + patch["path"])
    with wave.open(str(reference), "rb") as audio:
        seconds = audio.getnframes() / audio.getframerate()
    if not 3 <= seconds <= 10:
        raise ValueError("The voice reference must contain 3 to 10 seconds of original synthetic speech")
    return {"model": MODEL_ID, "source_revision": SOURCE_REVISION, "required_files": len(required),
            "reference_sha256": hashlib.sha256(reference.read_bytes()).hexdigest(), "reference_seconds": seconds}


class SpeechRuntime:
    """One voice owns its reference cache, CUDA models, and per-request cancellation hook."""

    def __init__(self, runtime_dir, reference, threads):
        started = time.perf_counter()
        os.chdir(runtime_dir)
        sys.path[:0] = [str(runtime_dir), str(runtime_dir / "GPT_SoVITS")]
        os.environ["HF_HUB_OFFLINE"] = "1"
        os.environ["TRANSFORMERS_OFFLINE"] = "1"
        os.environ["TOKENIZERS_PARALLELISM"] = "false"
        import torch
        import numpy as np
        import soundfile as sf
        import librosa
        from transformers import AutoModelForMaskedLM, AutoTokenizer
        from TTS_infer_pack.TTS import TTS, resample, spectrogram_torch
        from feature_extractor.cnhubert import CNHubert
        from sv import SV

        if not torch.cuda.is_available():
            raise RuntimeError("This speech profile requires CUDA. The CPU voice is a separate profile")
        torch.set_num_threads(threads)
        torch.cuda.reset_peak_memory_stats()
        self.torch, self.np = torch, np
        self.check_cancelled = lambda: None
        self.quiet_output = open(os.devnull, "w", encoding="utf-8")

        class CpuFeaturesTTS(TTS):
            # CPU features preserve CUDA memory for the conversation model and its KV cache.
            def empty_cache(instance):
                # Keep the CUDA allocator warm between turns. Tensor references still release after generator shutdown.
                # Upstream's full Python collection delays each cancellation and evicts reusable CUDA blocks.
                return None

            def init_bert_weights(instance, path):
                instance.bert_tokenizer = AutoTokenizer.from_pretrained(path, local_files_only=True)
                instance.bert_model = AutoModelForMaskedLM.from_pretrained(path, local_files_only=True).eval().float().cpu()

            def init_cnhuhbert_weights(instance, path):
                instance.cnhuhbert_model = CNHubert(path).eval().float().cpu()

            def init_sv_model(instance):
                if instance.sv_model is None:
                    instance.sv_model = SV(torch.device("cpu"), False)

            def _set_prompt_semantic(instance, path):
                samples, sample_rate = sf.read(path, dtype="float32", always_2d=True)
                samples = librosa.resample(samples.mean(axis=1), orig_sr=sample_rate, target_sr=16000)
                # Match the reviewed upstream reference padding, on CPU.
                samples = np.concatenate([samples, np.zeros(int(instance.configs.sampling_rate * 0.3), dtype=np.float32)])
                with torch.inference_mode():
                    hidden = instance.cnhuhbert_model.model(torch.from_numpy(samples).unsqueeze(0))["last_hidden_state"].transpose(1, 2)
                    codes = instance.vits_model.extract_latent(hidden.to(device="cuda", dtype=torch.float16))
                instance.prompt_cache["prompt_semantic"] = codes[0, 0]

            def _get_ref_spec(instance, path):
                # NOTICE:
                # This service accepts reviewed PCM WAV references through SoundFile.
                # torchaudio 2.9 load() requires a separate TorchCodec and FFmpeg runtime.
                # Source: torchaudio 2.9 load() documentation.
                # Remove this override when upstream supports the SoundFile reader directly.
                samples, rate = sf.read(path, dtype="float32", always_2d=True)
                raw = torch.from_numpy(samples.T.copy()).to("cuda")
                instance.prompt_cache.update({"raw_audio": raw, "raw_sr": rate})
                audio = resample(raw.mean(0, keepdim=True), rate, instance.configs.sampling_rate, instance.configs.device)
                maximum = audio.abs().max()
                if maximum > 1:
                    audio /= min(2, maximum)
                spectrum = spectrogram_torch(audio, instance.configs.filter_length, instance.configs.sampling_rate,
                                             instance.configs.hop_length, instance.configs.win_length, center=False).half()
                speaker_audio = resample(audio, instance.configs.sampling_rate, 16000, instance.configs.device).half()
                return spectrum, speaker_audio

        models = runtime_dir / "GPT_SoVITS/pretrained_models"
        config = {"custom": {"version": "v2ProPlus", "device": "cuda", "is_half": True,
                             "t2s_weights_path": str(models / "s1v3.ckpt"),
                             "vits_weights_path": str(models / "v2Pro/s2Gv2ProPlus.pth"),
                             "bert_base_path": str(models / "chinese-roberta-wwm-ext-large"),
                             "cnhuhbert_base_path": str(models / "chinese-hubert-base")}}
        self.model = CpuFeaturesTTS(config)
        self.model.text_preprocessor.device = torch.device("cpu")
        self.reference = str(reference)
        self.model.set_ref_audio(self.reference)
        phones, features, normalized = self.model.text_preprocessor.segment_and_extract_feature_for_text(REFERENCE_TEXT, "all_zh", "v2ProPlus")
        self.model.prompt_cache.update({"prompt_text": REFERENCE_TEXT, "prompt_lang": "all_zh", "phones": phones,
                                       "bert_features": features, "norm_text": normalized})
        samples, rate = sf.read(reference, dtype="float32", always_2d=True)
        samples = librosa.resample(samples.mean(axis=1), orig_sr=rate, target_sr=16000)
        with torch.inference_mode():
            speaker = self.model.sv_model.compute_embedding3(torch.from_numpy(samples).unsqueeze(0)).to("cuda", dtype=torch.float16)
        self.model.sv_model.compute_embedding3 = lambda _audio: speaker
        # A public PyTorch forward hook checks disconnects for every generated token.
        # BaseException exits the upstream generator without its costly model reload path.
        self.model.t2s_model.model.ar_predict_layer.register_forward_pre_hook(lambda _module, _args: self.check_cancelled())
        self.model.bert_model.register_forward_pre_hook(lambda _module, _args: self.check_cancelled())
        self.sample_rate = self.model.configs.sampling_rate
        self.channels = 1
        self.threads = threads
        # Warm public synthetic speech before readiness so the first conversation avoids CUDA kernel initialization gaps.
        self.warmup_result = self.synthesize({"text": "放学啦，今天有什么有趣的事情想和我说吗？"}, lambda _pcm: None, lambda: None)
        self.load_seconds = time.perf_counter() - started

    def describe(self):
        torch = self.torch
        return {"pid": os.getpid(), "device": "cuda", "dtype": "float16", "feature_device": "cpu",
                "voice": VOICE_ID, "sample_rate": self.sample_rate, "channels": self.channels,
                "threads": self.threads, "streaming": True, "instruction_control": False,
                "warmup": self.warmup_result,
                "load_seconds": round(self.load_seconds, 3),
                "cuda_allocated_mib": round(torch.cuda.memory_allocated() / 1048576, 2),
                "cuda_reserved_mib": round(torch.cuda.memory_reserved() / 1048576, 2),
                "cuda_peak_reserved_mib": round(torch.cuda.max_memory_reserved() / 1048576, 2),
                "synthesis_parameters": sum(p.numel() for model in (self.model.t2s_model, self.model.vits_model) for p in model.parameters())}

    def synthesize(self, request, emit, check_cancelled):
        started = time.perf_counter()
        self.check_cancelled = check_cancelled
        chunks, frames, first_pcm = 0, 0, None
        inputs = {"text": request["text"], "text_lang": "all_zh", "ref_audio_path": self.reference,
                  "prompt_text": REFERENCE_TEXT, "prompt_lang": "all_zh", "text_split_method": "cut0",
                  "top_k": 15, "top_p": 1.0, "temperature": 0.8, "repetition_penalty": 1.35,
                  "batch_size": 1, "split_bucket": False, "parallel_infer": False,
                  "streaming_mode": True, "min_chunk_length": 24, "fixed_length_chunk": False,
                  "overlap_length": 4, "fragment_interval": 0.0, "seed": 20261018}
        generator = self.model.run(inputs)
        try:
            # The upstream generator prints utterance text. Keep request content out of persistent service logs.
            # Inference owns one lock. Other request handlers never write to stdout or stderr.
            with self.torch.inference_mode(), redirect_stdout(self.quiet_output), redirect_stderr(self.quiet_output):
                for rate, audio in generator:
                    check_cancelled()
                    if rate != self.sample_rate:
                        raise RuntimeError("The speech model returned an unexpected sample rate")
                    if not len(audio):
                        continue
                    payload = self.np.asarray(audio, dtype="<i2").tobytes()
                    if first_pcm is None:
                        first_pcm = time.perf_counter() - started
                    emit(payload)
                    frames += len(audio)
                    chunks += 1
        finally:
            generator.close()
            self.check_cancelled = lambda: None
        seconds = frames / self.sample_rate
        elapsed = time.perf_counter() - started
        return {"first_pcm_seconds": round(first_pcm, 4), "generation_seconds": round(elapsed, 4),
                "audio_seconds": round(seconds, 4), "real_time_factor": round(elapsed / seconds, 4), "chunks": chunks,
                "cuda_peak_reserved_mib": round(self.torch.cuda.max_memory_reserved() / 1048576, 2)}


class SpeechServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address, runtime):
        super().__init__(address, SpeechHandler)
        self.runtime = runtime
        self.generation_lock = threading.Lock()
        self.counts = {"completed": 0, "cancelled": 0, "failed": 0, "busy": 0}
        self.last_result = None


class SpeechHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *_args):
        pass

    def begin(self, status, content_type, headers):
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

    def json(self, status, value):
        payload = json.dumps(value, ensure_ascii=False, allow_nan=False).encode()
        self.begin(status, "application/json; charset=utf-8", {"Content-Length": len(payload)})
        self.wfile.write(payload)

    def allowed(self):
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
            self.json(200, {"status": "ready", "model": MODEL_ID, **self.server.runtime.describe(),
                            "busy": self.server.generation_lock.locked(), "requests": dict(self.server.counts), "last_result": self.server.last_result})
        elif path == "/v1/models":
            self.json(200, {"object": "list", "data": [{"id": MODEL_ID, "object": "model", "owned_by": "local"}]})
        elif path == "/v1/audio/voices":
            self.json(200, {"voices": [{"id": VOICE_ID, "name": "Student Bright", "language": "Chinese"}]})
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
            self.json(429, {"error": {"message": "Speech synthesis is busy. Cancel the previous stream first"}})
            return
        request_id = uuid.uuid4().hex
        started, sent_headers, output = time.perf_counter(), False, io.BytesIO()
        runtime = self.server.runtime

        def emit(pcm):
            nonlocal sent_headers
            self.check_cancelled()
            if not request["stream"]:
                output.write(pcm)
                return
            if not sent_headers:
                self.begin(200, "audio/pcm", {"Transfer-Encoding": "chunked", "X-Audio-Sample-Rate": runtime.sample_rate,
                                             "X-Audio-Channels": runtime.channels, "X-Audio-Sample-Format": "pcm_s16le", "X-Request-Id": request_id})
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
                    payload, content_type = output.getvalue(), "audio/wav"
                else:
                    payload, content_type = pcm, "audio/pcm"
                self.begin(200, content_type, {"Content-Length": len(payload), "X-Audio-Sample-Rate": runtime.sample_rate,
                                             "X-Audio-Channels": runtime.channels, "X-Request-Id": request_id})
                sent_headers = True
                self.wfile.write(payload)
            self.server.counts["completed"] += 1
            self.server.last_result = {"request_id": request_id, "characters": len(request["text"]), **result}
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


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runtime-dir", type=Path, default=ROOT.parent / ".tools/gpt-sovits-v2proplus")
    parser.add_argument("--reference", type=Path, default=ROOT.parent / ".tools/modern-voice-2026-10-17/vivian-curious.wav")
    parser.add_argument("--port", type=int, default=18089)
    parser.add_argument("--threads", type=int, default=4)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    if not 1 <= args.port <= 65535 or not 1 <= args.threads <= 16:
        parser.error("Use port 1 to 65535 and threads 1 to 16")
    runtime_dir, reference = args.runtime_dir.resolve(), args.reference.resolve()
    checked = check_files(runtime_dir, reference)
    if args.check:
        print(json.dumps({"verified": True, **checked}), flush=True)
        return 0
    runtime = SpeechRuntime(runtime_dir, reference, args.threads)
    server = SpeechServer(("127.0.0.1", args.port), runtime)
    print(json.dumps({"listening": f"http://127.0.0.1:{args.port}", "model": MODEL_ID, **runtime.describe()}), flush=True)
    server.serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
