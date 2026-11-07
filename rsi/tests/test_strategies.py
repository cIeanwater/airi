"""Measured strategy provenance and proposer learning regression checks."""

import copy
import tempfile
import unittest
from pathlib import Path

from rsi.common import digest
from rsi.dataset import verify_episode
from rsi.evaluation import evaluate
from rsi.strategies import StrategyArchive, build_meta_dataset, compare_meta, validate_plan


TEMP = Path(__file__).resolve().parents[1] / "private/test-temp"
TEMP.mkdir(parents=True, exist_ok=True)


def plan(task="task-0", actor="actor-0"):
    return {"schema_version": 1, "id": "plan-" + task, "actor_version": actor, "task_id": task, "target": "基础回答",
            "source_ids": ["source-" + task], "data_instruction": "Use grounded short corrections for " + task,
            "selection": {"kinds": ["reflection"], "source_ids": ["source-" + task]},
            "mix": {"base": 0.4, "dialogue": 0.3, "reflection": 0.2, "initiative": 0.1},
            "training": {"learning_rate": 0.00005, "max_steps": 20, "max_length": 512, "rank": 8},
            "expected_gain": "正确率提升", "context": {"failed_cases": 3}}


def measured(task="task-0", actor="actor-0", win=True, demo=False, origin="model", split="development"):
    cases = [{"id": task + "-" + str(i), "group": task + "-g" + str(i), "source_ids": [task + "-eval" + str(i)],
              "task_id": task + "-eval", "slice": "base", "messages": [{"role": "user", "content": "合成问题" + str(i)}],
              "expect": {"exact": ["正确"]}} for i in range(4)]
    before = [{"id": c["id"], "actor_version": actor, "text": "错误", "complete": True, "tokens": 10, "latency_ms": 10} for c in cases]
    after = [{**p, "actor_version": "candidate-" + task, "text": "正确" if win else "错误"} for p in before]
    policy = {"min_cases": 4, "min_groups": 4, "min_per_slice": 4, "min_groups_per_slice": 4,
              "required_slices": ["base"], "protected_slices": ["base"], "target_slices": ["base"], "bootstrap_samples": 100}
    report = evaluate(cases, before, after, policy=policy, demo=demo)
    report["dataset_sha256"] = digest("dataset-" + task)
    return {"schema_version": 1, "actor_version": actor, "task_id": task, "plan": plan(task, actor),
            "dataset_sha256": report["dataset_sha256"], "suite_sha256": report["suite_sha256"], "report": report,
            "origin": origin, "cost": {"training_seconds": 10, "tokens": 100}, "adaptation_split": split,
            "context": {"failed_cases": 3}, "source_ids": ["source-" + task]}


