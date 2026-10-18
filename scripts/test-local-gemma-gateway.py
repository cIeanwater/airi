"""Check the Gemma gateway against a synthetic local upstream without a model."""

import http.client
import importlib.util
import json
import select
import socket
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("local_gemma_gateway", ROOT / "scripts/serve-local-gemma.py")
GATEWAY = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(GATEWAY)


class FakeUpstream(BaseHTTPRequestHandler):
    def log_message(self, *_args):
        return

    def send_json(self, status, value):
        payload = json.dumps(value, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_GET(self):
        if self.path == "/health":
            self.send_json(200 if self.server.ready else 503, {"status": "ok" if self.server.ready else "loading"})
        elif self.path == "/v1/models":
            self.send_json(200, {"data": [{"id": GATEWAY.MODEL_ID}]})
        else:
            self.send_json(404, {})

    def do_POST(self):
        payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        self.server.requests.append((self.path, payload, dict(self.headers)))
        if self.path == "/apply-template":
            if self.server.template_missing:
                self.send_json(404, {"error": "template unavailable"})
            else:
                self.send_json(200, {"prompt": "EXACT TEMPLATE " + json.dumps(payload["messages"], ensure_ascii=False)})
            return
        if self.server.reply_mode == "before-headers":
            self.server.started.set()
            self.wait_for_cancel()
            return
        if self.server.reply_mode == "http-error":
            self.send_json(503, {"error": {"message": "Synthetic model unavailable"}})
            return
        if self.server.reply_mode == "json-error":
            self.send_json(200, {"error": {"message": "Synthetic generation error"}})
            return
        if self.server.reply_mode == "invalid-json":
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"{invalid")
            return
        if not payload.get("stream"):
            self.send_json(200, {"id": "upstream-actual", "object": "chat.completion", "created": int(time.time()),
                                 "model": GATEWAY.MODEL_ID, "choices": [{"index": 0, "message": {"role": "assistant", "content": self.server.output}, "finish_reason": "stop"}],
                                 "usage": {"prompt_tokens": 12, "completion_tokens": 4, "total_tokens": 16}})
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()
        if self.server.reply_mode == "stream-error":
            self.wfile.write(b'data: {"error":{"message":"Synthetic generation error"}}\n\ndata: [DONE]\n\n')
            self.wfile.flush()
            return
        if self.server.reply_mode == "invalid-stream":
            self.wfile.write(b"data: not-json\n\n")
            self.wfile.flush()
            return
        self.send_event({"content": self.server.output})
        if self.server.reply_mode == "truncated-stream":
            return
        if self.server.silent:
            self.server.started.set()
            self.wait_for_cancel()
            return
        self.server.first_chunk.set()
        self.server.continue_stream.wait(3)
        self.send_event({}, "stop", {"prompt_tokens": 12, "completion_tokens": 4, "total_tokens": 16})
        self.wfile.write(b"data: [DONE]\n\n")
        self.wfile.flush()

    def wait_for_cancel(self):
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            ready, _, _ = select.select([self.connection], [], [], 0.05)
            if ready and self.connection.recv(1, socket.MSG_PEEK) == b"":
                self.server.canceled.set()
                return

    def send_event(self, delta, finish=None, usage=None):
        value = {"id": "upstream-actual", "object": "chat.completion.chunk", "created": int(time.time()),
                 "model": GATEWAY.MODEL_ID, "choices": [{"index": 0, "delta": delta, "finish_reason": finish}]}
        if usage:
            value["usage"] = usage
        self.wfile.write(("data: " + json.dumps(value, ensure_ascii=False) + "\n\n").encode("utf-8"))
        self.wfile.flush()


