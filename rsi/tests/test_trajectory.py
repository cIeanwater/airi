"""Exercise tool evidence and loss masks for a revised conversation."""

import copy
import tempfile
import unittest
from pathlib import Path

from rsi.common import digest, read_json, read_jsonl, write_jsonl
from rsi.finetune import inspect_dataset
from rsi.replay import ReplaySession
from rsi.rollout import assemble
from rsi.trajectory import build_trajectory, capture_request, compile_trajectory, replay_receipt
from rsi.tests.test_episodes import NativeTemplate


TOOLS = [{"type": "function", "function": {"name": "lookup", "parameters": {
    "type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"], "additionalProperties": False,
}}}]


def captured():
    messages = [
        {"role": "system", "content": "用中文回答。"},
        {"role": "user", "content": "可以确认桌面情况吗？"},
        {"role": "assistant", "content": "可以看一下。", "reasoning_content": "先确认任务。"},
        {"role": "user", "content": "请查当前窗口。"},
        {"role": "assistant", "content": None, "reasoning_content": "当前窗口需要查询。", "tool_calls": [
            {"id": "lookup-1", "type": "function", "function": {"name": "lookup", "arguments": '{"query":"窗口"}'}}]},
        {"role": "tool", "tool_call_id": "lookup-1", "content": "回执内容：浏览器"},
    ]
    return {"status": "ok", "http_status": 200, "model_input": {"messages": messages, "tools": TOOLS},
            "response": {"choices": [{"finish_reason": "stop", "message": {"role": "assistant", "content": "窗口是浏览器。"}}]}}


class TrajectoryTests(unittest.TestCase):
    def test_capture_refinement_supervises_multiple_assistant_decisions(self):
        record = captured()
        capture = capture_request(record, source_id="capture")
        messages = capture["messages"]
        messages[3]["content"] = "请查现在打开的窗口。"
        messages[6]["content"] = "目前打开的是浏览器。"
        messages[6]["reasoning_content"] = "按这次查到的窗口名称回答。"
        for index in (2, 4, 6):
            messages[index]["loss_weight"] = 1
        row = build_trajectory(messages, capture["tools"], evidence=capture["evidence"])
        compiled = compile_trajectory(row, NativeTemplate(), 4096)
        self.assertEqual(len(compiled), 3)
        targets = ["".join(chr(t) for t in value["labels"] if t != -100) for value in compiled]
        self.assertIn("先确认任务。", targets[0])
        self.assertIn("当前窗口需要查询。", targets[1])
        self.assertIn('"name": "lookup"', targets[1])
        self.assertIn("目前打开的是浏览器。", targets[2])
        self.assertTrue(all("回执内容" not in target for target in targets))
        self.assertEqual(capture["request_sha256"], digest(record))
        self.assertEqual(record["model_input"]["messages"][3]["content"], "请查当前窗口。")

        changed_call = copy.deepcopy(messages)
        changed_call[4]["tool_calls"][0]["function"]["arguments"] = {"query": "另一个窗口"}
        changed_result = copy.deepcopy(messages)
        changed_result[5]["content"] = "回执内容：终端"
        for edited in (changed_call, changed_result):
            with self.assertRaisesRegex(ValueError, "evidence does not match"):
                build_trajectory(edited, capture["tools"], evidence=capture["evidence"])

    def test_replay_receipt_comes_from_the_matching_fixture(self):
        environment = {"tool_replay": [{"name": "lookup", "arguments": {"query": "窗口"}, "status": "finished",
                                        "result": {"app": "browser"}}]}
        case = {"environment": environment, "environment_sha256": digest(environment)}
        session = ReplaySession(case)
        call = captured()["model_input"]["messages"][4]["tool_calls"][0]
        wrong = copy.deepcopy(call)
        wrong["function"]["arguments"] = '{"query":"不同查询"}'
        with self.assertRaisesRegex(ValueError, "differs from"):
            replay_receipt(session, wrong, source_id="fixture-row-1")
        message, evidence = replay_receipt(session, call, source_id="fixture-row-1")
        self.assertEqual(message["content"], '{"app":"browser"}')
        self.assertEqual(evidence["environment_sha256"], digest(environment))
        self.assertEqual(evidence["content_sha256"], digest(message["content"]))

    def test_causal_order_and_weights_reject_a_misplaced_result(self):
        capture = capture_request(captured(), source_id="capture")
        messages = capture["messages"]
        messages[6]["loss_weight"] = 1
        messages[6]["reasoning_content"] = "根据回执回答。"
        misplaced = messages[:5] + [messages[6], messages[5]]
        with self.assertRaisesRegex(ValueError, "missing_tool_result"):
            build_trajectory(misplaced, capture["tools"], evidence=capture["evidence"])
        messages[5]["loss_weight"] = 1
        with self.assertRaisesRegex(ValueError, "only assistant"):
            build_trajectory(messages, capture["tools"], evidence=capture["evidence"])

    def test_capture_keeps_empty_placeholders_for_review(self):
        record = captured()
        record["model_input"]["messages"].insert(3, {"role": "assistant", "content": ""})
        capture = capture_request(record, source_id="capture")
        self.assertEqual(capture["messages"][3], {"role": "assistant", "content": "", "loss_weight": 0})
        self.assertEqual(len(capture["evidence"]), 1)
        record["response"]["choices"][0]["finish_reason"] = "length"
        with self.assertRaisesRegex(ValueError, "completed response"):
            capture_request(record, source_id="capture")

    def test_assembled_manifest_rejects_changed_provenance(self):
        capture = capture_request(captured(), source_id="capture")
        capture["messages"][-1].update(reasoning_content="按回执报告窗口。", loss_weight=1)
        spec = {**capture, "id": "tool-report", "group": "fixture", "kind": "dialogue",
                "scope": {"user_id": "test", "character_id": "test"},
                "source_ids": ["capture"], "refinement": {"kind": "test"}}
        temporary_root = Path(__file__).resolve().parents[1] / "private/test-temp"
        temporary_root.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=temporary_root) as directory:
            dataset = Path(directory) / "dataset"
            assemble([spec], NativeTemplate(), 4096, dataset)
            self.assertIn("provenance_sha256", read_json(dataset / "manifest.json"))
            _, report = inspect_dataset(dataset)
            self.assertTrue(report["pilot_ready"])
            path = dataset / "provenance.jsonl"
            rows = read_jsonl(path)
            rows[0]["evidence"][0]["source_id"] = "other-source"
            write_jsonl(path, rows)
            with self.assertRaisesRegex(ValueError, "provenance is missing or changed"):
                inspect_dataset(dataset)


if __name__ == "__main__":
    unittest.main()
