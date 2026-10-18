"""Check local message records without a model, GPU, or personal messages."""

import importlib.util
import json
import os
import tempfile
import threading
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

    @unittest.skipUnless(os.name == "nt", "Windows readers can block atomic replacement")
    def test_atomic_replace_recovers_after_reader_releases_file(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            recorder = SERVER.MessageRecorder(root, 7, 1000)
            record = {**self.record(), "status": "started"}
            recorder.write(record)
            target = next(root.glob("*.json"))
            blocked = threading.Event()
            replace = Path.replace

            def notify_replace(path, destination):
                try:
                    return replace(path, destination)
                except PermissionError:
                    blocked.set()
                    raise

            writer = threading.Thread(target=recorder.write, args=({**record, "status": "ok"},))
            with patch.object(Path, "replace", new=notify_replace):
                try:
                    with target.open("r", encoding="utf-8"):
                        writer.start()
                        self.assertTrue(blocked.wait(1), "The reader did not block replacement")
                finally:
                    writer.join(2)
            self.assertFalse(writer.is_alive())
            self.assertIsNone(recorder.describe()["last_error"])
            self.assertEqual(json.loads(target.read_text(encoding="utf-8"))["status"], "ok")
            self.assertEqual(list(root.glob("*.tmp")), [])

    @unittest.skipUnless(os.name == "nt", "Retry Windows sharing errors only")
    def test_persistent_sharing_error_is_bounded_and_next_write_recovers(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            recorder = SERVER.MessageRecorder(root, 7, 1000)
            record = {**self.record(), "status": "started"}
            recorder.write(record)
            blocked = PermissionError("synthetic sharing violation")
            blocked.winerror = 32
            with patch.object(Path, "replace", side_effect=blocked) as replace, patch.object(SERVER.time, "sleep") as sleep:
                recorder.write({**record, "status": "ok"})
            self.assertEqual(replace.call_count, 6)
            self.assertEqual([call.args[0] for call in sleep.call_args_list], [0.01, 0.02, 0.04, 0.08, 0.16])
            self.assertEqual(recorder.describe()["last_error"], "PermissionError")
            self.assertEqual(json.loads(next(root.glob("*.json")).read_text(encoding="utf-8"))["status"], "started")
            self.assertEqual(list(root.glob("*.tmp")), [])
            recorder.write({**record, "status": "ok"})
            self.assertIsNone(recorder.describe()["last_error"])
            self.assertEqual(json.loads(next(root.glob("*.json")).read_text(encoding="utf-8"))["status"], "ok")

    def test_context_lookup_holds_writer_lock_and_releases_it_after_errors(self):
        recorder = SERVER.MessageRecorder(None, 7, 1000)
        query = {"message_id": "synthetic-message"}
        result = {"status": "unavailable", "reason": "recording-disabled"}

        def read(directory, actual_query, **limits):
            self.assertTrue(recorder.lock.locked())
            self.assertIsNone(directory)
            self.assertEqual(actual_query, query)
            self.assertEqual(limits, {"max_age_days": 7, "max_records": 1000})
            return result

        with patch.object(SERVER._context_module, "read_local_message_context", side_effect=read):
            self.assertEqual(recorder.read_context(query), result)
        self.assertFalse(recorder.lock.locked())
        with patch.object(SERVER._context_module, "read_local_message_context", side_effect=ValueError("synthetic query")):
            with self.assertRaisesRegex(ValueError, "synthetic query"):
                recorder.read_context(query)
        self.assertFalse(recorder.lock.locked())

    def test_rejects_nonpositive_retention(self):
        for days, count in [(0, 1000), (7, 0)]:
            with self.assertRaises(ValueError):
                SERVER.MessageRecorder(None, days, count)


if __name__ == "__main__":
    unittest.main()
