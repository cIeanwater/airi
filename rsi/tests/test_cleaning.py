"""Corpus boundary tests use synthetic records, never private conversations."""

import copy
import json
import tempfile
import unittest
from pathlib import Path

from rsi.cleaning import apply_review, content_hash, extract, finalize, _split
from rsi.common import read_jsonl, write_json, write_jsonl


SCOPE = {"user_id": "test-person", "character_id": "test-character"}
TOOL = {"type": "function", "function": {"name": "lookup", "parameters": {"type": "object"}}}


def request(messages=None, answer="收到。", reasoning="", gateway=False, **extra):
    model_input = {"model": "test-model", "messages": messages if messages is not None else [
        {"role": "system", "content": "请根据已有信息回答。"}, {"role": "user", "content": "你好。"}]}
    model_input.update(extra)
    message = {"role": "assistant", "content": answer}
    if reasoning:
        message["reasoning_content"] = reasoning
    result = {"startedAt": "2026-01-01T00:00:00Z", "status": 200,
              "request": model_input, "response": {"model": "test-model", "choices": [
                  {"finish_reason": "stop", "message": message}]}}
    if gateway:
        result.update({"status": "ok", "http_status": 200, "model_input": model_input})
    return result


def review(candidate, short_reasoning="", **overrides):
    result = {"candidate_id": candidate["id"], "candidate_sha256": candidate["content_sha256"],
              "reviewer": "synthetic-reviewer", "decision": "accept", "kind": candidate["kind"],
              "reason_codes": [], "short_reasoning": short_reasoning,
              "resolved_issues": list(candidate["review_required"]),
              "checks": {"answer_faithful": True, "no_future_leak": True, "task_appropriate": True,
                         "privacy_checked": True, "persona_consistent": True, "summary_faithful": True,
                         "tool_trace_valid": True}}
    result.update(overrides)
    return result


