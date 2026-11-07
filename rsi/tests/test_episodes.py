"""Synthetic experience capture, causal compilation, and leakage regression checks."""

import copy
import json
import tempfile
import unittest
from pathlib import Path

from rsi.common import digest, read_json, read_jsonl
from rsi.dataset import build_dataset, compile_episode, compile_messages, split_episodes, verify_episode
from rsi.episodes import EpisodeStore, import_request, seal_episode, validate_episode


TEMP = Path(__file__).resolve().parents[1] / "private/test-temp"
TEMP.mkdir(parents=True, exist_ok=True)


def episode(number=0, kind="dialogue", tool=False):
    messages = [{"id": "system", "role": "system", "origin": "application", "content": "合成规则" + str(number), "status": "finished"}]
    if kind == "dialogue":
        messages.append({"id": "input", "role": "user", "origin": "human", "content": "问题" + str(number), "status": "received"})
    tools = []
    if tool:
        tools = [{"type": "function", "function": {"name": "memory", "parameters": {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]}}}]
        messages.extend([
            {"id": "lookup", "role": "assistant", "origin": "model", "content": "", "status": "finished", "reasoning_content": "先核对证据。",
             "tool_calls": [{"id": "call-1", "type": "function", "function": {"name": "memory", "arguments": '{"query":"合成"}'}}]},
            {"id": "receipt", "role": "tool", "origin": "tool", "content": '{"fact":"合成事实"}', "status": "finished", "tool_call_id": "call-1"},
        ])
    messages.append({"id": "answer", "role": "assistant", "origin": "model", "content": "回答" + str(number), "status": "finished"})
    return seal_episode({"schema_version": 1, "id": "episode-" + str(number), "kind": kind,
                         "scope": {"user_id": "owner", "character_id": "character"}, "session_id": "session-" + str(number),
                         "person_id": "person", "source_ids": ["source-" + str(number)], "occurred_at": number,
                         "context": {}, "model": {"requested": "synthetic", "resolved": "synthetic"},
                         "tools": tools, "template_kwargs": {"enable_thinking": True}, "messages": messages,
                         "outcome": {"status": "unverified"}})


def evidence(value, tool=False):
    checks = [{"message_id": "answer", "kind": "exact", "expected": value["messages"][-1]["content"]}]
    if tool:
        receipt = next(m for m in value["messages"] if m["role"] == "tool")
        checks.insert(0, {"message_id": "lookup", "kind": "tool_result", "reasoning_expected": "先核对证据。",
                          "expected": {"call_id": "call-1", "name": "memory", "arguments": {"query": "合成"}, "status": "finished", "content_sha256": digest(receipt["content"])}})
    return {"episode_sha256": value["content_sha256"], "reviewer": "synthetic-independent-fixture", "independent": True, "checks": checks}


class NativeTemplate:
    """Small deterministic tokenizer boundary for protocol tests, without model dependencies."""

    chat_template = "<|channel>"

    def apply_chat_template(self, messages, tools=None, tokenize=False, add_generation_prompt=False, **kwargs):
        text = "<bos>" + ("<think>" if kwargs.get("enable_thinking") else "")
        if tools:
            text += json.dumps(tools, sort_keys=True, ensure_ascii=False)
        for message in messages:
            text += "<turn>" + message["role"] + "\n"
            if message.get("reasoning_content"):
                text += "<|channel>thought\n" + message["reasoning_content"] + "\n<channel|>"
            if message.get("tool_calls"):
                text += json.dumps(message["tool_calls"], sort_keys=True, ensure_ascii=False)
            text += message["content"] + "<end>\n"
        return text + ("<turn>assistant\n" if add_generation_prompt else "")

    def encode(self, text, add_special_tokens=False):
        return [ord(character) for character in text]


