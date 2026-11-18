"""Native call parsing and fixture provenance checks."""

import tempfile
import unittest
from pathlib import Path

from rsi.common import digest
from rsi.demo import run_demo
from rsi.replay import Arguments, ReplaySession, parse_calls


class ReplayTests(unittest.TestCase):
    def test_native_nested_arguments(self):
        text = ' {query:<|"|>中文查询<|"|>,limit:3,filter:{enabled:true},ids:[1,2]}'
        self.assertEqual(Arguments(text).parse(), {"query": "中文查询", "limit": 3, "filter": {"enabled": True}, "ids": [1, 2]})
        calls = parse_calls('<|tool_call>call:memory' + text + '<tool_call|>')
        self.assertEqual(calls[0]["function"]["arguments"]["query"], "中文查询")

    def test_partial_or_duplicate_arguments_reject(self):
        for text in ('{x:1,x:2}', '{x:NaN}', '{x:1} trailing'):
            with self.assertRaises(ValueError):
                Arguments(text).parse()
        with self.assertRaises(ValueError):
            parse_calls('<|tool_call>call:lookup{x:1}')

    def test_receipt_requires_actual_matching_call(self):
        environment = {"tool_replay": [{"name": "memory", "arguments": {"query": "coffee"}, "status": "finished", "result": "source evidence",
                                        "effects": [{"kind": "tool", "name": "memory", "output": {"found": True}}]}]}
        case = {"id": "case", "environment": environment, "environment_sha256": digest(environment)}
        host = ReplaySession(case)
        prediction = {"id": "case", "actor_version": "actor", "text": "claimed success", "complete": True}
        self.assertEqual(host.receipts(case, prediction), [])
        call = {"id": "call", "function": {"name": "memory", "arguments": {"query": "wrong"}}}
        with self.assertRaises(ValueError):
            host.execute(call)
        call["function"]["arguments"]["query"] = "coffee"
        self.assertEqual(host.execute(call), "source evidence")
        receipts = host.receipts(case, prediction)
        self.assertEqual(receipts[0]["arguments_sha256"], digest({"query": "coffee"}))
        with self.assertRaises(ValueError):
            host.execute(call)

    def test_full_synthetic_walkthrough_cannot_promote(self):
        root = Path(__file__).resolve().parents[1] / "private/test-temp"
        root.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=root) as temporary:
            result = run_demo(Path(temporary) / "demo")
            self.assertEqual(result["goal_status"], "completed")
            self.assertGreater(result["dataset_records"], 0)
            self.assertFalse(result["decision"]["promote"])
            self.assertEqual(result["network_calls"], 0)


if __name__ == "__main__":
    unittest.main()
