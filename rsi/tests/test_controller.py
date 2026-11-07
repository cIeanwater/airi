"""Controller boundaries with synthetic adapters and content-bound datasets."""

import subprocess
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from rsi.client import completion
from rsi.common import digest, file_hash, read_json, tree_hash, write_json, write_jsonl
from rsi.controller import Registry, poll, release_actor, run_cycle
from rsi.episodes import seal_episode
from rsi.evaluation import evaluate


def config():
    return {"model": "unused-hf-base", "device": 0, "qlora": True, "rank": 8,
            "max_length": 512, "learning_rate": 0.00005, "max_steps": 1}


def plan():
    return {"schema_version": 1, "id": "plan-one", "actor_version": "actor-one", "task_id": "adaptation-one",
            "target": "base", "source_ids": ["selected-source", "excluded-source"],
            "data_instruction": "verified-trajectories", "selection": {"kinds": ["dialogue"], "source_ids": ["selected-source"]},
            "mix": {"base": 0.5, "dialogue": 0.5, "reflection": 0, "initiative": 0},
            "training": {"rank": 8, "max_length": 512, "learning_rate": 0.00005, "max_steps": 1},
            "expected_gain": "Improve verified answers", "context": {"observed_gap": "repeated factual error"}}


def episode(number, source="selected-source", kind="dialogue"):
    return seal_episode({"schema_version": 1, "id": "episode-" + str(number), "kind": kind,
                         "scope": {"user_id": "synthetic", "character_id": "actor"},
                         "session_id": "session-" + str(number), "source_ids": [source], "occurred_at": number,
                         "context": {}, "model": {"actor_version": "actor-one"}, "tools": [], "template_kwargs": {},
                         "messages": [{"id": "system", "role": "system", "origin": "application", "content": "Rules", "status": "finished"},
                                      {"id": "user", "role": "user", "origin": "human", "content": "Question " + str(number), "status": "received"},
                                      {"id": "answer", "role": "assistant", "origin": "model", "content": "Verified answer", "status": "finished"}],
                         "outcome": {"status": "verified"}, "train_indices": [2], "verification_sha256": digest("synthetic-verification")})


def cases():
    return [{"id": "eval-" + str(i), "group": "held-out-group-" + str(i), "task_id": "held-out-task-" + str(i),
             "source_ids": ["held-out-source-" + str(i)], "slice": "base", "messages": [{"role": "user", "content": "Held out " + str(i)}],
             "expect": {"exact": ["correct"]}} for i in range(4)]


def policy():
    return {"min_cases": 4, "min_groups": 4, "min_per_slice": 4, "min_groups_per_slice": 4,
            "required_slices": ["base"], "protected_slices": ["base"], "target_slices": ["base"], "bootstrap_samples": 100}


def predictions(suite, version, text):
    return [{"id": item["id"], "actor_version": version, "text": text, "complete": True, "tokens": 4, "latency_ms": 10} for item in suite]


class UncalledBackend:
    def train(self, *args, **kwargs):
        raise AssertionError("Training reached an invalid input")

    def predict(self, *args, **kwargs):
        raise AssertionError("Prediction reached an invalid input")


class WrongDatasetBackend:
    """Expose a child training run bound to a different dataset, without loading a model."""

    def __init__(self):
        self.calls = []

    def train(self, configuration, dataset, output, parent, role="actor"):
        self.calls.append("train")
        artifact = Path(output) / "adapter"
        write_json(artifact / "lineage.json", {"dataset_sha256": "0" * 64, "role": role, "rank": configuration["rank"]})
        (artifact / "adapter_model.safetensors").write_bytes(b"synthetic-adapter")
        return {"artifact": str(artifact), "version": tree_hash(artifact), "dataset_sha256": "0" * 64,
                "lineage": read_json(artifact / "lineage.json"), "training_seconds": 1, "tokens": 10}

    def predict(self, configuration, suite, output, actor):
        self.calls.append("predict")
        return {"predictions": predictions(suite, actor["version"], "wrong"), "evidence": []}