class StrategyTests(unittest.TestCase):
    def test_plan_bounds_reject_mutated_budget_and_missing_sources(self):
        value = plan()
        self.assertEqual(validate_plan(value), value)
        value["training"]["rank"] = 16
        with self.assertRaisesRegex(ValueError, "fixed"):
            validate_plan(value)
        value = plan()
        value["mix"] = {"base": 0.1, "dialogue": 0.7, "reflection": 0.1, "initiative": 0.1}
        with self.assertRaisesRegex(ValueError, "30%"):
            validate_plan(value)
        value = plan()
        value["selection"]["source_ids"] = ["invented"]
        with self.assertRaisesRegex(ValueError, "declared"):
            validate_plan(value)
        value = plan()
        value["training"]["learning_rate"] = float("nan")
        with self.assertRaises(ValueError):
            validate_plan(value)

    def test_archive_retains_failures_and_is_idempotent(self):
        with tempfile.TemporaryDirectory(dir=TEMP) as temporary:
            archive = StrategyArchive(temporary)
            record = measured()
            saved = archive.append(record)
            self.assertEqual(archive.append(record), saved)
            failure = archive.append(measured("task-failed", win=False))
            self.assertEqual(failure["status"], "failed")
            self.assertEqual(len(archive.records()), 2)
            conflicting = copy.deepcopy(record)
            conflicting["id"] = saved["id"]
            conflicting["cost"]["tokens"] = 101
            with self.assertRaisesRegex(ValueError, "different content"):
                archive.append(conflicting)

    def test_report_actor_and_dataset_bindings_are_required(self):
        with tempfile.TemporaryDirectory(dir=TEMP) as temporary:
            archive = StrategyArchive(temporary)
            value = measured()
            value["dataset_sha256"] = digest("changed-dataset")
            with self.assertRaisesRegex(ValueError, "training dataset"):
                archive.append(value)
            value = measured()
            value["report"]["baseline_version"] = "wrong-actor"
            with self.assertRaisesRegex(ValueError, "measured actor"):
                archive.append(value)

    def test_selection_never_reuses_stale_actor_rewards_or_meta_test(self):
        with tempfile.TemporaryDirectory(dir=TEMP) as temporary:
            archive = StrategyArchive(temporary)
            archive.append(measured("winner"))
            archive.append(measured("manual", origin="manual"))
            archive.append(measured("failure", win=False))
            archive.append(measured("old", actor="actor-old"))
            archive.append(measured("sealed", split="meta_test"))
            selected = archive.select("actor-0", 4)
            self.assertEqual({v["task_id"] for v in selected}, {"winner", "manual", "failure"})
            self.assertNotIn("report", selected[0])
            self.assertIn("manual", {v["origin"] for v in selected})
            self.assertIn("failed", {v["status"] for v in selected})

    def test_meta_data_requires_independent_actual_wins(self):
        records = [measured("one"), measured("two"), measured("three")]
        result = build_meta_dataset(records, "actor-0")
        self.assertTrue(result["eligible"])
        self.assertFalse(result["ready_for_training"])
        self.assertEqual(result["tasks"], 3)
        for episode in result["episodes"]:
            verification = result["verifications"][episode["id"]]
            self.assertTrue(verify_episode(episode, verification)["eligible"])
        records[2] = measured("three", demo=True)
        self.assertFalse(build_meta_dataset(records, "actor-0")["eligible"])
        records[2] = measured("three", win=False)
        self.assertFalse(build_meta_dataset(records, "actor-0")["eligible"])

    def test_meta_rewrites_of_same_source_do_not_count_as_independent_tasks(self):
        records = [measured("one"), measured("two"), measured("three")]
        for record in records:
            record["plan"]["source_ids"] = ["shared-source"]
            record["plan"]["selection"]["source_ids"] = ["shared-source"]
            record["source_ids"] = ["shared-source"]
        self.assertFalse(build_meta_dataset(records, "actor-0")["eligible"])

    def test_meta_comparison_requires_same_actor_budget_and_held_out_trials(self):
        budget = {"max_training_seconds": 30, "max_tokens": 200, "max_steps": 20}
        trial = {"task_id": "test-one", "actor_version": "actor-0", "budget_sha256": digest(budget), "budget": budget, "split": "meta_test",
                 "baseline_proposer_version": "proposer-old", "candidate_proposer_version": "proposer-new",
                 "baseline": measured("test-one", win=False, split="meta_test"), "candidate": measured("test-one", split="meta_test")}
        trial["baseline"]["context"]["proposer_version"] = "proposer-old"
        trial["candidate"]["context"]["proposer_version"] = "proposer-new"
        result = compare_meta([trial], "actor-0", digest(budget))
        self.assertFalse(result["promote"])
        with self.assertRaisesRegex(ValueError, "same actor and budget"):
            compare_meta([trial], "actor-0", digest("another-budget"))
        with self.assertRaisesRegex(ValueError, "overlaps proposer"):
            compare_meta([trial], "actor-0", digest(budget), training_task_ids=["test-one"])
        trial["candidate"]["cost"]["tokens"] = 1000
        with self.assertRaisesRegex(ValueError, "exceeded"):
            compare_meta([trial], "actor-0", digest(budget))
        trial["candidate"]["cost"]["tokens"] = 100
        trial["split"] = "development"
        with self.assertRaisesRegex(ValueError, "held-out"):
            compare_meta([trial], "actor-0", digest(budget))


if __name__ == "__main__":
    unittest.main()