class CleaningTests(unittest.TestCase):
    def setUp(self):
        temporary_root = Path(__file__).resolve().parents[1] / "private" / "test-temp"
        temporary_root.mkdir(parents=True, exist_ok=True)
        self.temporary = tempfile.TemporaryDirectory(dir=temporary_root)
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.snapshot = self.root / "snapshot"

    def add_request(self, name, value, category="companion"):
        write_json(self.snapshot / category / (name + ".json"), value)

    def extract(self, policy=None):
        out = self.root / "extracted"
        report = extract(self.snapshot, out, SCOPE, policy)
        return report, read_jsonl(out / "candidates.jsonl")

    def test_gateway_and_companion_semantic_duplicates_share_one_record(self):
        self.add_request("one", request())
        self.add_request("two", request(gateway=True), "gateway")
        report, rows = self.extract()
        self.assertEqual((report["candidates"], report["duplicates"]), (1, 1))
        self.assertEqual(len(rows[0]["source_ids"]), 2)
        self.assertEqual(rows[0]["content_sha256"], content_hash(rows[0]))

    def test_assistant_only_trajectory_is_kept_with_only_final_target(self):
        prefix = [{"role": "system", "content": "整理记忆。"},
                  {"role": "assistant", "content": "先核对已有记录。", "reasoning_content": "这是历史思考。"}]
        self.add_request("self", request(prefix, "NO_UPDATE", "没有新的可靠事实，因此暂不更新。"))
        _, rows = self.extract()
        row, reasons = apply_review(rows[0], review(rows[0], "无新事实，暂不更新。"))
        self.assertEqual(reasons, [])
        self.assertEqual(row["train_indices"], [2])
        self.assertEqual(row["messages"][:-1], prefix)
        self.assertEqual(row["messages"][-1]["reasoning_content"], "无新事实，暂不更新。")

    def test_exact_tool_arguments_and_results_remain_unchanged(self):
        call = {"id": "call-1", "type": "function", "function": {"name": "lookup", "arguments": '{ "key": "value" }'}}
        prefix = [{"role": "user", "content": "读取已知记录。"},
                  {"role": "assistant", "content": "", "tool_calls": [call]},
                  {"role": "tool", "content": '{"found":true}', "tool_call_id": "call-1"}]
        self.add_request("tool", request(prefix, "已找到。", tools=[TOOL]))
        _, rows = self.extract()
        row, reasons = apply_review(rows[0], review(rows[0]))
        self.assertEqual(reasons, [])
        self.assertEqual(row["messages"][:-1], prefix)
        self.assertEqual(row["tools"], [TOOL])

    def test_missing_tool_result_and_multimodal_input_are_excluded(self):
        call = {"id": "call-1", "type": "function", "function": {"name": "lookup", "arguments": "{}"}}
        self.add_request("pending", request([{"role": "assistant", "tool_calls": [call]}], tools=[TOOL]))
        self.add_request("image", request([{"role": "user", "content": [{"type": "image_url", "image_url": "ignored"}]}]))
        report, rows = self.extract()
        self.assertFalse(rows)
        self.assertEqual(report["rejection_reasons"], {"missing_tool_result": 1, "multimodal_input_requires_separate_pipeline": 1})

    def test_application_no_speak_rule_and_malformed_json_are_hard_rejected(self):
        messages = [{"role": "system", "content": "【即时思考】按输入决定是否开口。"},
                    {"role": "user", "content": json.dumps({"engagement": {"canSpeak": False}})}]
        self.add_request("forbidden", request(messages, '{"thought":"准备说话。","speak":true,"text":"你好。"}'))
        self.add_request("malformed", request(messages, '{"thought":'))
        report, rows = self.extract()
        self.assertFalse(rows)
        self.assertEqual(report["rejection_reasons"], {"forbidden_initiative": 1, "malformed_initiative_json": 1})

    def test_fenced_initiative_is_parsed_without_rewriting_final_content(self):
        answer = '```json\n{"thought":"保持安静。","speak":false,"text":""}\n```'
        self.add_request("fence", request(answer=answer))
        _, rows = self.extract()
        row, _ = apply_review(rows[0], review(rows[0]))
        self.assertEqual(row["messages"][-1]["content"], answer)

    def test_identity_policy_is_runtime_scoped(self):
        self.add_request("bad", request(answer="我叫示例用户。"))
        self.add_request("good", request(answer="你叫示例用户。"))
        report, rows = self.extract({"character_name": "示例角色", "person_names": ["示例用户"]})
        self.assertEqual(report["rejection_reasons"], {"spoken_identity_inversion": 1})
        self.assertEqual(len(rows), 1)

    def test_missing_message_id_does_not_link_unrelated_event(self):
        write_json(self.snapshot / "events" / "one.json", {"id": "event-1", "scope": SCOPE, "kind": "message",
                   "text": "Unrelated event", "status": "finished", "sessionId": "unrelated-session"})
        self.add_request("one", request([{"role": "user", "content": '{"id": ["not-a-string"]}'}]))
        _, rows = self.extract()
        self.assertIsNone(rows[0]["session_id"])
        self.assertEqual(len(rows[0]["source_ids"]), 1)
        self.assertIn("session_unlinked", rows[0]["review_required"])

    def test_tampered_source_or_unbound_review_cannot_be_applied(self):
        self.add_request("one", request())
        _, rows = self.extract()
        verdict = review(rows[0])
        changed = copy.deepcopy(rows[0])
        changed["messages"][-1]["content"] = "changed"
        with self.assertRaisesRegex(ValueError, "hash changed"):
            apply_review(changed, verdict)
        verdict["candidate_sha256"] = "wrong"
        with self.assertRaisesRegex(ValueError, "not bound"):
            apply_review(rows[0], verdict)

    def test_review_cannot_override_signed_media_url_credentials(self):
        for separator in ("?", "&amp;"):
            with self.subTest(separator=separator):
                self.add_request("one", request([{"role": "user", "content": "https://example.invalid/image" + separator + "rkey=private-signed-token"}]))
                # Reuse a candidate only within this isolated synthetic test snapshot.
                output = self.root / ("extracted-" + str(len(separator)))
                extract(self.snapshot, output, SCOPE)
                row = read_jsonl(output / "candidates.jsonl")[0]
                self.assertEqual(apply_review(row, review(row))[1], ["privacy_credential_in_source"])

    def test_review_cannot_admit_deployment_diagnostics_as_chat(self):
        self.add_request("one", request())
        _, rows = self.extract()
        row = rows[0]
        row["messages"][0]["content"] = "这是独立的部署诊断，不是用户聊天。"
        row["content_sha256"] = content_hash(row)
        self.assertEqual(apply_review(row, review(row))[1], ["fixture_in_source"])

    def test_no_automatic_acceptance_of_missing_semantic_evidence(self):
        self.add_request("one", request())
        _, rows = self.extract()
        verdict = review(rows[0], resolved_issues=[])
        self.assertEqual(apply_review(rows[0], verdict)[1], ["unresolved_source_issues"])
        verdict = review(rows[0])
        verdict["checks"]["no_future_leak"] = False
        self.assertEqual(apply_review(rows[0], verdict)[1], ["semantic_checks_incomplete"])

    def test_summary_must_be_source_bound_chinese_and_not_longer(self):
        self.add_request("one", request(reasoning="先核对事实，没有证据就不推断。"))
        _, rows = self.extract()
        for summary, expected in [("", "missing_chinese_summary"), ("English", "summary_not_chinese"),
                                  ("这是新增加的内容。" * 8, "summary_not_shorter_or_over_budget")]:
            with self.subTest(summary=summary):
                self.assertEqual(apply_review(rows[0], review(rows[0], summary))[1], [expected])
        verdict = review(rows[0], "无证据，不推断。")
        verdict["checks"]["summary_faithful"] = False
        self.assertEqual(apply_review(rows[0], verdict)[1], ["summary_faithfulness_unverified"])

    def test_missing_reviews_quarantine_and_small_lineage_does_not_fake_evaluation(self):
        self.add_request("one", request(answer="第一条。", reasoning="先检查证据，再表达判断。"))
        self.add_request("two", request(answer="第二条。"))
        _, rows = self.extract()
        verdicts = self.root / "reviews.jsonl"
        write_jsonl(verdicts, [review(rows[0], "核对证据再判断。") if rows[0]["messages"][-1].get("reasoning_content") else review(rows[0])])
        out = self.root / "final"
        report = finalize(self.root / "extracted" / "candidates.jsonl", verdicts, out)
        self.assertEqual((report["accepted"], report["quarantined"]), (1, 1))
        self.assertFalse(report["readiness"]["ready"])
        self.assertEqual(report["splits"], {"train": 1, "validation": 0, "test": 0})
        self.assertEqual((report["final_answer_changed"], report["tool_definitions_or_arguments_changed"]), (0, 0))

    def test_shared_lineage_and_prompt_are_connected_before_temporal_split(self):
        def row(index):
            return {"id": str(index), "group": "group-" + str(index), "source_ids": ["source-" + str(index)],
                    "messages": [{"role": "user", "content": str(index)}, {"role": "assistant", "content": "ok"}],
                    "tools": [], "occurred_at": index}
        rows = [row(i) for i in range(12)]
        rows[-1]["source_ids"].append("source-0")
        rows[-2]["messages"][0] = rows[1]["messages"][0]
        splits, groups = _split(rows, 10)
        self.assertEqual(groups, 10)
        assigned = {r["id"]: name for name, values in splits.items() for r in values}
        self.assertEqual(assigned["0"], assigned["11"])
        self.assertEqual(assigned["1"], assigned["10"])
        self.assertEqual(set(assigned), {str(i) for i in range(12)})


if __name__ == "__main__":
    unittest.main()