class ControllerTests(unittest.TestCase):
    def setUp(self):
        temporary_root = Path(__file__).resolve().parents[1] / "private/test-temp"
        temporary_root.mkdir(parents=True, exist_ok=True)
        self.temporary = tempfile.TemporaryDirectory(dir=temporary_root)
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.registry = Registry(self.root / "registry")
        self.registry.initialize("actor", "actor-one")

    def dataset(self, training):
        root = self.root / "dataset"
        base = episode(0, "base-replay-source")
        base["context"]["dataset_kind"] = "base"
        base = seal_episode(base)
        splits = {"train": [base, *training], "validation": [episode(100, "validation-source")], "test": [episode(200, "test-source")]}
        for split, rows in splits.items():
            write_jsonl(root / (split + ".jsonl"), rows)
        manifest = {"schema_version": 1, "kind": "behavior", "mix": plan()["mix"],
                    "records": [{"id": e["id"], "split": split, "content_sha256": e["content_sha256"],
                                 "verification_sha256": e["verification_sha256"]} for split, rows in splits.items() for e in rows],
                    "files": {split: file_hash(root / (split + ".jsonl")) for split in splits}}
        write_json(root / "manifest.json", manifest)
        return root

    def settings(self):
        return {"training": config(), "archive": str(self.root / "archive"), "data_instruction": plan()["data_instruction"],
                "evaluation": policy(), "actor_scope": {"user_id": "synthetic", "character_id": "actor"}}

    def candidate(self, name="candidate"):
        root = self.root / name
        root.mkdir()
        (root / "adapter_model.safetensors").write_bytes(name.encode())
        return {"artifact": str(root), "version": tree_hash(root)}

    def report(self, candidate):
        suite = cases()
        return evaluate(suite, predictions(suite, "actor-one", "wrong"), predictions(suite, candidate["version"], "correct"), policy=policy())

    def test_plan_cannot_train_sources_excluded_by_selection(self):
        dataset = self.dataset([episode(1, "excluded-source")])
        with self.assertRaisesRegex(ValueError, "[Ss]elect|[Ss]ource|[Pp]lan"):
            run_cycle(self.settings(), plan(), dataset, cases(), self.root / "cycle", self.registry,
                      execute=True, backend=UncalledBackend())

    def test_plan_cannot_train_kinds_excluded_by_selection(self):
        dataset = self.dataset([episode(1, kind="reflection")])
        with self.assertRaisesRegex(ValueError, "[Kk]ind|[Ss]elect|[Pp]lan"):
            run_cycle(self.settings(), plan(), dataset, cases(), self.root / "cycle", self.registry,
                      execute=True, backend=UncalledBackend())

    def test_personalized_cycle_rejects_another_identity_scope(self):
        other = episode(1)
        other["scope"] = {"user_id": "different-owner", "character_id": "actor"}
        other = seal_episode(other)
        dataset = self.dataset([other])
        original = file_hash(self.registry.path)
        with self.assertRaisesRegex(ValueError, "[Ss]cope|[Ii]dentity"):
            run_cycle(self.settings(), plan(), dataset, cases(), self.root / "cycle", self.registry,
                      execute=True, backend=UncalledBackend())
        self.assertEqual(file_hash(self.registry.path), original)

    def test_child_dataset_binding_is_checked_before_candidate_evaluation(self):
        dataset = self.dataset([episode(1)])
        backend = WrongDatasetBackend()
        with self.assertRaisesRegex(ValueError, "[Dd]ataset|[Ll]ineage|[Bb]ind"):
            run_cycle(self.settings(), plan(), dataset, cases(), self.root / "cycle", self.registry,
                      execute=True, backend=backend)
        self.assertEqual(backend.calls, ["predict", "train"])
        self.assertEqual(self.registry.state()["actor"]["version"], "actor-one")
        self.assertFalse((self.root / "archive").exists())

    def test_plan_only_cycle_and_poll_have_no_side_effects(self):
        output = self.root / "not-created"
        result = run_cycle(self.settings(), plan(), "not-a-dataset", cases(), output, self.registry, backend=UncalledBackend())
        self.assertIs(result["execute"], False)
        self.assertFalse(output.exists())
        watched = poll({"inbox": str(self.root / "missing-inbox")})
        self.assertFalse(watched["execute"])
        self.assertFalse((self.root / "missing-inbox").exists())

    def test_registry_stale_lease_and_rollback(self):
        candidate = self.candidate()
        expected = self.registry.state()["actor"]
        report = self.report(candidate)
        self.assertTrue(self.registry.promote("actor", candidate, report, expected=expected)["promote"])
        with self.assertRaisesRegex(ValueError, "[Cc]hampion changed"):
            self.registry.promote("actor", candidate, report, expected=expected)
        self.assertEqual(self.registry.rollback("actor")["actor"], expected)

    def test_registry_rejects_changed_candidate_and_changed_prior_version(self):
        previous = self.candidate("prior")
        registry = Registry(self.root / "registered-artifact")
        registry.initialize("actor", previous["version"], previous["artifact"])
        candidate = self.candidate()
        suite = cases()
        report = evaluate(suite, predictions(suite, previous["version"], "wrong"), predictions(suite, candidate["version"], "correct"), policy=policy())
        expected = registry.state()["actor"]
        (Path(candidate["artifact"]) / "adapter_model.safetensors").write_bytes(b"changed")
        with self.assertRaisesRegex(ValueError, "candidate artifact"):
            registry.promote("actor", candidate, report, expected=expected)
        candidate["version"] = tree_hash(candidate["artifact"])
        report = evaluate(suite, predictions(suite, previous["version"], "wrong"), predictions(suite, candidate["version"], "correct"), policy=policy())
        registry.promote("actor", candidate, report, expected=expected)
        (Path(previous["artifact"]) / "adapter_model.safetensors").write_bytes(b"changed prior")
        with self.assertRaisesRegex(ValueError, "Prior artifact changed"):
            registry.rollback("actor")

    def test_release_actor_rechecks_saved_prediction_evidence(self):
        candidate = self.candidate()
        dataset = self.dataset([episode(1)])
        run = self.root / "saved-run"
        run.mkdir()
        shutil.copytree(dataset, run / "dataset")
        suite = cases()
        before = predictions(suite, "actor-one", "wrong")
        after = predictions(suite, candidate["version"], "correct")
        report = evaluate(suite, before, after, policy=policy())
        report["dataset_sha256"] = digest(read_json(dataset / "manifest.json"))
        write_json(run / "settings.json", self.settings())
        write_json(run / "suite.json", suite)
        write_jsonl(run / "baseline-predictions.jsonl", before)
        write_jsonl(run / "candidate-predictions.jsonl", after)
        write_json(run / "judgments.json", [])
        write_json(run / "receipts.json", [])
        write_json(run / "evaluation.json", report)
        write_json(run / "baseline.json", self.registry.state()["actor"])
        write_json(run / "training/run.json", {"artifact": candidate["artifact"], "artifact_sha256": candidate["version"]})
        self.assertTrue(release_actor(run, self.registry)["promote"])
        self.registry.rollback("actor")
        unchanged_registry = file_hash(self.registry.path)
        after[0]["text"] = "Edited after the evaluation"
        write_jsonl(run / "candidate-predictions.jsonl", after)
        with self.assertRaisesRegex(ValueError, "evidence or policy changed"):
            release_actor(run, self.registry)
        self.assertEqual(file_hash(self.registry.path), unchanged_registry)

    def test_proposer_release_binds_candidate_actor_and_trial_versions_before_writes(self):
        prior = self.candidate("prior-proposer")
        candidate = self.candidate("new-proposer")
        self.registry.initialize("proposer", prior["version"], prior["artifact"])
        expected = self.registry.state()["proposer"]
        original = file_hash(self.registry.path)
        trial = {"baseline_proposer_version": expected["version"], "candidate_proposer_version": candidate["version"]}
        with patch("rsi.strategies.compare_meta", side_effect=AssertionError("Invalid artifact reached comparison")) as comparison:
            for field in ("baseline_proposer_version", "candidate_proposer_version"):
                bad_trial = {**trial, field: "wrong-proposer"}
                with self.assertRaisesRegex(ValueError, "different proposer artifacts"):
                    self.registry.promote_proposer(candidate, [bad_trial], "actor-one", digest("budget"), [], expected=expected)
                self.assertEqual(file_hash(self.registry.path), original)
            comparison.assert_not_called()
        with patch("rsi.strategies.compare_meta", return_value={"promote": True}):
            with self.assertRaisesRegex(ValueError, "Actor or proposer changed"):
                self.registry.promote_proposer(candidate, [trial], "different-actor", digest("budget"), [], expected=expected)
        self.assertEqual(file_hash(self.registry.path), original)
        (Path(candidate["artifact"]) / "adapter_model.safetensors").write_bytes(b"artifact changed")
        with patch("rsi.strategies.compare_meta", side_effect=AssertionError("Changed artifact reached comparison")) as comparison:
            with self.assertRaisesRegex(ValueError, "Proposer artifact changed"):
                self.registry.promote_proposer(candidate, [trial], "actor-one", digest("budget"), [], expected=expected)
            comparison.assert_not_called()
        self.assertEqual(file_hash(self.registry.path), original)

    def test_client_plan_never_opens_network(self):
        with patch("urllib.request.urlopen", side_effect=AssertionError("Unexpected network request")):
            result = completion({"endpoint": "http://127.0.0.1:18083/v1", "model": "synthetic"},
                                [{"role": "system", "content": "Rules"}], execute=False)
        self.assertIs(result["execute"], False)

    def test_unapproved_remote_connection_is_rejected_before_network(self):
        with patch("urllib.request.urlopen", side_effect=AssertionError("Unexpected network request")):
            with self.assertRaisesRegex(ValueError, "explicit connection policy"):
                completion({"endpoint": "https://example.com/v1", "model": "synthetic"}, [], execute=True)

    def test_cli_contracts_load_without_optional_training_dependencies(self):
        for command in (None, "cycle", "goals", "train", "infer", "meta-build", "meta-evaluate", "watch"):
            result = subprocess.run([sys.executable, "-m", "rsi", *([command] if command else []), "--help"],
                                    cwd=Path(__file__).resolve().parents[2], capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main()
