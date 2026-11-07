"""Outcome and comparison regressions using isolated synthetic evidence."""

import copy
import unittest

from rsi.common import digest
from rsi.evaluation import case_hash, check_suite, diagnose, evaluate, gaps_from_report, gate, prediction_hash


def case(index=0, slice_name="base"):
    return {"id": "case-" + str(index), "group": "group-" + str(index), "task_id": "task-" + str(index),
            "source_ids": ["source-" + str(index)], "slice": slice_name,
            "messages": [{"role": "user", "content": "合成问题" + str(index)}], "expect": {"exact": ["正确"]}}


def prediction(item, actor="actor", answer="正确"):
    return {"id": item["id"], "actor_version": actor, "text": answer, "complete": True,
            "tokens": 8, "latency_ms": 10}


def small_policy():
    return {"min_cases": 4, "min_groups": 4, "min_per_slice": 4, "min_groups_per_slice": 4,
            "required_slices": ["base"], "protected_slices": ["base"], "target_slices": ["base"], "bootstrap_samples": 100}


class EvaluationTests(unittest.TestCase):
    def test_missing_predictions_never_pass_coverage(self):
        cases = [case(i) for i in range(5)]
        baseline = [prediction(c, answer="错误") for c in cases]
        candidate = [prediction(c, "new") for c in cases[:-1]]
        report = evaluate(cases, baseline, candidate, policy=small_policy())
        self.assertEqual(report["coverage"], 0.8)
        self.assertIn("incomplete_evaluation_coverage", gate(report)["reasons"])

    def test_paired_gain_and_demo_cannot_publish(self):
        cases = [case(i) for i in range(4)]
        before = [prediction(c, answer="错误") for c in cases]
        after = [prediction(c, "new") for c in cases]
        report = evaluate(cases, before, after, policy=small_policy())
        self.assertEqual(report["target"]["gain_lower_95"], 1)
        self.assertTrue(gate(report)["promote"])
        report = evaluate(cases, before, after, policy=small_policy(), demo=True)
        self.assertFalse(gate(report)["promote"])
        self.assertIn("demonstration_results_cannot_promote", gate(report)["reasons"])

    def test_noninferiority_uses_interval_not_only_mean(self):
        cases = [case(i) for i in range(10)]
        before = [prediction(c, answer="正确" if i < 5 else "错误") for i, c in enumerate(cases)]
        after = [prediction(c, "new", "错误" if i < 5 else "正确") for i, c in enumerate(cases)]
        report = evaluate(cases, before, after, policy=small_policy())
        self.assertEqual(report["overall"]["mean_gain"], 0)
        self.assertLess(report["slices"]["base"]["gain_lower_95"], -0.02)
        self.assertIn("noninferiority_not_established:base", gate(report)["reasons"])

    def test_grouped_samples_cannot_fake_independence(self):
        cases = [case(i) for i in range(20)]
        for item in cases:
            item["group"] = "one-shared-experience"
        report = evaluate(cases, [prediction(c, answer="错误") for c in cases], [prediction(c, "new") for c in cases], policy=small_policy())
        self.assertEqual(report["overall"]["groups"], 1)
        self.assertIn("insufficient_independent_cases", gate(report)["reasons"])

    def test_tool_effects_need_external_content_bound_receipts(self):
        item = case(slice_name="tools")
        item["environment_sha256"] = digest("frozen-environment")
        item["expect"] = {"effects": [{"kind": "tool", "name": "lookup", "arguments": {"query": "value"}, "output": {"status": "ok", "answer": 42}}]}
        output = prediction(item)
        output["tool_calls"] = [{"id": "call-1", "function": {"name": "lookup", "arguments": {"query": "value"}}}]
        output["effects"] = [{"verified": True, "answer": 42}]
        report = evaluate([item], [output], [{**output, "actor_version": "new"}], policy=small_policy())
        self.assertEqual(report["overall"]["candidate_score"], 0)
        self.assertIn("missing_trusted_evidence", gate(report)["reasons"])
        receipts = [{"case_id": item["id"], "actor_version": p["actor_version"], "prediction_sha256": prediction_hash(p),
                     "environment_sha256": item["environment_sha256"], "source": "replay", "kind": "tool", "name": "lookup",
                     "call_id": "call-1", "arguments_sha256": digest({"query": "value"}),
                     "output": {"status": "ok", "answer": 42}} for p in (output, {**output, "actor_version": "new"})]
        report = evaluate([item], [output], [{**output, "actor_version": "new"}], evidence=receipts, policy=small_policy())
        self.assertEqual(report["overall"]["candidate_score"], 1)
        changed = {**output, "actor_version": "new", "text": "修改了调用结果的叙述"}
        report = evaluate([item], [output], [changed], evidence=receipts, policy=small_policy())
        self.assertEqual(report["overall"]["candidate_score"], 0)

    def test_tool_receipts_cannot_cover_a_different_argument_or_call(self):
        item = case(slice_name="tools")
        item["environment_sha256"] = digest("environment")
        item["expect"] = {"effects": [{"kind": "tool", "name": "lookup", "arguments": {"query": "required"}, "output": {"status": "ok"}}]}
        outputs = [prediction(item), prediction(item, "new")]
        for output in outputs:
            output["tool_calls"] = [{"id": "real-call", "function": {"name": "lookup", "arguments": {"query": "different"}}}]
        receipts = [{"case_id": item["id"], "actor_version": p["actor_version"], "prediction_sha256": prediction_hash(p),
                     "environment_sha256": item["environment_sha256"], "source": "replay", "kind": "tool", "name": "lookup",
                     "call_id": "invented-call", "arguments_sha256": digest({"query": "required"}), "output": {"status": "ok"}} for p in outputs]
        report = evaluate([item], outputs[:1], outputs[1:], evidence=receipts, policy=small_policy())
        self.assertEqual(report["overall"]["candidate_score"], 0)
        self.assertTrue(any(f["reason"].startswith("tool_arguments_or_call_mismatch") for f in report["findings"]))

    def test_policy_cannot_change_after_measurement(self):
        cases = [case(i) for i in range(4)]
        report = evaluate(cases, [prediction(c, answer="错误") for c in cases], [prediction(c, "new") for c in cases], policy=small_policy())
        with self.assertRaisesRegex(ValueError, "policy changed"):
            gate(report, {**small_policy(), "minimum_gain": 0.1})

    def test_memory_revision_effect_is_not_an_approved_flag(self):
        item = case(slice_name="memory")
        item["environment_sha256"] = digest("memory-snapshot")
        item["expect"] = {"effects": [{"kind": "memory", "name": "update", "output": {"before_revision": 2, "after_revision": 3, "source_ids": ["event"]}}]}
        before, after = prediction(item), prediction(item, "new")
        receipts = [{"case_id": item["id"], "actor_version": p["actor_version"], "prediction_sha256": prediction_hash(p),
                     "environment_sha256": item["environment_sha256"], "source": "replay", "kind": "memory", "name": "update",
                     "output": {"approved": True, "before_revision": 2, "after_revision": 4, "source_ids": ["event"]}} for p in (before, after)]
        report = evaluate([item], [before], [after], evidence=receipts, policy=small_policy())
        self.assertEqual(report["overall"]["candidate_score"], 0)
        self.assertTrue(any(f["reason"] == "effect_mismatch:update" for f in report["findings"]))

    def test_persona_review_binds_case_output_and_calibrated_judge(self):
        item = case(slice_name="persona")
        item["rubric_sha256"] = digest("rubric")
        before, after = prediction(item), prediction(item, "new")
        calibration = {"judge_version": "independent-v1", "calibration_sha256": digest("calibration"), "rubric_sha256": item["rubric_sha256"]}
        judgments = [{"case_id": item["id"], "case_sha256": case_hash(item), "prediction_sha256": prediction_hash(p),
                      "reviewer": "fixed-judge", "independent": True, "score": 1, **calibration} for p in (before, after)]
        policy = {**small_policy(), "judges": {"fixed-judge": calibration}}
        report = evaluate([item], [before], [after], judgments=judgments, policy=policy)
        self.assertEqual(report["overall"]["candidate_score"], 1)
        modified = {**after, "reasoning_summary": "新思考"}
        report = evaluate([item], [before], [modified], judgments=judgments, policy=policy)
        self.assertEqual(report["overall"]["candidate_score"], 0)
        uncalibrated = evaluate([item], [before], [after], judgments=judgments, policy=small_policy())
        self.assertIn("missing_trusted_evidence", gate(uncalibrated)["reasons"])

    def test_lineage_and_prompt_leaks_are_rejected(self):
        item = case()
        episode = {"source_ids": item["source_ids"], "messages": []}
        with self.assertRaisesRegex(ValueError, "lineage"):
            check_suite([item], [episode])
        episode = {"source_ids": ["separate"], "messages": [*item["messages"], {"role": "assistant", "content": "target"}]}
        with self.assertRaisesRegex(ValueError, "prompt"):
            check_suite([item], [episode])

    def test_nonfinite_metrics_and_uncalibrated_scores_stop(self):
        item = case()
        before = prediction(item)
        after = prediction(item, "new")
        after["latency_ms"] = float("nan")
        with self.assertRaisesRegex(ValueError, "numeric"):
            evaluate([item], [before], [after])

    def test_diagnosis_repairs_faults_before_training(self):
        gaps = [{"episode_id": str(i), "group": str(i), "evidence_sha256": digest(i), "cause": "parameter",
                 "context_sufficient": True, "category": "memory-use"} for i in range(3)]
        self.assertEqual(diagnose(gaps, 3, True)["action"], "train")
        self.assertEqual(diagnose(gaps, 3, True, cooldown_ready=False)["action"], "defer")
        repeated = copy.deepcopy(gaps)
        for gap in repeated:
            gap["group"] = "same-source"
        self.assertEqual(diagnose(repeated, 3, True)["action"], "defer")
        gaps.append({"episode_id": "failure", "group": "fault", "evidence_sha256": digest("fault"), "cause": "tool"})
        self.assertEqual(diagnose(gaps, 3, True)["action"], "repair_tool")

    def test_diagnosis_can_use_measured_case_failures(self):
        cases = [case(i) for i in range(4)]
        for item in cases:
            item["diagnostic"] = {"cause": "parameter", "category": "grounded-answer", "context_sufficient": True}
        report = evaluate(cases, [prediction(c, answer="错误") for c in cases], [prediction(c, "new") for c in cases], policy=small_policy())
        gaps = gaps_from_report(cases, report)
        self.assertEqual(diagnose(gaps, 4, True)["action"], "train")
        with self.assertRaisesRegex(ValueError, "measured evaluation suite"):
            gaps_from_report([case()], report)


if __name__ == "__main__":
    unittest.main()
