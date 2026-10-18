"""Proxy local Gemma inference and keep optional exact request records."""

import argparse
import http.client
import importlib.util
import json
import select
import socket
import threading
import time
import uuid
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit


ROOT = Path(__file__).resolve().parents[1]
MODEL_ID = "gemma4-e2b-int4"
MAX_BODY_BYTES = 1024 * 1024
SPEC = importlib.util.spec_from_file_location("local_qwen_protocol", ROOT / "scripts/serve-qwen35-2b-bf16.py")
SHARED = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(SHARED)


class UpstreamProtocolError(ValueError):
    """The local model returned invalid JSON, SSE, or a generation error."""


def upstream_json(raw: bytes) -> dict:
    try:
        value = json.loads(raw, parse_constant=SHARED.reject_nonfinite_json)
    except (ValueError, UnicodeError, RecursionError) as error:
        raise UpstreamProtocolError("The Gemma upstream returned invalid JSON") from error
    if not isinstance(value, dict):
        raise UpstreamProtocolError("The Gemma upstream must return a JSON object")
    return value


def validate_request(payload: object, max_output: int) -> dict:
    """Keep the original request in records. Forward only supported local parameters."""
    if not isinstance(payload, dict) or payload.get("model") != MODEL_ID:
        raise ValueError(f"model must be {MODEL_ID}")
    checked = SHARED.validate_request({**payload, "model": SHARED.MODEL_ID}, max_output)
    if "stream" in payload and not isinstance(payload["stream"], bool):
        raise ValueError("stream must be a boolean")
    result = {**checked, "model": MODEL_ID, "stream": payload.get("stream", False),
              "chat_template_kwargs": {"enable_thinking": False}}
    if result["stream"]:
        result["stream_options"] = {"include_usage": True}
    if "tool_choice" in payload:
        choice = payload["tool_choice"]
        if choice not in ("auto", "none", "required") and not isinstance(choice, dict):
            raise ValueError("tool_choice must select a function or use auto, none, or required")
        result["tool_choice"] = choice
    return result


class CompletionAccumulator:
    """Aggregate received OpenAI stream events without changing the client stream."""

    def __init__(self):
        self.metadata = {}
        self.text = ""
        self.reasoning = ""
        self.calls = {}
        self.finish_reason = None
        self.usage = None

    def consume(self, event: dict) -> None:
        self.metadata.update({key: event[key] for key in ("id", "created", "model", "system_fingerprint") if key in event})
        if isinstance(event.get("usage"), dict):
            self.usage = event["usage"]
        choices = event.get("choices", [])
        if not isinstance(choices, list):
            raise UpstreamProtocolError("The Gemma upstream choices must be a list")
        for choice in choices:
            if not isinstance(choice, dict) or choice.get("index") != 0:
                continue
            if choice.get("finish_reason") is not None:
                self.finish_reason = choice["finish_reason"]
            delta = choice.get("delta", {})
            if not isinstance(delta, dict):
                continue
            if isinstance(delta.get("content"), str):
                self.text += delta["content"]
            if isinstance(delta.get("reasoning_content"), str):
                self.reasoning += delta["reasoning_content"]
            parts = delta.get("tool_calls", [])
            if not isinstance(parts, list):
                raise UpstreamProtocolError("The Gemma upstream tool calls must be a list")
            for part in parts:
                if not isinstance(part, dict) or not isinstance(part.get("index"), int):
                    continue
                call = self.calls.setdefault(part["index"], {"type": "function", "function": {"name": "", "arguments": ""}})
                for key in ("id", "type"):
                    if key in part:
                        call[key] = part[key]
                function = part.get("function", {})
                if not isinstance(function, dict):
                    raise UpstreamProtocolError("The Gemma upstream function must be an object")
                for key in ("name", "arguments"):
                    if isinstance(function.get(key), str):
                        call["function"][key] += function[key]

    def response(self) -> dict:
        message = {"role": "assistant", "content": self.text or None}
        if self.reasoning:
            message["reasoning_content"] = self.reasoning
        if self.calls:
            message["tool_calls"] = [self.calls[index] for index in sorted(self.calls)]
        result = {**self.metadata, "object": "chat.completion", "choices": [
            {"index": 0, "message": message, "finish_reason": self.finish_reason}]}
        if self.usage is not None:
            result["usage"] = self.usage
        return result


