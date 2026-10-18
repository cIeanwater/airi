"""Serve the local Qwen3.5-2B BF16 checkpoint for AIRI text chat.

The service uses existing PyTorch and Transformers packages. It binds to loopback,
checks publisher hashes, and records complete messages only with an explicit flag.
"""

import argparse
import gc
import hashlib
import importlib.util
import json
import math
import os
import queue
import re
import socket
import struct
import threading
import time
import uuid
from collections import Counter
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit


ROOT = Path(__file__).resolve().parent.parent
MODEL_ID = "qwen3.5-2b-bf16"
MAX_BODY_BYTES = 1024 * 1024

_context_spec = importlib.util.spec_from_file_location("local_message_context", ROOT / "scripts/local-message-context.py")
_context_module = importlib.util.module_from_spec(_context_spec)
_context_spec.loader.exec_module(_context_module)


def reject_nonfinite_json(value: str) -> None:
    raise ValueError(f"JSON numbers must be finite, received {value}")


class MessageRecorder:
    """Keep optional request records on disk, without HTTP headers."""

    def __init__(self, directory: Path | None, max_age_days: int, max_records: int):
        if max_age_days < 1 or max_records < 1:
            raise ValueError("Message retention limits must be positive integers")
        self.directory = directory.resolve() if directory is not None else None
        self.max_age_days = max_age_days
        self.max_records = max_records
        self.last_error = None
        self.lock = threading.Lock()
        if self.directory is not None:
            self.directory.mkdir(parents=True, exist_ok=True)
            self.prune()

    def describe(self) -> dict:
        return {
            "enabled": self.directory is not None,
            "directory": str(self.directory) if self.directory is not None else None,
            "max_age_days": self.max_age_days,
            "max_records": self.max_records,
            "last_error": self.last_error,
        }

    def prune(self) -> None:
        if self.directory is None:
            return
        cutoff = time.time() - self.max_age_days * 86400
        records = []
        for target in self.directory.glob("*-chatcmpl-*.json"):
            if not re.fullmatch(r"\d{8}T\d{12}Z-chatcmpl-[0-9a-f]{32}\.json", target.name) or target.is_symlink():
                continue
            if target.stat().st_mtime < cutoff:
                target.unlink()
            else:
                records.append(target)
        for target in sorted(records, key=lambda path: path.name, reverse=True)[self.max_records:]:
            target.unlink()

    def read_context(self, query: dict) -> dict:
        with self.lock:
            return _context_module.read_local_message_context(self.directory, query,
                max_age_days=self.max_age_days, max_records=self.max_records)

    def write(self, record: dict) -> None:
        if self.directory is None:
            return
        try:
            if not re.fullmatch(r"chatcmpl-[0-9a-f]{32}", record["request_id"]):
                raise ValueError("Invalid record request ID")
            stamp = datetime.fromtimestamp(record["started_at_unix"], timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
            target = self.directory / f"{stamp}-{record['request_id']}.json"
            temporary = target.with_suffix(".json.tmp")
            with self.lock:
                try:
                    temporary.write_text(json.dumps(record, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")
                    # NOTICE:
                    # Windows readers can briefly block atomic replacement without FILE_SHARE_DELETE.
                    # See https://learn.microsoft.com/en-us/windows/win32/api/fileapi/nf-fileapi-createfilea.
                    # Remove retries when supported readers no longer block replacement.
                    for attempt in range(6):
                        try:
                            temporary.replace(target)
                            break
                        except PermissionError as error:
                            if os.name != "nt" or getattr(error, "winerror", None) not in {5, 32, 33} or attempt == 5:
                                raise
                            time.sleep(0.01 * 2 ** attempt)
                finally:
                    temporary.unlink(missing_ok=True)
                self.prune()
                self.last_error = None
        except (OSError, ValueError) as error:
            self.last_error = type(error).__name__
            print(json.dumps({"request_id": record["request_id"], "message_record_error": self.last_error}), flush=True)


def allows_browser_origin(origin: str | None) -> bool:
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


def verify_checkpoint(directory: Path) -> dict:
    config = json.loads((directory / "config.json").read_text(encoding="utf-8"))
    if config.get("architectures") != ["Qwen3_5ForConditionalGeneration"]:
        raise ValueError("Expected Qwen3_5ForConditionalGeneration")
    if config["text_config"].get("dtype") != "bfloat16" or config.get("quantization_config"):
        raise ValueError("Expected an unquantized BF16 checkpoint")
    if config["text_config"].get("num_hidden_layers") != 24 or config["text_config"].get("hidden_size") != 2048:
        raise ValueError("Expected the Qwen3.5-2B architecture")
    manifest = json.loads((directory / "publisher-manifest.json").read_text(encoding="utf-8"))
    if manifest.get("repo") != "Qwen/Qwen3.5-2B":
        raise ValueError("Publisher manifest does not identify Qwen/Qwen3.5-2B")
    hashes = {}
    for entry in manifest["files"]:
        target = (directory / entry["Path"]).resolve()
        if directory not in target.parents or not target.is_file():
            raise ValueError(f"Missing or unsafe checkpoint file: {entry['Path']}")
        if target.stat().st_size != entry["Size"]:
            raise ValueError(f"Checkpoint size mismatch: {target.name}")
        digest = hashlib.sha256()
        with target.open("rb") as source:
            for chunk in iter(lambda: source.read(8 * 1024 * 1024), b""):
                digest.update(chunk)
        if digest.hexdigest() != entry["Sha256"]:
            raise ValueError(f"Publisher SHA-256 mismatch: {target.name}")
        hashes[entry["Path"]] = digest.hexdigest()
    index = json.loads((directory / "model.safetensors.index.json").read_text(encoding="utf-8"))
    tensors = {}
    dtype_counts = Counter()
    payload_bytes = 0
    for shard in sorted(set(index["weight_map"].values())):
        target = directory / shard
        with target.open("rb") as source:
            header_bytes = struct.unpack("<Q", source.read(8))[0]
            header = json.loads(source.read(header_bytes))
        entries = {name: value for name, value in header.items() if name != "__metadata__"}
        offset = 0
        for name, value in sorted(entries.items(), key=lambda item: item[1]["data_offsets"][0]):
            start, end = value["data_offsets"]
            dtype = value["dtype"]
            if dtype not in {"BF16", "F32"}:
                raise ValueError(f"Unexpected weight dtype: {dtype}")
            size = math.prod(value["shape"]) * (2 if dtype == "BF16" else 4)
            if start != offset or end - start != size or index["weight_map"].get(name) != shard:
                raise ValueError(f"Invalid safetensors layout: {name}")
            offset = end
            dtype_counts[dtype] += 1
        if offset + header_bytes + 8 != target.stat().st_size:
            raise ValueError(f"Truncated or trailing safetensors data: {shard}")
        tensors.update(entries)
        payload_bytes += offset
    if set(tensors) != set(index["weight_map"]) or payload_bytes != index["metadata"]["total_size"]:
        raise ValueError("The safetensors index and payload do not match")
    return {
        "publisher_repo": manifest["repo"],
        "model_path": str(directory),
        "architecture": config["architectures"][0],
        "checkpoint_tensor_count": len(tensors),
        "checkpoint_dtype_counts": dict(dtype_counts),
        "checkpoint_payload_bytes": payload_bytes,
        "publisher_hashes_verified": True,
        "sha256": hashes,
        "quantization": None,
        "native_audio": False,
        "service_modalities": ["text"],
    }


def validate_request(payload: object, max_output: int) -> dict:
    if not isinstance(payload, dict) or payload.get("model") != MODEL_ID:
        raise ValueError(f"model must be {MODEL_ID}")
    if payload.get("audio") or payload.get("modalities", ["text"]) != ["text"]:
        raise ValueError("Native audio is unsupported. Transcribe audio with local ASR first.")
    messages = payload.get("messages")
    if not isinstance(messages, list) or not 1 <= len(messages) <= 128:
        raise ValueError("messages must contain between 1 and 128 items")
    for message in messages:
        if not isinstance(message, dict) or message.get("role") not in {"system", "user", "assistant", "tool"}:
            raise ValueError("Invalid message role")
        content = message.get("content")
        if isinstance(content, list):
            if any(not isinstance(part, dict) or part.get("type") != "text" or not isinstance(part.get("text"), str) for part in content):
                raise ValueError("This text endpoint rejects image, video, and input_audio content")
        elif not isinstance(content, str) and not (content is None and message.get("tool_calls")):
            raise ValueError("Message content must be text")
    tools = payload.get("tools", [])
    if not isinstance(tools, list) or len(tools) > 64:
        raise ValueError("tools must be a list with at most 64 entries")
    for tool in tools:
        if not isinstance(tool, dict) or tool.get("type") != "function" or not isinstance(tool.get("function"), dict):
            raise ValueError("Only function tools are supported")
    output_tokens = payload.get("max_tokens", 128)
    if not isinstance(output_tokens, int) or isinstance(output_tokens, bool) or not 1 <= output_tokens <= max_output:
        raise ValueError(f"max_tokens must be an integer between 1 and {max_output}")
    temperature = payload.get("temperature", 0.7)
    top_p = payload.get("top_p", 0.8)
    if not isinstance(temperature, (int, float)) or not math.isfinite(temperature) or not 0 <= temperature <= 2:
        raise ValueError("temperature must be between 0 and 2")
    if not isinstance(top_p, (int, float)) or not math.isfinite(top_p) or not 0 < top_p <= 1:
        raise ValueError("top_p must be greater than 0 and at most 1")
    return {"messages": messages, "tools": tools, "max_tokens": output_tokens, "temperature": temperature, "top_p": top_p}


def split_tool_calls(text: str) -> tuple[str, list]:
    calls = []
    for match in re.finditer(r"<tool_call>\s*([\s\S]*?)\s*</tool_call>", text):
        value = json.loads(match.group(1))
        if not isinstance(value, dict) or not isinstance(value.get("name"), str):
            raise ValueError("The model returned an invalid tool call")
        arguments = value.get("arguments", {})
        calls.append({"id": f"call_{uuid.uuid4().hex[:16]}", "type": "function", "function": {
            "name": value["name"], "arguments": arguments if isinstance(arguments, str) else json.dumps(arguments, ensure_ascii=False),
        }})
    return re.sub(r"<tool_call>[\s\S]*?</tool_call>", "", text).strip(), calls


def generation_end_token_ids(tokenizer, generation_config) -> list[int]:
    """End each chat turn at the tokenizer EOS or a checkpoint EOS."""
    chat_eos = tokenizer.eos_token_id
    if not isinstance(chat_eos, int) or isinstance(chat_eos, bool) or chat_eos < 0:
        raise ValueError("The tokenizer must define a valid chat EOS token ID")
    configured = generation_config.eos_token_id
    additional = configured if isinstance(configured, list) else [configured]
    return list(dict.fromkeys([chat_eos, *[
        token for token in additional if isinstance(token, int) and not isinstance(token, bool) and token >= 0
    ]]))


class GenerationBoundary:
    """Keep protocol turn boundaries out of text and speech, including split stream chunks."""

    def __init__(self, markers: tuple[str, ...] = ("<|im_end|>", "<|endoftext|>", "<|im_start|>")):
        if not markers or any(not marker for marker in markers):
            raise ValueError("Generation boundary markers must be nonempty")
        self.stopped = False
        self._buffer = ""
        self._markers = markers

    def consume(self, text: str) -> str:
        if self.stopped:
            return ""
        self._buffer += text
        positions = [position for marker in self._markers if (position := self._buffer.find(marker)) >= 0]
        if positions:
            result = self._buffer[:min(positions)]
            self._buffer = ""
            self.stopped = True
            return result

        # A streamer chunk can divide a protocol marker. Hold only its possible prefix.
        held = max((
            length
            for marker in self._markers
            for length in range(1, min(len(self._buffer), len(marker) - 1) + 1)
            if self._buffer.endswith(marker[:length])
        ), default=0)
        result = self._buffer[:-held] if held else self._buffer
        self._buffer = self._buffer[-held:] if held else ""
        return result

    def finish(self) -> str:
        result = self._buffer
        self._buffer = ""
        return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model-path", type=Path, default=ROOT / "local-models" / "qwen3.5-2b-bf16")
    parser.add_argument("--port", type=int, default=18083)
    parser.add_argument("--max-context", type=int, default=8192)
    parser.add_argument("--max-output", type=int, default=256)
    parser.add_argument("--evidence-file", type=Path, default=ROOT / "local-models" / "debug" / "qwen35-2b-bf16-runtime.json")
    parser.add_argument("--record-messages-dir", type=Path, help="Save complete local request and response JSON in this directory")
    parser.add_argument("--record-messages-days", type=int, default=7)
    parser.add_argument("--record-messages-max-files", type=int, default=1000)
    parser.add_argument("--release-cuda-cache", action="store_true", help="Release unused CUDA allocations after each request for a shared GPU")
    parser.add_argument("--verify-only", action="store_true")
    args = parser.parse_args()
    recorder = MessageRecorder(args.record_messages_dir, args.record_messages_days, args.record_messages_max_files)
    directory = args.model_path.resolve()
    if not args.verify_only:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            probe.bind(("127.0.0.1", args.port))
    print(json.dumps({"stage": "verify_checkpoint", "pid": os.getpid()}), flush=True)
    evidence = verify_checkpoint(directory)
    if args.verify_only:
        print(json.dumps(evidence, indent=2))
        return 0

    os.environ["HF_HUB_OFFLINE"] = "1"
    os.environ["TRANSFORMERS_OFFLINE"] = "1"
    print(json.dumps({"stage": "import_runtime"}), flush=True)
    import torch
    import transformers
    from transformers import AutoTokenizer, Qwen3_5ForConditionalGeneration, StoppingCriteria, TextIteratorStreamer

    if not torch.cuda.is_available() or not torch.cuda.is_bf16_supported():
        raise RuntimeError("This BF16 service requires a CUDA GPU with BF16 support")
    print(json.dumps({"stage": "load_tokenizer", "gpu": torch.cuda.get_device_name(0)}), flush=True)
    started = time.perf_counter()
    tokenizer = AutoTokenizer.from_pretrained(str(directory), local_files_only=True)
    print(json.dumps({"stage": "load_weights", "dtype": "torch.bfloat16", "device": "cuda:0"}), flush=True)
    model = Qwen3_5ForConditionalGeneration.from_pretrained(
        str(directory), dtype=torch.bfloat16, device_map="cuda:0", local_files_only=True,
    ).eval()
    torch.cuda.synchronize()
    dtype_counts = Counter(str(parameter.dtype) for parameter in model.parameters())
    device_counts = Counter(str(parameter.device) for parameter in model.parameters())
    if set(device_counts) != {"cuda:0"} or any(dtype not in {"torch.bfloat16", "torch.float32"} for dtype in dtype_counts):
        raise RuntimeError("Unexpected loaded weight device or dtype")
    if not dtype_counts["torch.bfloat16"] or getattr(model, "is_quantized", False):
        raise RuntimeError("The loaded model is not an unquantized BF16 model")
    end_token_ids = generation_end_token_ids(tokenizer, model.generation_config)
    evidence.update({
        "model_id": MODEL_ID, "pid": os.getpid(), "port": args.port,
        "torch": torch.__version__, "transformers": transformers.__version__,
        "gpu": torch.cuda.get_device_name(0), "bf16_supported": True,
        "runtime_parameter_dtype_counts": dict(dtype_counts), "runtime_parameter_device_counts": dict(device_counts),
        "parameter_count": sum(parameter.numel() for parameter in model.parameters()),
        "model_class": type(model).__name__, "load_seconds": round(time.perf_counter() - started, 3),
        "max_context": args.max_context, "max_output": args.max_output,
        "cuda_allocated_bytes": torch.cuda.memory_allocated(),
        "generation_end_token_ids": end_token_ids,
    })
    args.evidence_file.parent.mkdir(parents=True, exist_ok=True)
    args.evidence_file.write_text(json.dumps(evidence, ensure_ascii=False, indent=2), encoding="utf-8")
    lock = threading.Lock()

    class CancelGeneration(StoppingCriteria):
        def __init__(self, canceled: threading.Event):
            self.canceled = canceled

        def __call__(self, input_ids, scores, **kwargs):
            return self.canceled.is_set()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format, *values):
            # Request bodies and headers never enter the log.
            return

        def send_headers(self, status: int, content_type: str) -> None:
            self.last_response_status = status
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            origin = self.headers.get("Origin")
            if origin is not None and allows_browser_origin(origin):
                self.send_header("Access-Control-Allow-Origin", origin)
            self.send_header("Vary", "Origin")
            self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization, X-Humanning-Session-ID, X-Humanning-Turn-ID, X-Humanning-Message-ID")
            self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
            if getattr(self, "completion_id", None) is not None:
                self.send_header("X-Request-ID", self.completion_id)
                self.send_header("Access-Control-Expose-Headers", "X-Request-ID")
            self.end_headers()

        def response(self, status: int, value: dict) -> None:
            self.send_headers(status, "application/json; charset=utf-8")
            self.wfile.write(json.dumps(value, ensure_ascii=False).encode("utf-8"))

        def reject_foreign_origin(self) -> bool:
            if allows_browser_origin(self.headers.get("Origin")):
                return False
            self.response(403, {"error": {"message": "Only loopback HTTP(S) browser origins can use this local service."}})
            return True

        def do_OPTIONS(self) -> None:
            if self.reject_foreign_origin():
                return
            self.send_headers(204, "text/plain")

        def do_GET(self) -> None:
            if self.path == "/health":
                self.response(200, {"status": "ready", **evidence, "message_recording": recorder.describe(),
                                    "release_cuda_cache": args.release_cuda_cache,
                                    "gpu_memory": {"allocated_bytes": torch.cuda.memory_allocated(),
                                                   "reserved_bytes": torch.cuda.memory_reserved()}})
            elif self.path == "/v1/models":
                self.response(200, {"object": "list", "data": [{"id": MODEL_ID, "object": "model", "owned_by": "local"}]})
            else:
                self.response(404, {"error": {"message": "Unknown endpoint"}})

        def do_POST(self) -> None:
            if self.reject_foreign_origin():
                return
            if self.path == "/v1/debug/message-context":
                try:
                    length = int(self.headers.get("Content-Length", "0"))
                    if not 0 < length <= MAX_BODY_BYTES:
                        raise ValueError("The JSON request must be between 1 byte and 1 MiB")
                    query = json.loads(self.rfile.read(length), parse_constant=reject_nonfinite_json)
                    result = recorder.read_context(query)
                    self.response(200, result)
                except (ValueError, TypeError) as error:
                    self.response(400, {"error": {"message": str(error)}})
                return
            if self.path != "/v1/chat/completions":
                self.response(404, {"error": {"message": "Unknown endpoint"}})
                return
            completion_id = f"chatcmpl-{uuid.uuid4().hex}"
            self.completion_id = completion_id
            started_at = time.time()
            record = {
                "schema_version": 1, "request_id": completion_id,
                "started_at": datetime.fromtimestamp(started_at, timezone.utc).isoformat(),
                "started_at_unix": started_at, "status": "started",
                "endpoint": "/v1/chat/completions", "request": None,
                "response": None, "stream_events": [], "stream_done": False,
            }
            correlation = {key: self.headers.get(header, "") for key, header in (
                ("session_id", "X-Humanning-Session-ID"), ("turn_id", "X-Humanning-Turn-ID"),
                ("message_id", "X-Humanning-Message-ID"))}
            if all(re.fullmatch(r"[A-Za-z0-9_-]{1,128}", value) for value in correlation.values()):
                record["correlation"] = correlation

            def save_record(status: str, http_status: int, error=None) -> None:
                record.update(status=status, http_status=http_status,
                              finished_at=datetime.now(timezone.utc).isoformat(),
                              seconds=round(time.time() - started_at, 3))
                if error is not None:
                    record["error"] = error
                recorder.write(record)

            try:
                length = int(self.headers.get("Content-Length", "0"))
                if not 0 < length <= MAX_BODY_BYTES:
                    raise ValueError("The JSON request must be between 1 byte and 1 MiB")
                payload = json.loads(self.rfile.read(length), parse_constant=reject_nonfinite_json)
                record["request"] = payload
                recorder.write(record)
                request = validate_request(payload, args.max_output)
            except (ValueError, TypeError, KeyError) as error:
                result = {"error": {"message": str(error), "type": "invalid_request_error"}}
                record["response"] = result
                save_record("error", 400, result["error"])
                self.response(400, result)
                return
            if not lock.acquire(blocking=False):
                result = {"error": {"message": "The local GPU is busy. Retry after the current request."}}
                record["response"] = result
                save_record("error", 503, result["error"])
                self.response(503, result)
                return
            streaming = bool(payload.get("stream", False))
            canceled = threading.Event()
            worker = None
            stream_started = False
            generation_error = []
            generated_tokens = []
            request_started = time.perf_counter()
            created = int(started_at)
            output_text = ""
            raw_output_text = ""
            boundary = GenerationBoundary()
            final_status = "error"
            final_http_status = 500
            inputs = None
            output = None

            def send_event(delta: dict, finish_reason=None, usage=None) -> None:
                event = {"id": completion_id, "object": "chat.completion.chunk", "created": created, "model": MODEL_ID,
                         "choices": [{"index": 0, "delta": delta, "finish_reason": finish_reason}]}
                if usage is not None:
                    event["usage"] = usage
                self.wfile.write(("data: " + json.dumps(event, ensure_ascii=False) + "\n\n").encode("utf-8"))
                self.wfile.flush()
                if recorder.directory is not None:
                    record["stream_events"].append(event)

            try:
                inputs = tokenizer.apply_chat_template(
                    request["messages"], tools=request["tools"] or None, add_generation_prompt=True,
                    enable_thinking=False, tokenize=True, return_dict=True, return_tensors="pt",
                ).to("cuda:0")
                prompt_tokens = inputs["input_ids"].shape[-1]
                if recorder.directory is not None:
                    record["model_input"] = {
                        **request, "enable_thinking": False, "add_generation_prompt": True,
                        "prompt_tokens": int(prompt_tokens),
                        "rendered_prompt": tokenizer.decode(inputs["input_ids"][0], skip_special_tokens=False),
                    }
                    recorder.write(record)
                if prompt_tokens + request["max_tokens"] > args.max_context:
                    raise ValueError(
                        f"Prompt ({prompt_tokens}) and requested output ({request['max_tokens']}) "
                        f"exceed the {args.max_context}-token runtime limit"
                    )
                options = {"max_new_tokens": request["max_tokens"], "do_sample": request["temperature"] > 0,
                           "use_cache": True, "eos_token_id": end_token_ids,
                           "stopping_criteria": [CancelGeneration(canceled)]}
                if options["do_sample"]:
                    options.update(temperature=request["temperature"], top_p=request["top_p"])
                if recorder.directory is not None:
                    record["model_input"]["generation_options"] = {
                        key: value for key, value in options.items() if key != "stopping_criteria"
                    }
                    recorder.write(record)
                if streaming:
                    streamer = TextIteratorStreamer(tokenizer, skip_prompt=True, skip_special_tokens=False, timeout=1)

                    def generate() -> None:
                        try:
                            with torch.inference_mode():
                                output = model.generate(**inputs, **options, streamer=streamer)
                            generated_tokens.append(int(output.shape[-1] - prompt_tokens))
                        except Exception as error:
                            generation_error.append(type(error).__name__)
                            streamer.end()

                    self.send_headers(200, "text/event-stream; charset=utf-8")
                    stream_started = True
                    send_event({"role": "assistant"})
                    worker = threading.Thread(target=generate, daemon=True)
                    worker.start()
                    output_text = ""
                    # Tool responses are buffered until their JSON envelope is complete.
                    # Ordinary text replies stream as the model produces text chunks.
                    while worker.is_alive() or not streamer.text_queue.empty():
                        try:
                            part = next(streamer)
                        except queue.Empty:
                            continue
                        except StopIteration:
                            break
                        raw_output_text += part
                        clean = boundary.consume(part)
                        output_text += clean
                        if boundary.stopped:
                            canceled.set()
                        if not request["tools"]:
                            if clean:
                                send_event({"content": clean})
                    worker.join()
                    if generation_error:
                        raise RuntimeError(generation_error[0])
                    tail = boundary.finish()
                    output_text += tail
                    if tail and not request["tools"]:
                        send_event({"content": tail})
                else:
                    with torch.inference_mode():
                        output = model.generate(**inputs, **options)
                    generated_tokens.append(int(output.shape[-1] - prompt_tokens))
                    raw_output_text = tokenizer.decode(output[0, prompt_tokens:], skip_special_tokens=False)
                    output_text = boundary.consume(raw_output_text) + boundary.finish()
                content, calls = split_tool_calls(output_text)
                count = generated_tokens[0]
                finish = "tool_calls" if calls else ("length" if count >= request["max_tokens"] and not boundary.stopped else "stop")
                usage = {"prompt_tokens": int(prompt_tokens), "completion_tokens": count, "total_tokens": int(prompt_tokens) + count}
                message = {"role": "assistant", "content": content or None}
                if calls:
                    message["tool_calls"] = calls
                record["response"] = {
                    "id": completion_id, "object": "chat.completion", "created": created, "model": MODEL_ID,
                    "choices": [{"index": 0, "message": message, "finish_reason": finish}], "usage": usage,
                }
                if streaming:
                    if request["tools"] and content:
                        send_event({"content": content})
                    if calls:
                        send_event({"tool_calls": [{"index": index, **call} for index, call in enumerate(calls)]})
                    send_event({}, finish, usage)
                    self.wfile.write(b"data: [DONE]\n\n")
                    self.wfile.flush()
                    record["stream_done"] = True
                else:
                    self.response(200, record["response"])
                final_status = "ok"
                final_http_status = 200
                print(json.dumps({"request_id": completion_id, "stream": streaming, **usage,
                                  "seconds": round(time.perf_counter() - request_started, 3), "status": "ok"}), flush=True)
            except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                canceled.set()
                final_status = "canceled"
                final_http_status = getattr(self, "last_response_status", 499)
                record["error"] = {"type": "client_disconnected", "message": "The client disconnected before completion."}
            except Exception as error:
                canceled.set()
                error_type = generation_error[0] if generation_error else type(error).__name__
                message = str(error) if isinstance(error, ValueError) else f"Generation failed: {error_type}. Read runtime metadata and GPU memory."
                result = {"error": {"message": message, "type": error_type}}
                record["response"] = result
                record["error"] = result["error"]
                final_http_status = 200 if stream_started else (400 if isinstance(error, ValueError) else 500)
                if stream_started:
                    try:
                        event = {"error": {"message": message}}
                        self.wfile.write(("data: " + json.dumps(event) + "\n\n").encode("utf-8"))
                        self.wfile.write(b"data: [DONE]\n\n")
                        self.wfile.flush()
                        if recorder.directory is not None:
                            record["stream_events"].append(event)
                        record["stream_done"] = True
                    except OSError:
                        pass
                else:
                    self.response(final_http_status, result)
                print(json.dumps({"request_id": completion_id, "status": "error", "error_type": error_type}), flush=True)
            finally:
                canceled.set()
                if worker is not None and worker.ident is not None:
                    worker.join()
                try:
                    if args.release_cuda_cache:
                        inputs = None
                        output = None
                        gc.collect()
                        torch.cuda.empty_cache()
                finally:
                    lock.release()
                if recorder.directory is not None:
                    record["model_output_text"] = output_text
                    record["model_output_raw_text"] = raw_output_text
                save_record(final_status, final_http_status)

    server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    server.daemon_threads = True
    print(json.dumps({"status": "ready", "base_url": f"http://127.0.0.1:{args.port}/v1", "model_id": MODEL_ID,
                      "runtime_parameter_dtype_counts": dict(dtype_counts), "evidence_file": str(args.evidence_file)}), flush=True)
    server.serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
