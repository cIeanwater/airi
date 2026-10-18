"""Check exact message lookup with synthetic records in temporary directories."""

import importlib.util
import json
import os
import tempfile
import time
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("local_message_context", ROOT / "scripts/local-message-context.py")
CONTEXT = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(CONTEXT)


class MessageContextTests(unittest.TestCase):
    def setUp(self):
        temporary_root = ROOT / ".temp"
        temporary_root.mkdir(exist_ok=True)
        self.temporary = tempfile.TemporaryDirectory(prefix="message-context-", dir=temporary_root)
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name)
        self.started = time.time()
        self.sequence = 0

    def record(self, content="真实回复", *, correlation=None, started=None, **fields):
        self.sequence += 1
        request_id = "chatcmpl-" + f"{self.sequence:032x}"
        result = {
            "schema_version": 1,
            "request_id": request_id,
            "started_at_unix": self.started if started is None else started,
            "status": "ok",
            "request": {"messages": [{"role": "system", "content": "当时的系统提示"}]},
            "model_input": {"rendered_prompt": "<|im_start|>system\n当时的系统提示<|im_end|>"},
            "response": {"choices": [{"index": 0, "message": {"role": "assistant", "content": content}}]},
            **fields,
        }
        if correlation is not None:
            result["correlation"] = correlation
        path = self.directory / f"20261018T000000{self.sequence:06d}Z-{request_id}.json"
        path.write_text(json.dumps(result, ensure_ascii=False), encoding="utf-8")
        return path, result

    def query(self, **fields):
        return CONTEXT.read_local_message_context(self.directory, fields)

    def test_id_returns_all_tool_rounds_in_request_order(self):
        identity = {"message_id": "reply-a", "session_id": "session-a", "turn_id": "turn-a"}
        _, last = self.record(correlation=identity, started=self.started + 2)
        _, first = self.record(None, correlation=identity, started=self.started)
        self.record("other reply", correlation={**identity, "message_id": "reply-b"})
        found = self.query(message_id="reply-a", session_id="session-a")
        self.assertEqual(found["status"], "found")
        self.assertEqual(found["match"], "id")
        self.assertEqual(found["records"], [first, last])

    def test_session_is_required_to_disambiguate_reused_message_id(self):
        self.record(correlation={"message_id": "reply-a", "session_id": "session-a", "turn_id": "turn-a"})
        self.record(correlation={"message_id": "reply-a", "session_id": "session-b", "turn_id": "turn-b"})
        self.assertEqual(self.query(message_id="reply-a"), {"status": "unavailable", "reason": "ambiguous"})
        found = self.query(message_id="reply-a", session_id="session-a")
        self.assertEqual(len(found["records"]), 1)

    def test_id_does_not_return_another_session(self):
        self.record(correlation={"message_id": "reply-a", "session_id": "session-a", "turn_id": "turn-a"})
        self.assertEqual(self.query(message_id="reply-a", session_id="session-b"),
                         {"status": "unavailable", "reason": "not-recorded"})

    def test_old_record_matches_exact_output_and_nearby_timestamp(self):
        _, record = self.record("真实回复 user assistant [2026-10-18]")
        found = self.query(message_id="old-id", content="真实回复 user assistant [2026-10-18]",
                           created_at=(self.started + 90) * 1000)
        self.assertEqual(found, {"status": "found", "match": "content", "records": [record]})

    def test_old_record_removes_only_valid_airi_control_markers(self):
        _, record = self.record('<|ACT {"emotion":"happy"}|>真实回复<|DELAY 0.5|> user assistant')
        found = self.query(content="真实回复 user assistant", created_at=self.started * 1000)
        self.assertEqual(found["records"], [record])
        self.assertEqual(found["match"], "content")

    def test_invalid_or_unknown_markers_remain_literal(self):
        for output in ('真实<|ACT bad json|>回复', '真实<|DELAY 0|>回复', '真实<|OTHER payload|>回复'):
            with self.subTest(output=output):
                self.record(output)
                self.assertEqual(self.query(content="真实回复", created_at=self.started * 1000),
                                 {"status": "unavailable", "reason": "not-recorded"})

    def test_content_lookup_never_guesses_between_equal_replies(self):
        self.record()
        self.record(started=self.started + 2)
        self.assertEqual(self.query(content="真实回复", created_at=self.started * 1000),
                         {"status": "unavailable", "reason": "ambiguous"})

    def test_content_lookup_requires_timestamp_and_preserves_all_text(self):
        self.record("真实回复 user assistant")
        for query in ({"content": "真实回复 user assistant"},
                      {"content": "真实回复", "created_at": self.started * 1000},
                      {"content": "真实回复 user assistant", "created_at": (self.started + 121) * 1000}):
            with self.subTest(query=query):
                self.assertEqual(CONTEXT.read_local_message_context(self.directory, query),
                                 {"status": "unavailable", "reason": "not-recorded"})

    def test_content_fallback_does_not_override_recorded_identity(self):
        self.record(correlation={"message_id": "reply-a", "session_id": "session-a", "turn_id": "turn-a"})
        self.assertEqual(self.query(message_id="reply-b", content="真实回复", created_at=self.started * 1000),
                         {"status": "unavailable", "reason": "not-recorded"})

    def test_disabled_missing_expired_and_corrupt_records_are_explicitly_unavailable(self):
        self.assertEqual(CONTEXT.read_local_message_context(None, {"message_id": "reply-a"}),
                         {"status": "unavailable", "reason": "recording-disabled"})
        self.assertEqual(CONTEXT.read_local_message_context(self.directory / "missing", {"message_id": "reply-a"}),
                         {"status": "unavailable", "reason": "not-recorded"})
        path, _ = self.record(correlation={"message_id": "reply-a", "session_id": "session-a", "turn_id": "turn-a"})
        old_time = self.started - 8 * 86400
        os.utime(path, (old_time, old_time))
        broken, _ = self.record()
        broken.write_text("{broken", encoding="utf-8")
        nonfinite, _ = self.record()
        nonfinite.write_text('{"started_at_unix": NaN}', encoding="utf-8")
        self.assertEqual(self.query(message_id="reply-a"), {"status": "unavailable", "reason": "not-recorded"})

    def test_only_recorder_filenames_are_read(self):
        path, _ = self.record(correlation={"message_id": "reply-a", "session_id": "session-a", "turn_id": "turn-a"})
        path.rename(self.directory / "private-chatcmpl-other.json")
        self.assertEqual(self.query(message_id="reply-a"), {"status": "unavailable", "reason": "not-recorded"})

    def test_top_level_headers_are_excluded_without_changing_request_messages(self):
        _, record = self.record(correlation={"message_id": "reply-a", "session_id": "session-a", "turn_id": "turn-a"},
                               headers={"Authorization": "secret"}, Authorization="secret", http_headers={"Cookie": "secret"})
        found = self.query(message_id="reply-a")
        returned = found["records"][0]
        self.assertNotIn("headers", returned)
        self.assertNotIn("Authorization", returned)
        self.assertNotIn("http_headers", returned)
        self.assertEqual(returned["request"], record["request"])
        self.assertEqual(returned["model_input"], record["model_input"])

    def test_symbolic_link_never_reads_another_directory(self):
        outside = tempfile.TemporaryDirectory(prefix="outside-message-context-", dir=ROOT / ".temp")
        self.addCleanup(outside.cleanup)
        path, _ = self.record(correlation={"message_id": "reply-a", "session_id": "session-a", "turn_id": "turn-a"})
        outside_path = Path(outside.name) / path.name
        path.replace(outside_path)
        try:
            path.symlink_to(outside_path)
        except OSError:
            self.assertTrue(outside_path.is_file())
            self.assertFalse(path.exists())
            return
        self.assertEqual(self.query(message_id="reply-a"), {"status": "unavailable", "reason": "not-recorded"})

    def test_invalid_queries_raise_value_error(self):
        for query in (None, [], {}, {"filename": "../private.json"}, {"message_id": 42},
                      {"message_id": ""}, {"session_id": "session-a"}, {"created_at": True},
                      {"content": "text", "created_at": float("nan")}, {"content": "text", "created_at": -1}):
            with self.subTest(query=query):
                with self.assertRaises(ValueError):
                    CONTEXT.read_local_message_context(self.directory, query)


if __name__ == "__main__":
    unittest.main()