class GemmaUpstream:
    """Use one configured loopback server. Never forward client URLs or credentials."""

    def __init__(self, url: str):
        parsed = urlsplit(url)
        if (parsed.scheme != "http" or parsed.hostname not in {"127.0.0.1", "localhost", "::1"}
                or parsed.username or parsed.password or parsed.path not in ("", "/") or parsed.query or parsed.fragment):
            raise ValueError("The Gemma upstream must be an HTTP loopback server without credentials or a path")
        self.host = parsed.hostname
        self.port = parsed.port or 80

    def connection(self, timeout: float = 120) -> http.client.HTTPConnection:
        return http.client.HTTPConnection(self.host, self.port, timeout=timeout)

    def json(self, method: str, path: str, value=None, timeout: float = 5) -> tuple[int, dict]:
        connection = self.connection(timeout)
        try:
            body = json.dumps(value, ensure_ascii=False, allow_nan=False).encode("utf-8") if value is not None else None
            connection.request(method, path, body, {"Content-Type": "application/json"})
            response = connection.getresponse()
            raw = response.read(32 * MAX_BODY_BYTES + 1)
            if len(raw) > 32 * MAX_BODY_BYTES:
                raise UpstreamProtocolError("The Gemma upstream JSON response exceeds 32 MiB")
            return response.status, upstream_json(raw)
        finally:
            connection.close()