class GemmaGatewayTests(unittest.TestCase):
    def setUp(self):
        temporary_root = ROOT / ".temp"
        temporary_root.mkdir(exist_ok=True)
        self.temporary = tempfile.TemporaryDirectory(prefix="gemma-gateway-", dir=temporary_root)
        self.directory = Path(self.temporary.name)
        self.addCleanup(self.temporary.cleanup)
        self.upstream = ThreadingHTTPServer(("127.0.0.1", 0), FakeUpstream)
        self.upstream.daemon_threads = True
        self.upstream.ready = True
        self.upstream.template_missing = False
        self.upstream.output = '<|ACT {"emotion":"happy"}|>合成回复 user assistant'
        self.upstream.requests = []
        self.upstream.silent = False
        self.upstream.reply_mode = None
        self.upstream.started = threading.Event()
        self.upstream.canceled = threading.Event()
        self.upstream.first_chunk = threading.Event()
        self.upstream.continue_stream = threading.Event()
        self.upstream.continue_stream.set()
        self.recorder = GATEWAY.SHARED.MessageRecorder(self.directory, 7, 1000)
        self.gateway = GATEWAY.create_server(0, f"http://127.0.0.1:{self.upstream.server_port}", self.recorder)
        for server in (self.upstream, self.gateway):
            threading.Thread(target=server.serve_forever, daemon=True).start()
            self.addCleanup(server.server_close)
            self.addCleanup(server.shutdown)

    def connection(self):
        client = http.client.HTTPConnection("127.0.0.1", self.gateway.server_port, timeout=5)
        self.addCleanup(client.close)
        return client

    def payload(self, **fields):
        return {"model": GATEWAY.MODEL_ID, "messages": [{"role": "system", "content": "合成系统消息"}, {"role": "user", "content": "你好 user assistant"}], **fields}

    def chat(self, payload=None, headers=None):
        client = self.connection()
        client.request("POST", "/v1/chat/completions", json.dumps(payload or self.payload()),
                       {"Content-Type": "application/json", **(headers or {})})
        return client, client.getresponse()

    def record(self):
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            paths = list(self.directory.glob("*.json"))
            if paths:
                value = json.loads(paths[0].read_text(encoding="utf-8"))
                if value.get("status") != "started":
                    return value
            time.sleep(0.01)
        self.fail("The gateway did not finish its record")

    def test_nonstream_records_exact_request_template_output_and_correlation(self):
        payload = self.payload(api_key="secret", baseURL="https://foreign.invalid", chat_template_kwargs={"enable_thinking": True})
        _, response = self.chat(payload, {"Authorization": "Bearer private", "X-Humanning-Message-ID": "reply-a", "X-Humanning-Session-ID": "session-a", "X-Humanning-Turn-ID": "turn-a"})
        result = json.loads(response.read())
        self.assertEqual(response.status, 200)
        self.assertEqual(result["id"], "upstream-actual")
        self.assertEqual(result["choices"][0]["message"]["content"], self.upstream.output)
        record = self.record()
        self.assertEqual(record["request"], payload)
        self.assertEqual(record["response"], result)
        self.assertEqual(record["correlation"], {"message_id": "reply-a", "session_id": "session-a", "turn_id": "turn-a"})
        self.assertTrue(record["model_input"]["rendered_prompt"].startswith("EXACT TEMPLATE "))
        self.assertEqual(record["model_input"]["messages"], payload["messages"])
        self.assertNotIn("Authorization", json.dumps(record))
        for _, forwarded, headers in self.upstream.requests:
            self.assertNotIn("api_key", forwarded)
            self.assertNotIn("baseURL", forwarded)
            self.assertFalse(forwarded["chat_template_kwargs"]["enable_thinking"])
            self.assertNotIn("Authorization", headers)

    def test_stream_forwards_first_event_before_generation_finishes(self):
        self.upstream.continue_stream.clear()
        _, response = self.chat(self.payload(stream=True))
        line = response.readline().decode("utf-8")
        event = json.loads(line.removeprefix("data: "))
        self.assertEqual(event["choices"][0]["delta"]["content"], self.upstream.output)
        self.assertFalse(self.upstream.continue_stream.is_set())
        self.upstream.continue_stream.set()
        remaining = response.read().decode("utf-8")
        self.assertIn("[DONE]", remaining)
        self.assertTrue(remaining.endswith("[DONE]\n\n"))
        record = self.record()
        self.assertTrue(record["stream_done"])
        self.assertEqual(record["model_output_raw_text"], self.upstream.output)
        self.assertEqual(record["response"]["usage"]["total_tokens"], 16)
        self.assertEqual(len(record["stream_events"]), 2)

    def test_client_abort_closes_silent_upstream_and_marks_canceled(self):
        self.upstream.silent = True
        client, response = self.chat(self.payload(stream=True))
        self.assertTrue(response.readline().startswith(b"data:"))
        response.close()
        client.close()
        self.assertTrue(self.upstream.canceled.wait(1.5), "Cancel did not reach the upstream while its stream was silent")
        self.assertEqual(self.record()["status"], "canceled")

    def test_client_abort_before_first_response_cancels_prompt_processing(self):
        self.upstream.reply_mode = "before-headers"
        client = self.connection()
        client.request("POST", "/v1/chat/completions", json.dumps(self.payload(stream=True)), {"Content-Type": "application/json"})
        self.assertTrue(self.upstream.started.wait(1.5))
        client.close()
        self.assertTrue(self.upstream.canceled.wait(1.5), "Cancel did not reach the upstream before response headers")
        self.assertEqual(self.record()["status"], "canceled")

    def test_upstream_http_and_json_errors_do_not_report_success(self):
        for mode, expected_status in (("http-error", 503), ("json-error", 502), ("invalid-json", 502)):
            with self.subTest(mode=mode):
                self.upstream.reply_mode = mode
                for path in self.directory.glob("*.json"):
                    path.unlink()
                _, response = self.chat()
                self.assertEqual(response.status, expected_status)
                self.assertIn("error", json.loads(response.read()))
                self.assertEqual(self.record()["status"], "error")

    def test_stream_errors_preserve_trace_and_finish_client_once(self):
        self.upstream.reply_mode = "stream-error"
        _, response = self.chat(self.payload(stream=True))
        output = response.read().decode("utf-8")
        self.assertEqual(response.status, 200)
        self.assertEqual(output.count("[DONE]"), 1)
        self.assertIn('"error"', output)
        record = self.record()
        self.assertEqual(record["status"], "error")
        self.assertFalse(record["stream_done"])
        self.assertEqual(record["upstream_error"], {"message": "Synthetic generation error"})
        self.assertEqual(record["stream_events"], [{"error": {"message": "Synthetic generation error"}}])

    def test_invalid_and_truncated_streams_are_upstream_failures(self):
        for mode in ("invalid-stream", "truncated-stream"):
            with self.subTest(mode=mode):
                self.upstream.reply_mode = mode
                for path in self.directory.glob("*.json"):
                    path.unlink()
                _, response = self.chat(self.payload(stream=True))
                output = response.read().decode("utf-8")
                self.assertEqual(output.count("[DONE]"), 1)
                record = self.record()
                self.assertEqual(record["status"], "error")
                self.assertEqual(record["http_status"], 200)
                self.assertEqual(record["upstream_http_status"], 200)
                self.assertEqual(record["error"]["type"], "UpstreamProtocolError")
                self.assertFalse(record["stream_done"])

    def test_warmup_uses_public_persona_and_never_records_a_chat(self):
        prompt_root = self.directory / "prompts"
        prompt_root.mkdir()
        (prompt_root / "local-companion.zh-CN.md").write_text("Public persona", encoding="utf-8")
        (prompt_root / "local-memory-policy.zh-CN.md").write_text("Public memory policy", encoding="utf-8")
        seconds = GATEWAY.warmup(f"http://127.0.0.1:{self.upstream.server_port}", prompt_root)
        self.assertGreaterEqual(seconds, 0)
        path, payload, _ = self.upstream.requests[-1]
        self.assertEqual(path, "/v1/chat/completions")
        self.assertEqual(payload["messages"][0], {"role": "system", "content": "Public persona\nPublic memory policy"})
        self.assertEqual(payload["max_tokens"], 1)
        self.assertFalse(payload["chat_template_kwargs"]["enable_thinking"])
        self.assertEqual(list(self.directory.glob("*.json")), [])
        self.upstream.reply_mode = "json-error"
        with self.assertRaisesRegex(RuntimeError, "warmup failed"):
            GATEWAY.warmup(f"http://127.0.0.1:{self.upstream.server_port}", prompt_root)

    def test_health_requires_ready_upstream(self):
        client = self.connection()
        client.request("GET", "/health")
        response = client.getresponse()
        value = json.loads(response.read())
        self.assertEqual(response.status, 200)
        self.assertEqual(value["model_id"], GATEWAY.MODEL_ID)
        self.upstream.ready = False
        client = self.connection()
        client.request("GET", "/health")
        response = client.getresponse()
        self.assertEqual(response.status, 503)
        self.assertNotEqual(json.loads(response.read())["status"], "ready")

    def test_template_failure_is_explicit_and_preserves_messages(self):
        self.upstream.template_missing = True
        _, response = self.chat()
        self.assertEqual(response.status, 200)
        response.read()
        record = self.record()
        self.assertEqual(record["model_input"]["rendered_prompt_status"], "unavailable")
        self.assertIsNone(record["model_input"]["rendered_prompt"])
        self.assertEqual(record["model_input"]["messages"], self.payload()["messages"])

    def test_context_lookup_returns_actual_record_for_message(self):
        _, response = self.chat(headers={"X-Humanning-Message-ID": "reply-a", "X-Humanning-Session-ID": "session-a", "X-Humanning-Turn-ID": "turn-a"})
        response.read()
        self.record()
        client = self.connection()
        client.request("POST", "/v1/debug/message-context", json.dumps({"message_id": "reply-a", "session_id": "session-a"}), {"Content-Type": "application/json"})
        response = client.getresponse()
        found = json.loads(response.read())
        self.assertEqual(response.status, 200)
        self.assertEqual(found["match"], "id")
        self.assertEqual(found["records"][0]["response"]["id"], "upstream-actual")

    def test_foreign_origin_is_rejected_and_correlation_headers_are_allowed(self):
        _, response = self.chat(headers={"Origin": "https://foreign.invalid"})
        self.assertEqual(response.status, 403)
        response.read()
        self.assertEqual(self.upstream.requests, [])
        client = self.connection()
        client.request("OPTIONS", "/v1/chat/completions", headers={"Origin": "http://localhost:5173"})
        response = client.getresponse()
        self.assertEqual(response.status, 204)
        self.assertIn("X-Humanning-Message-ID", response.getheader("Access-Control-Allow-Headers"))
        response.read()

    def test_validation_rejects_wrong_model_and_multimodal_before_upstream(self):
        for payload in (self.payload(model="wrong"), self.payload(messages=[{"role": "user", "content": [{"type": "image_url", "image_url": "https://foreign.invalid"}]}])):
            _, response = self.chat(payload)
            self.assertEqual(response.status, 400)
            response.read()
        self.assertEqual(self.upstream.requests, [])

    def test_tool_call_stream_fragments_keep_exact_function_arguments(self):
        accumulator = GATEWAY.CompletionAccumulator()
        accumulator.consume({"id": "tool-request", "choices": [{"index": 0, "delta": {"tool_calls": [
            {"index": 0, "id": "call-a", "type": "function", "function": {"name": "remember", "arguments": '{"topic":'}}]}}]})
        accumulator.consume({"choices": [{"index": 0, "delta": {"tool_calls": [
            {"index": 0, "function": {"arguments": '"school"}'}}]}, "finish_reason": "tool_calls"}]})
        result = accumulator.response()
        self.assertEqual(result["choices"][0]["message"]["tool_calls"], [{"id": "call-a", "type": "function", "function": {"name": "remember", "arguments": '{"topic":"school"}'}}])
        self.assertEqual(result["choices"][0]["finish_reason"], "tool_calls")

    def test_get_context_lookup_and_invalid_filename_query(self):
        _, response = self.chat(headers={"X-Humanning-Message-ID": "reply-a", "X-Humanning-Session-ID": "session-a", "X-Humanning-Turn-ID": "turn-a"})
        response.read()
        self.record()
        client = self.connection()
        client.request("GET", "/v1/debug/message-context?message_id=reply-a&session_id=session-a")
        response = client.getresponse()
        self.assertEqual(response.status, 200)
        self.assertEqual(json.loads(response.read())["match"], "id")
        client = self.connection()
        client.request("GET", "/v1/debug/message-context?filename=..%2Fprivate.json")
        response = client.getresponse()
        self.assertEqual(response.status, 400)
        response.read()

    def test_upstream_configuration_rejects_remote_hosts_and_credentials(self):
        for url in ("https://127.0.0.1:18088", "http://foreign.invalid", "http://secret@127.0.0.1", "http://127.0.0.1/other"):
            with self.subTest(url=url):
                with self.assertRaises(ValueError):
                    GATEWAY.GemmaUpstream(url)


if __name__ == "__main__":
    unittest.main()
