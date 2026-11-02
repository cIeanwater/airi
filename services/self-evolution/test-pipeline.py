"""Synthetic regression tests. No private data, network, GPU, or optimizer steps."""

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from evaluation import paired_report, prediction_hash, promotion_gate, suite_check
from evolution import artifact_hash, cycle, promote, proposal_check, rollback
from pipeline import assemble, content_hash, digest, lines, make_candidate, mix_rows, read, request_candidate, reviewed, snapshot, split_rows, write, write_lines
from proposal import suggest
from teacher import propose
from training import encode, load_dataset, model_identity


TEST_TEMP = Path(__file__).resolve().parent / "private/test-temp"
TEST_TEMP.mkdir(parents=True, exist_ok=True)
tempfile.tempdir = str(TEST_TEMP)


def candidate(i=0, kind="base"):
    return make_candidate([{"role": "user", "content": f"合成问题{i}"}, {"role": "assistant", "content": f"合成回答{i}"}],
                          kind, f"session-{i}", [f"source-{i}"], i)


def decision(row):
    return {"id": row["id"], "content_sha256": row["content_sha256"], "approved": True,
            "reviewer": "synthetic-human", "privacy_checked": True, "evidence_checked": True}


def passing_report():
    return {"n": 100, "independent_groups": 100, "gain_lower_95": 0.1, "missing_independent_reviews": [],
            "slices": {k: {"n": 20, "gain": 0.1} for k in ("base", "persona", "reasoning", "memory", "tools")}}


class Template:
    chat_template = "<|channel>"
    eos_token_id = 1

    def apply_chat_template(self, messages, tokenize=False, add_generation_prompt=False, enable_thinking=False, tools=None):
        text = "<bos>" + ("<think>" if enable_thinking else "")
        if tools:
            text += str(tools)
        for message in messages:
            text += "<turn>" + message["role"] + "\n"
            if message.get("reasoning_content"):
                text += "<|channel>thought\n" + message["reasoning_content"] + "\n<channel|>"
            text += message["content"] + "<end>\n"
        return text + ("<turn>assistant\n" if add_generation_prompt else "")

    def encode(self, text, add_special_tokens=False):
        return [ord(c) for c in text]