def create_server(port: int, upstream_url: str, recorder, max_output: int = 512) -> ThreadingHTTPServer:
    upstream = GemmaUpstream(upstream_url)

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            return

        def send_headers(self, status: int, content_type: str) -> None:
            self.last_response_status = status
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            origin = self.headers.get("Origin")
            if origin is not None and SHARED.allows_browser_origin(origin):
                self.send_header("Access-Control-Allow-Origin", origin)
            self.send_header("Vary", "Origin")
            self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization, X-Humanning-Session-ID, X-Humanning-Turn-ID, X-Humanning-Message-ID")
            self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
            self.send_header("Connection", "close")
            if getattr(self, "completion_id", None):
                self.send_header("X-Request-ID", self.completion_id)
                self.send_header("Access-Control-Expose-Headers", "X-Request-ID")
            self.end_headers()
            self.close_connection = True

        def response(self, status: int, value: dict) -> None:
            self.send_headers(status, "application/json; charset=utf-8")
            self.wfile.write(json.dumps(value, ensure_ascii=False, allow_nan=False).encode("utf-8"))
            self.wfile.flush()

        def reject_foreign_origin(self) -> bool:
            if SHARED.allows_browser_origin(self.headers.get("Origin")):
                return False
            self.response(403, {"error": {"message": "Only loopback HTTP(S) browser origins can use this local service."}})
            return True

        def read_json(self) -> object:
            length = int(self.headers.get("Content-Length", "0"))
            if not 0 < length <= MAX_BODY_BYTES:
                raise ValueError("The JSON request must be between 1 byte and 1 MiB")
            return json.loads(self.rfile.read(length), parse_constant=SHARED.reject_nonfinite_json)

        def lookup(self, query: dict) -> None:
            result = recorder.read_context(query)
            self.response(200, result)

        def do_OPTIONS(self) -> None:
            if not self.reject_foreign_origin():
                self.send_headers(204, "text/plain")

        def do_GET(self) -> None:
            if self.reject_foreign_origin():
                return
            parsed = urlsplit(self.path)
            if parsed.path == "/v1/debug/message-context":
                try:
                    parameters = parse_qs(parsed.query, keep_blank_values=True)
                    if any(len(values) != 1 for values in parameters.values()):
                        raise ValueError("Each message query field requires one value")
                    query = {key: values[0] for key, values in parameters.items()}
                    if "created_at" in query:
                        query["created_at"] = float(query["created_at"])
                    self.lookup(query)
                except (ValueError, TypeError) as error:
                    self.response(400, {"error": {"message": str(error)}})
                return
            if self.path == "/health":
                try:
                    status, health = upstream.json("GET", "/health", timeout=2)
                    ready = status == 200 and health.get("status") in {"ok", "ready"}
                except (OSError, ValueError, http.client.HTTPException):
                    ready = False
                self.response(200 if ready else 503, {"status": "ready" if ready else "upstream-unavailable",
                    "model_id": MODEL_ID, "runtime": "llama.cpp", "quantization": "QAT Q4_0",
                    "service_modalities": ["text"], "message_recording": recorder.describe()})
            elif self.path == "/v1/models":
                self.response(200, {"object": "list", "data": [{"id": MODEL_ID, "object": "model", "owned_by": "local"}]})
            else:
                self.response(404, {"error": {"message": "Unknown endpoint"}})

        def do_POST(self) -> None:
            if self.reject_foreign_origin():
                return
            if self.path == "/v1/debug/message-context":
                try:
                    self.lookup(self.read_json())
                except (ValueError, TypeError) as error:
                    self.response(400, {"error": {"message": str(error)}})
                return
            if self.path != "/v1/chat/completions":
                self.response(404, {"error": {"message": "Unknown endpoint"}})
                return
            self.completion_id = f"chatcmpl-{uuid.uuid4().hex}"
            started = time.time()
            record = {"schema_version": 1, "request_id": self.completion_id, "started_at_unix": started,
                "started_at": datetime.fromtimestamp(started, timezone.utc).isoformat(), "status": "started",
                "endpoint": "/v1/chat/completions", "request": None, "response": None,
                "stream_events": [], "stream_done": False, "model_output_raw_text": ""}
            correlation = {key: self.headers.get(header) for key, header in (
                ("message_id", "X-Humanning-Message-ID"), ("session_id", "X-Humanning-Session-ID"), ("turn_id", "X-Humanning-Turn-ID"))}
            if all(isinstance(value, str) and value and len(value) <= 512 for value in correlation.values()):
                record["correlation"] = correlation
            connection = None
            upstream_response = None
            upstream_socket = None
            finished = threading.Event()
            canceled = threading.Event()
            monitor = None
            stream_started = False
            final_status, final_http = "error", 500
            accumulator = CompletionAccumulator()

            def watch_client():
                while not finished.wait(0.05):
                    try:
                        readable, _, _ = select.select([self.connection], [], [], 0)
                        if not readable or self.connection.recv(1, socket.MSG_PEEK):
                            continue
                    except OSError:
                        pass
                    canceled.set()
                    if upstream_socket is not None:
                        try:
                            upstream_socket.shutdown(socket.SHUT_RDWR)
                        except OSError:
                            pass
                    return

            try:
                payload = self.read_json()
                record["request"] = payload
                recorder.write(record)
                request = validate_request(payload, max_output)
                record["model_input"] = {**request, "rendered_prompt": None, "rendered_prompt_status": "not-recorded"}
                if recorder.directory is not None:
                    try:
                        status, template = upstream.json("POST", "/apply-template", request)
                        if status != 200 or not isinstance(template.get("prompt"), str):
                            raise ValueError("The upstream template endpoint did not return a prompt")
                        record["model_input"].update(rendered_prompt=template["prompt"], rendered_prompt_status="available",
                                                     rendered_prompt_source="llama.cpp /apply-template")
                    except (OSError, ValueError, http.client.HTTPException) as error:
                        record["model_input"].update(rendered_prompt_status="unavailable", rendered_prompt_error=type(error).__name__)
                    recorder.write(record)
                connection = upstream.connection()
                connection.connect()
                upstream_socket = connection.sock
                monitor = threading.Thread(target=watch_client, daemon=True)
                monitor.start()
                encoded = json.dumps(request, ensure_ascii=False, allow_nan=False).encode("utf-8")
                connection.request("POST", "/v1/chat/completions", encoded, {"Content-Type": "application/json"})
                upstream_response = connection.getresponse()
                final_http = upstream_response.status
                record["upstream_http_status"] = final_http
                if not request["stream"] or final_http != 200:
                    raw = upstream_response.read(32 * MAX_BODY_BYTES + 1)
                    if len(raw) > 32 * MAX_BODY_BYTES:
                        raise UpstreamProtocolError("The Gemma upstream JSON response exceeds 32 MiB")
                    result = upstream_json(raw)
                    record["response"] = result
                    if result.get("choices"):
                        choices = result["choices"]
                        if not isinstance(choices, list) or not isinstance(choices[0], dict) or not isinstance(choices[0].get("message"), dict):
                            raise UpstreamProtocolError("The Gemma upstream returned an invalid completion")
                        record["model_output_raw_text"] = choices[0]["message"].get("content") or ""
                    if final_http == 200 and "error" in result:
                        final_http = 502
                    self.response(final_http, result)
                    final_status = "ok" if final_http == 200 else "error"
                else:
                    self.send_headers(200, "text/event-stream; charset=utf-8")
                    stream_started = True
                    while not canceled.is_set():
                        line = upstream_response.readline(MAX_BODY_BYTES + 1)
                        if not line:
                            break
                        if len(line) > MAX_BODY_BYTES:
                            raise UpstreamProtocolError("The Gemma upstream SSE line exceeds 1 MiB")
                        if record.get("first_response_seconds") is None and line.startswith(b"data:"):
                            record["first_response_seconds"] = round(time.time() - started, 4)
                        if not line.startswith(b"data:"):
                            self.wfile.write(line)
                            self.wfile.flush()
                            continue
                        data = line[5:].strip()
                        if data == b"[DONE]":
                            self.wfile.write(line + b"\n")
                            self.wfile.flush()
                            record["stream_done"] = True
                            break
                        event = upstream_json(data)
                        if recorder.directory is not None:
                            record["stream_events"].append(event)
                        if "error" in event:
                            record["upstream_error"] = event["error"]
                            raise UpstreamProtocolError("The Gemma upstream returned a streaming error")
                        accumulator.consume(event)
                        self.wfile.write(line)
                        self.wfile.flush()
                    if canceled.is_set():
                        raise ConnectionAbortedError("The client disconnected")
                    if not record["stream_done"]:
                        raise UpstreamProtocolError("The Gemma upstream stream ended without DONE")
                    final_status = "ok"
            except (ValueError, TypeError, KeyError, OSError, http.client.HTTPException) as error:
                disconnected = canceled.is_set() or isinstance(error, (BrokenPipeError, ConnectionResetError, ConnectionAbortedError))
                final_status = "canceled" if disconnected else "error"
                final_http = 499 if disconnected else 502 if isinstance(error, UpstreamProtocolError) else 400 if isinstance(error, (ValueError, TypeError, KeyError)) else 502
                record["error"] = {"type": "client_disconnected" if disconnected else type(error).__name__,
                                   "message": "The client disconnected before completion." if disconnected else str(error)}
                if not disconnected:
                    result = {"error": record["error"]}
                    try:
                        if stream_started:
                            self.wfile.write(("data: " + json.dumps(result) + "\n\ndata: [DONE]\n\n").encode("utf-8"))
                            self.wfile.flush()
                        else:
                            record["response"] = result
                            self.response(final_http, result)
                    except OSError:
                        final_status = "canceled"
            finally:
                finished.set()
                if upstream_response is not None:
                    upstream_response.close()
                if connection is not None:
                    connection.close()
                if monitor is not None:
                    monitor.join(timeout=0.2)
                if stream_started:
                    record["response"] = accumulator.response()
                    record["response_source"] = "aggregated_upstream_stream_events"
                    record["model_output_raw_text"] = accumulator.text
                record["model_output_text"] = record["model_output_raw_text"]
                record["model_output_source"] = "upstream_response_content"
                record.update(status=final_status, http_status=getattr(self, "last_response_status", final_http), finished_at=datetime.now(timezone.utc).isoformat(),
                              seconds=round(time.time() - started, 4))
                recorder.write(record)

    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    server.daemon_threads = True
    return server


