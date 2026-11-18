"""Review selection tests use synthetic capture records."""

import tempfile
import unittest
from pathlib import Path

from rsi.cleaning import content_hash
from rsi.common import digest, file_hash, read_jsonl, write_json, write_jsonl
from rsi.curate import budget_reviews, collect_reviews, review_batches, snapshot


def candidate(identity, answer, occurred_at):
    row = {"id": identity, "kind": "dialogue", "occurred_at": occurred_at,
           "messages": [{"role": "system", "content": "Use the provided facts."},
                        {"role": "assistant", "content": answer, "reasoning_content": "已确认。"}],
           "review_required": ["semantic_quality_review"], "tools": []}
    row["content_sha256"] = content_hash(row)
    return row


class CurateTests(unittest.TestCase):
    def setUp(self):
        parent = Path(__file__).resolve().parents[1] / "private" / "test-temp"
        parent.mkdir(parents=True, exist_ok=True)
        temporary = tempfile.TemporaryDirectory(dir=parent)
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)

    def test_redundancy_is_quarantined_without_semantic_acceptance(self):
        source = self.root / "candidates.jsonl"
        write_jsonl(source, [candidate("earlier", "相同答案。", 1), candidate("later", "相同答案。", 2)])
        result = review_batches(source, self.root / "packets", 1)
        self.assertEqual(result["semantic_review_candidates"], 1)
        selected = read_jsonl(self.root / "packets/selected.jsonl")
        suppressed = read_jsonl(self.root / "packets/suppressed-reviews.jsonl")
        self.assertEqual(selected[0]["id"], "later")
        self.assertEqual(suppressed[0]["decision"], "quarantine")
        self.assertEqual(suppressed[0]["duplicate_of"], "later")
        self.assertEqual(suppressed[0]["checks"], {})

    def test_changed_candidate_cannot_receive_a_bound_review_packet(self):
        row = candidate("one", "原始内容。", 1)
        row["messages"][-1]["content"] = "修改内容。"
        source = self.root / "changed.jsonl"
        write_jsonl(source, [row])
        with self.assertRaisesRegex(ValueError, "unchanged"):
            review_batches(source, self.root / "packets")

    def test_review_collection_rejects_missing_duplicate_or_stale_decisions(self):
        row = candidate("one", "原始内容。", 1)
        source = self.root / "candidates.jsonl"
        write_jsonl(source, [row])
        path = self.root / "reviews.jsonl"
        review = {"candidate_id": "one", "candidate_sha256": row["content_sha256"],
                  "reviewer": "synthetic", "decision": "quarantine", "reason_codes": ["needs_context"]}
        write_jsonl(path, [])
        with self.assertRaisesRegex(ValueError, "Every candidate"):
            collect_reviews(source, [path], self.root / "missing.jsonl")
        write_jsonl(path, [review, review])
        with self.assertRaisesRegex(ValueError, "repeated"):
            collect_reviews(source, [path], self.root / "duplicate.jsonl")
        review["candidate_sha256"] = "changed"
        write_jsonl(path, [review])
        with self.assertRaisesRegex(ValueError, "not bound"):
            collect_reviews(source, [path], self.root / "stale.jsonl")

    def test_long_context_is_flagged_and_full_source_remains_unchanged(self):
        row = candidate("long", "结论。", 1)
        row["messages"][0]["content"] = "x" * 7000
        row["content_sha256"] = content_hash(row)
        source = self.root / "long.jsonl"
        write_jsonl(source, [row])
        original = source.read_bytes()
        review_batches(source, self.root / "packets")
        packet = read_jsonl(self.root / "packets/batch-000.jsonl")[0]
        self.assertTrue(packet["context_view_truncated"])
        self.assertEqual(packet["full_candidate_file"], str(source.resolve()))
        self.assertEqual(source.read_bytes(), original)

    def test_snapshot_preserves_exact_source_bytes_and_rejects_overwrite(self):
        capture = self.root / "capture"
        capture.mkdir()
        (capture / "one.json").write_bytes(b'{"status": 200}\n')
        out = self.root / "snapshot"
        result = snapshot({"companion": capture}, out)
        self.assertEqual(result["files"], 1)
        self.assertEqual((out / "companion/one.json").read_bytes(), (capture / "one.json").read_bytes())
        with self.assertRaisesRegex(ValueError, "new snapshot"):
            snapshot({"companion": capture}, out)

    def test_context_budget_quarantines_long_rows_without_changing_source_or_answers(self):
        dataset = self.root / "dataset"
        rows, reviews = [], []
        for identity in ("short", "long"):
            row = candidate(identity, "保留答案。", 1)
            row.update(group=identity, source_ids=[identity], scope={"user_id": "fixture", "character_id": "fixture"},
                       train_indices=[1], review={"original_candidate_sha256": "original-" + identity})
            row["content_sha256"] = content_hash(row)
            rows.append(row)
            reviews.append({"candidate_id": identity, "candidate_sha256": "original-" + identity, "decision": "accept", "reviewer": "fixture"})
        for split in ("train", "validation", "test"):
            write_jsonl(dataset / (split + ".jsonl"), rows if split == "train" else [])
        manifest = {"files": {split: file_hash(dataset / (split + ".jsonl")) for split in ("train", "validation", "test")}}
        write_json(dataset / "manifest.json", manifest)
        original = (dataset / "train.jsonl").read_bytes()
        token_report = {"dataset": {"manifest_sha256": digest(manifest)}, "max_length": 4096, "rows": [
            {"id": "short", "target_index": 1, "tokens": 512, "status": "accepted"},
            {"id": "long", "target_index": 1, "tokens": 5000, "status": "rejected", "reason": "sequence_exceeds_configured_context"}]}
        write_json(self.root / "tokens.json", token_report)
        write_jsonl(self.root / "reviews.jsonl", reviews)
        result = budget_reviews(dataset, self.root / "reviews.jsonl", self.root / "tokens.json", self.root / "budget.jsonl")
        self.assertEqual(result["selected"], 1)
        filtered = read_jsonl(self.root / "budget.jsonl")
        self.assertEqual(filtered[0]["decision"], "accept")
        self.assertEqual(filtered[1]["decision"], "quarantine")
        self.assertEqual((dataset / "train.jsonl").read_bytes(), original)
        token_report["dataset"]["manifest_sha256"] = "another-dataset"
        write_json(self.root / "tokens.json", token_report)
        with self.assertRaisesRegex(ValueError, "different dataset"):
            budget_reviews(dataset, self.root / "reviews.jsonl", self.root / "tokens.json", self.root / "wrong-budget.jsonl")


if __name__ == "__main__":
    unittest.main()