class PipelineTests(unittest.TestCase):
    def test_unreviewed_never_trains(self):
        self.assertEqual(reviewed([candidate()], [])[0], [])

    def test_changed_candidate_invalidates_review(self):
        row = candidate()
        review = decision(row)
        row["messages"][-1]["content"] = "修改后的回答"
        with self.assertRaises(ValueError):
            reviewed([row], [review])

    def test_incomplete_and_multimodal_requests(self):
        with self.assertRaisesRegex(ValueError, "incomplete_response"):
            request_candidate({})
        value = {"status": 200, "request": {"messages": [{"role": "user", "content": []}]},
                 "response": {"choices": [{"finish_reason": "stop", "message": {"role": "assistant", "content": "ok"}}]}}
        with self.assertRaisesRegex(ValueError, "multimodal"):
            request_candidate(value)

    def test_gateway_preserves_effective_prompt_and_tools(self):
        tools = [{"type": "function", "function": {"name": "lookup", "parameters": {"type": "object", "properties": {}}}}]
        value = {"schema_version": 1, "status": "ok", "http_status": 200,
                 "request": {"messages": [{"role": "user", "content": "原始输入"}]},
                 "model_input": {"messages": [{"role": "user", "content": "实际模型输入"}], "tools": tools},
                 "response": {"choices": [{"finish_reason": "stop", "message": {"role": "assistant", "content": "合成答复"}}]}}
        row = request_candidate(value)
        self.assertEqual(row["messages"][0]["content"], "实际模型输入")
        self.assertEqual(row["tools"], tools)
        self.assertIn("tool_context_requires_review", row["issues"])
        encoded = encode(row, Template(), 2000, "none")
        self.assertIn("lookup", "".join(chr(c) for c in encoded["input_ids"]))
        value["response"]["choices"][0]["message"]["tool_calls"] = [{"id": "call"}]
        with self.assertRaisesRegex(ValueError, "tool_protocol"):
            request_candidate(value)

    def test_secrets_and_privacy(self):
        row = candidate()
        row["messages"][-1]["content"] = "sk-" + "x" * 24
        row["content_sha256"] = content_hash(row)
        self.assertEqual(reviewed([row], [decision(row)])[1], {"secret": 1})
        row["messages"][-1]["content"] = "test@example.com"
        row["content_sha256"] = content_hash(row)
        self.assertEqual(reviewed([row], [decision(row)])[1], {"pii": 1})

    def test_reasoning_requires_faithfulness_and_chinese(self):
        row = candidate(kind="reasoning")
        row["short_reasoning"] = "证据不足，先核对记录。"
        row["content_sha256"] = content_hash(row)
        review = decision(row)
        self.assertEqual(reviewed([row], [review])[1], {"reasoning_unverified": 1})
        review.update(faithfulness_checked=True, answer_checked=True)
        self.assertEqual(len(reviewed([row], [review])[0]), 1)
        row["short_reasoning"] = "English only"
        row["content_sha256"] = content_hash(row)
        review["content_sha256"] = row["content_sha256"]
        self.assertEqual(reviewed([row], [review])[1], {"reasoning_language_or_budget": 1})

    def test_transitive_lineage_never_crosses_splits(self):
        rows = [candidate(i) for i in range(30)]
        rows[29]["source_ids"] = rows[0]["source_ids"]
        rows[15]["group"] = rows[0]["group"]
        rows[16]["messages"][:-1] = rows[15]["messages"][:-1]
        splits = split_rows(rows)
        labels = {r["id"]: s for s, rs in splits.items() for r in rs}
        self.assertEqual(len({labels[rows[i]["id"]] for i in (0, 15, 16, 29)}), 1)
        self.assertTrue(splits["test"])

    def test_mix_never_silently_repeats_or_drops_replay(self):
        with self.assertRaises(ValueError):
            mix_rows([candidate()], {"base": 0.1, "dialogue": 0.9, "persona": 0, "reasoning": 0}, 100)
        with self.assertRaisesRegex(ValueError, "no usable"):
            mix_rows([candidate()], {"base": 0.5, "dialogue": 0.5, "persona": 0, "reasoning": 0}, 20)

    def test_snapshot_deduplicates_copies(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            event = {"id": "e", "scope": {"userId": "u", "characterId": "c"}, "kind": "message", "actor": "user",
                     "status": "received", "sessionId": "s", "channel": "desktop", "text": "合成", "occurredAt": 1}
            for name in ("one", "two"):
                write(root / name / "scope/archive/events/event.json", event)
            events, copies, _ = snapshot([root / "one", root / "two"])
            self.assertEqual((len(events), copies), (1, 1))
            event["text"] = "冲突"
            write(root / "two/scope/archive/events/event.json", event)
            with self.assertRaisesRegex(ValueError, "Conflicting"):
                snapshot([root / "one", root / "two"])

    def test_build_manifest_and_tamper_detection(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            rows = [candidate(i) for i in range(30)]
            write_lines(root / "candidates.jsonl", rows)
            write_lines(root / "reviews.jsonl", [decision(r) for r in rows])
            result = assemble(root / "candidates.jsonl", root / "reviews.jsonl", root / "dataset",
                              {"base": 1, "dialogue": 0, "persona": 0, "reasoning": 0}, 100)
            self.assertEqual(result["splits"]["test"], 3)
            self.assertTrue(load_dataset(root / "dataset")["train"])
            (root / "dataset/train.jsonl").write_text("", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "manifest mismatch"):
                load_dataset(root / "dataset")

    def test_loss_masks_prompt_and_keeps_native_turn_end(self):
        row = candidate()
        encoded = encode(row, Template(), 1000, "none")
        start = next(i for i, value in enumerate(encoded["labels"]) if value != -100)
        self.assertTrue(all(v == -100 for v in encoded["labels"][:start]))
        self.assertEqual("".join(chr(c) for c in encoded["labels"][start:]), "合成回答0<end>\n")
        self.assertIsNone(encode(row, Template(), 2, "none"))

    def test_gemma_native_reasoning_and_summary_are_distinct(self):
        row = candidate(kind="reasoning")
        row.update(short_reasoning="证据不足，先核对。", reasoning_source="已有合成思考")
        encoded = encode(row, Template(), 1000, "gemma4-native")
        target = "".join(chr(c) for c in encoded["labels"] if c != -100)
        self.assertIn("<|channel>thought\n证据不足", target)
        self.assertIn("合成回答0", target)
        summary = encode(row, Template(), 1000, "summary")
        self.assertNotIn("合成回答0", "".join(chr(c) for c in summary["labels"] if c != -100))

    def test_teacher_dry_run_has_no_network(self):
        row = candidate()
        row["reasoning_source"] = "已有合成思考"
        with patch("urllib.request.urlopen", side_effect=AssertionError("network")):
            outputs, report = propose([row], "test", 10, 1000)
        self.assertFalse(report["execute"])
        self.assertEqual(outputs, [])

    def test_gguf_is_not_a_training_base(self):
        with tempfile.TemporaryDirectory() as temporary:
            (Path(temporary) / "model.gguf").write_bytes(b"synthetic")
            with self.assertRaisesRegex(ValueError, "config.json"):
                model_identity(temporary)

    def test_evaluation_rejects_overlap(self):
        row = candidate()
        case = {"id": "x", "kind": "base", "messages": row["messages"][:-1], "group": "new", "source_ids": ["new"], "expect": {"all": ["回答"]}}
        with self.assertRaisesRegex(ValueError, "overlaps"):
            suite_check([case], [row])

    def test_missing_reviews_and_small_suite_cannot_promote(self):
        cases = [{"id": "x", "kind": "persona", "expect": {"all": ["你好"]}}]
        predictions = [{"id": "x", "text": "你好", "tokens": 2}]
        report = paired_report(cases, predictions, predictions)
        self.assertFalse(promotion_gate(report)["promote"])
        self.assertEqual(len(report["missing_independent_reviews"]), 2)

    def test_invalid_review_scores_and_missing_predictions(self):
        cases = [{"id": "x", "kind": "persona", "expect": {"all": ["你好"]}}]
        predictions = [{"id": "x", "text": "你好", "tokens": 2}]
        with self.assertRaises(ValueError):
            paired_report(cases, predictions, [])
        with self.assertRaisesRegex(ValueError, "Invalid independent"):
            paired_report(cases, predictions, predictions, [{"id": "x", "prediction_sha256": prediction_hash(predictions[0]), "score": float("nan"), "independent": True, "reviewer": "test"}])

    def test_promote_rollback_and_stale_champion(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            adapter = root / "adapter"
            adapter.mkdir()
            (adapter / "adapter_model.safetensors").write_bytes(b"synthetic")
            report = {**passing_report(), "candidate_artifact_sha256": artifact_hash(adapter), "baseline_champion_sha256": digest(None)}
            registry = root / "registry.json"
            self.assertTrue(promote(registry, adapter, report, None)["promote"])
            with self.assertRaisesRegex(ValueError, "changed"):
                promote(registry, adapter, report, None)
            self.assertIsNone(rollback(registry)["champion"])
            (adapter / "adapter_model.safetensors").write_bytes(b"changed")
            with self.assertRaisesRegex(ValueError, "bound"):
                promote(registry, adapter, report, None)

    def test_proposal_dry_run_and_bounds(self):
        config = read(Path(__file__).with_name("config.example.json"))
        with patch("urllib.request.urlopen", side_effect=AssertionError("network")):
            self.assertFalse(suggest(config, {}, {}, "http://127.0.0.1:18083/v1", "synthetic")["execute"])
        with self.assertRaises(ValueError):
            suggest(config, {}, {}, "https://example.com", "synthetic")
        proposal = {"training": config, "mix": {"base": 0.5, "dialogue": 0.2, "persona": 0.2, "reasoning": 0.1}, "character_budget": 1000, "rationale": "合成"}
        self.assertFalse(cycle(proposal, "missing", "missing", "missing", "unused", "missing")["execute"])
        proposal["mix"]["base"] = float("nan")
        with self.assertRaises(ValueError):
            proposal_check(proposal)

    def test_cli_contracts(self):
        root = Path(__file__).parent
        for script, command in (("pipeline.py", "curate"), ("pipeline.py", "review-template"), ("training.py", "infer"),
                                ("evolution.py", "cycle"), ("evolution.py", "evaluate"), ("teacher.py", None), ("proposal.py", None)):
            result = subprocess.run([sys.executable, str(root / script), *([command] if command else []), "--help"], capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main()