def warmup(upstream_url: str, prompt_root: Path) -> float:
    """Warm the public persona prefix without creating a conversation record."""
    started = time.perf_counter()
    prompt = "\n".join((prompt_root / name).read_text(encoding="utf-8").strip() for name in (
        "local-companion.zh-CN.md", "local-memory-policy.zh-CN.md"))
    status, result = GemmaUpstream(upstream_url).json("POST", "/v1/chat/completions", {
        "model": MODEL_ID, "messages": [{"role": "system", "content": prompt}, {"role": "user", "content": "只回复一个字：好"}],
        "stream": False, "max_tokens": 1, "temperature": 0, "chat_template_kwargs": {"enable_thinking": False},
    }, timeout=150)
    if status != 200 or not isinstance(result.get("choices"), list) or not result["choices"] or "error" in result:
        raise RuntimeError("Local Gemma warmup failed. Read the native model log.")
    return round(time.perf_counter() - started, 3)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=18083)
    parser.add_argument("--upstream", default="http://127.0.0.1:18088")
    parser.add_argument("--max-output", type=int, default=512)
    parser.add_argument("--record-messages-dir", type=Path)
    parser.add_argument("--record-messages-days", type=int, default=7)
    parser.add_argument("--record-messages-max-files", type=int, default=1000)
    parser.add_argument("--warmup", action="store_true", help="Warm local inference before the gateway becomes ready")
    args = parser.parse_args()
    recorder = SHARED.MessageRecorder(args.record_messages_dir, args.record_messages_days, args.record_messages_max_files)
    if args.warmup:
        prompt_root = ROOT / "packages/stage-ui/src/constants/prompts"
        seconds = warmup(args.upstream, prompt_root)
        print(json.dumps({"stage": "warmup_complete", "seconds": seconds}), flush=True)
    server = create_server(args.port, args.upstream, recorder, args.max_output)
    print(json.dumps({"status": "listening", "model_id": MODEL_ID, "port": args.port, "upstream": args.upstream}), flush=True)
    server.serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