class EpisodeTests(unittest.TestCase):
    def test_consecutive_assistant_phases_use_native_continuation(self):
        class MergingTemplate(NativeTemplate):
            def apply_chat_template(self, messages, tools=None, tokenize=False, add_generation_prompt=False, continue_final_message=False, **kwargs):
                text = "<bos>"
                previous = None
                for message in messages:
                    if message["role"] != "assistant" or previous != "assistant":
                        text += "<turn>" + message["role"] + "\n"
                    text += message["content"]
                    if message["role"] != "assistant":
                        text += "<end>\n"
                    previous = message["role"]
                if previous == "assistant" and not continue_final_message:
                    text += "<end>\n"
                return text + ("<turn>assistant\n" if add_generation_prompt else "")

        value = episode(kind="reflection")
        value["messages"].append({"id": "second", "role": "assistant", "origin": "model", "content": "后续自我修正", "status": "finished"})
        value = seal_episode(value)
        rows = compile_episode(value, MergingTemplate(), 1000, [1, 2])
        self.assertEqual("".join(chr(c) for c in rows[1]["labels"] if c != -100), "后续自我修正<end>\n")

    def test_assistant_only_reflection_and_immutable_capture(self):
        value = episode(kind="reflection")
        self.assertFalse(any(m["role"] == "user" for m in value["messages"]))
        with tempfile.TemporaryDirectory(dir=TEMP) as temporary:
            store = EpisodeStore(temporary)
            self.assertTrue(store.append(value))
            self.assertFalse(store.append(value))
            self.assertEqual(store.get(value["id"]), value)
            self.assertEqual(store.audit()["assistant_only"], 1)
            changed = copy.deepcopy(value)
            changed["messages"][-1]["content"] = "替换"
            with self.assertRaisesRegex(ValueError, "different content"):
                store.append(seal_episode(changed))

    def test_protocol_matches_calls_and_parallel_receipts(self):
        value = episode(tool=True)
        validate_episode(value)
        broken = copy.deepcopy(value)
        broken["messages"][-2]["tool_call_id"] = "wrong-call"
        with self.assertRaisesRegex(ValueError, "pending call"):
            seal_episode(broken)
        missing = copy.deepcopy(value)
        del missing["messages"][-2]
        with self.assertRaisesRegex(ValueError, "Tool results"):
            seal_episode(missing)

    def test_future_state_wrong_origin_and_changed_hash_are_rejected(self):
        value = episode()
        value["context"]["snapshot_at"] = value["occurred_at"] + 1
        with self.assertRaisesRegex(ValueError, "Future context"):
            seal_episode(value)
        value = episode()
        value["messages"][-1]["origin"] = "human"
        with self.assertRaisesRegex(ValueError, "role and origin"):
            seal_episode(value)
        value = episode()
        value["messages"][-1]["content"] = "changed"
        with self.assertRaisesRegex(ValueError, "hash mismatch"):
            validate_episode(value)

    def test_gateway_uses_effective_input_and_remains_unverified(self):
        record = {"schema_version": 1, "request_id": "request", "status": "ok", "http_status": 200,
                  "started_at": "2026-11-08T00:00:00Z", "correlation": {"session_id": "known-session"},
                  "request": {"messages": [{"role": "user", "content": "原始输入"}]},
                  "model_input": {"model": "e2b", "messages": [{"role": "system", "content": "状态"}], "chat_template_kwargs": {"enable_thinking": True}},
                  "response": {"model": "resolved", "choices": [{"finish_reason": "stop", "message": {"role": "assistant", "content": "有效回答"}}]}}
        value = import_request(record, scope={"user_id": "u", "character_id": "c"}, kind="reflection")
        self.assertEqual(value["messages"][0]["content"], "状态")
        self.assertEqual(value["session_id"], "known-session")
        self.assertEqual(value["outcome"]["status"], "unverified")
        self.assertTrue(value["template_kwargs"]["enable_thinking"])
        self.assertFalse(verify_episode(value, None)["eligible"])

    def test_cancelled_output_does_not_become_success(self):
        record = {"status": "canceled", "http_status": 200, "model_input": {"messages": [{"role": "system", "content": "状态"}]},
                  "response": {"choices": [{"finish_reason": "stop", "message": {"role": "assistant", "content": "未发送的尾句"}}]}}
        value = import_request(record)
        proof = {"episode_sha256": value["content_sha256"], "reviewer": "independent", "independent": True,
                 "resolved_issues": value["context"]["issues"], "checks": [{"message_id": value["messages"][-1]["id"], "kind": "exact", "expected": "未发送的尾句"}]}
        result = verify_episode(value, proof)
        self.assertFalse(result["eligible"])
        self.assertIn("unfinished_or_external_target", result["reasons"])

    def test_verification_recomputes_checks_and_binds_content(self):
        value = episode(tool=True)
        proof = evidence(value, tool=True)
        result = verify_episode(value, proof)
        self.assertTrue(result["eligible"])
        self.assertEqual(result["train_indices"], [2, 4])
        proof["checks"][0]["expected"]["content_sha256"] = "forged"
        proof["checks"][0]["passed"] = True
        self.assertFalse(verify_episode(value, proof)["eligible"])
        proof = evidence(value)
        value["messages"][-1]["content"] = "different"
        value = seal_episode(value)
        self.assertIn("verification_missing_or_stale", verify_episode(value, proof)["reasons"])

    def test_compilation_masks_inputs_and_keeps_only_causal_targets(self):
        value = episode(tool=True)
        result = verify_episode(value, evidence(value, tool=True))
        value["train_indices"] = result["train_indices"]
        rows = compile_episode(value, NativeTemplate(), 10000)
        self.assertEqual(len(rows), 2)
        call_target = "".join(chr(token) for token in rows[0]["labels"] if token != -100)
        self.assertIn("memory", call_target)
        self.assertIn("先核对证据", call_target)
        self.assertNotIn("合成事实", call_target)
        self.assertNotIn("回答0", call_target)
        answer_target = "".join(chr(token) for token in rows[1]["labels"] if token != -100)
        self.assertEqual(answer_target, "回答0<end>\n")
        answer_input = "".join(chr(token) for token in rows[1]["input_ids"])
        self.assertIn("合成事实", answer_input)

    def test_no_user_required_and_context_is_not_truncated(self):
        value = episode(kind="initiative")
        row = compile_episode(value, NativeTemplate(), 1000, [1])[0]
        self.assertTrue(any(token != -100 for token in row["labels"]))
        with self.assertRaisesRegex(ValueError, "exceeds context"):
            compile_messages(value["messages"], NativeTemplate(), 2)

    def test_lineage_and_goal_groups_cannot_cross_splits(self):
        values = [episode(i) for i in range(20)]
        for index in (0, 18):
            values[index]["goal_id"] = "one-project"
            values[index] = seal_episode(values[index])
        values[19]["source_ids"].append(values[18]["source_ids"][0])
        values[19] = seal_episode(values[19])
        splits = split_episodes(values)
        labels = {e["id"]: split for split, rows in splits.items() for e in rows}
        self.assertEqual(len({labels[values[i]["id"]] for i in (0, 18, 19)}), 1)

    def test_dataset_requires_real_checks_replay_and_independent_groups(self):
        values = [episode(i) for i in range(20)]
        for value in values:
            value["context"]["dataset_kind"] = "base"
        values = [seal_episode(value) for value in values]
        proofs = {value["id"]: evidence(value) for value in values}
        with tempfile.TemporaryDirectory(dir=TEMP) as temporary:
            output = Path(temporary) / "dataset"
            manifest = build_dataset(values, output, proofs, {"base": 1, "dialogue": 0, "reflection": 0, "initiative": 0}, 1200)
            self.assertTrue(manifest["files"]["train"])
            self.assertEqual(len(read_jsonl(output / "test.jsonl")), 2)
            self.assertEqual(read_json(output / "manifest.json"), manifest)
            with self.assertRaisesRegex(ValueError, "independent"):
                build_dataset([values[0]], Path(temporary) / "small", {values[0]["id"]: proofs[values[0]["id"]]}, {"base": 1, "dialogue": 0, "reflection": 0, "initiative": 0}, 1200)


if __name__ == "__main__":
    unittest.main()
