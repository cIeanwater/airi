"""Check local message records without a model, GPU, or personal messages."""

import importlib.util
import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch


SPEC = importlib.util.spec_from_file_location("bf16_server", Path(__file__).with_name("serve-qwen35-2b-bf16.py"))
SERVER = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(SERVER)


class MessageRecorderTests(unittest.TestCase):
    def record(self, suffix=0):
        return {
            "request_id": f"chatcmpl-{suffix:032x}", "started_at_unix": time.time() + suffix,
            "request": {"messages": [{"role": "user", "content": "合成问题"}]},
            "response": {"choices": [{"message": {"role": "assistant", "content": "合成回答"}}]},
        }

    def test_disabled_recording_does_not_touch_disk(self):
        recorder = SERVER.MessageRecorder(None, 7, 1000)
        recorder.write(self.record())
        self.assertFalse(recorder.describe()["enabled"])
        self.assertIsNone(recorder.describe()["last_error"])

    def test_atomic_record_contains_complete_unicode_request_and_response(self):
        with tempfile.TemporaryDirectory() as directory:
            recorder = SERVER.MessageRecorder(Path(directory), 7, 1000)
            record = self.record()
            recorder.write(record)
            paths = list(Path(directory).glob("*.json"))
            self.assertEqual(len(paths), 1)
            self.assertEqual(json.loads(paths[0].read_text(encoding="utf-8")), record)
            self.assertEqual(list(Path(directory).glob("*.tmp")), [])
            record["status"] = "ok"
            recorder.write(record)
            self.assertEqual(len(list(Path(directory).glob("*.json"))), 1)
            self.assertEqual(json.loads(paths[0].read_text(encoding="utf-8"))["status"], "ok")

    def test_retention_deletes_only_owned_records(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            unrelated = root / "do-not-delete.json"
            unrelated.write_text("{}", encoding="utf-8")
            recorder = SERVER.MessageRecorder(root, 7, 2)
            for suffix in range(3):
                recorder.write(self.record(suffix))
            paths = sorted(root.glob("*-chatcmpl-*.json"))
            self.assertEqual(len(paths), 2)
            old_time = time.time() - 8 * 86400
            os.utime(paths[0], (old_time, old_time))
            recorder.prune()
            self.assertEqual(len(list(root.glob("*-chatcmpl-*.json"))), 1)
            self.assertTrue(unrelated.exists())

    def test_failed_write_is_visible_without_failing_generation(self):
        with tempfile.TemporaryDirectory() as directory:
            recorder = SERVER.MessageRecorder(Path(directory), 7, 1000)
            recorder.directory.rmdir()
            recorder.write(self.record())
            self.assertEqual(recorder.describe()["last_error"], "FileNotFoundError")

    def test_failed_replace_preserves_previous_record_and_removes_temporary_file(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            recorder = SERVER.MessageRecorder(root, 7, 1000)
            record = self.record()
            recorder.write(record)
            previous = next(root.glob("*.json")).read_text(encoding="utf-8")
            record["status"] = "ok"
            with patch.object(Path, "replace", side_effect=PermissionError("synthetic write failure")):
                recorder.write(record)
            self.assertEqual(recorder.describe()["last_error"], "PermissionError")
            self.assertEqual(next(root.glob("*.json")).read_text(encoding="utf-8"), previous)
            self.assertEqual(list(root.glob("*.tmp")), [])

    def test_rejects_nonpositive_retention(self):
        for days, count in [(0, 1000), (7, 0)]:
            with self.assertRaises(ValueError):
                SERVER.MessageRecorder(None, days, count)


if __name__ == "__main__":
    unittest.main()
