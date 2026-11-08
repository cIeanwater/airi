"""Reproducible capture snapshots and prompt-driven local review packets."""

import argparse
import collections
import json
import re
from pathlib import Path

from .common import digest, file_hash, read_json, read_jsonl, utc_now, write_json, write_jsonl


def snapshot(collections_map, output):
    out = Path(output)
    if out.exists():
        raise ValueError("Use a new snapshot directory")
    manifest = []
    for name, directory in collections_map.items():
        if name not in {"events", "companion", "gateway"}:
            raise ValueError("Unknown capture collection")
        root = Path(directory)
        paths = sorted(root.glob("*/archive/events/*.json")) if name == "events" else sorted(root.glob("*.json"))
        for path in paths:
            destination = out / name / path.relative_to(root)
            data = path.read_bytes()
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(data)
            manifest.append({"collection": name, "path": path.relative_to(root).as_posix(),
                             "bytes": len(data), "sha256": file_hash(destination)})
    if not manifest:
        raise ValueError("No capture files exist in the configured collections")
    write_json(out / "manifest.json", {"created_at": utc_now(), "files": manifest, "snapshot_sha256": digest(manifest)})
    return {"files": len(manifest), "snapshot_sha256": digest(manifest)}


def _json_content(text):
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text.strip())
    try:
        return json.loads(text)
    except ValueError:
        return None


def _decision(row):
    answer = row["messages"][-1]["content"]
    parsed = _json_content(answer)
    if row["kind"] == "initiative" and isinstance(parsed, dict):
        if parsed.get("speak") is False:
            state = _json_content(next((m["content"] for m in reversed(row["messages"][:-1]) if m["role"] == "user"), "{}")) or {}
            if not isinstance(state, dict):
                return ["silence-unstructured", row["id"]]
            engagement = state.get("engagement", {})
            return ["silence", engagement.get("phase"), engagement.get("canSpeak"), bool(state.get("observation")),
                    min(len(state.get("conversation") or []), 2)]
        return ["initiative", parsed.get("speak"), re.sub(r"\s+", "", parsed.get("text", ""))]
    if answer.strip() == "NO_UPDATE":
        # Retain negative examples from different days rather than hundreds of repeated targets.
        return ["no-update", int(row.get("occurred_at", 0) // 86400000)]
    return [row["kind"], re.sub(r"\s+", "", answer)]


def _view(value):
    if isinstance(value, dict):
        return {k: _view(v) for k, v in value.items() if k not in {"raw", "provenance", "generationTranscript", "assets", "slices", "tool_results"}}
    if isinstance(value, list):
        return [_view(v) for v in value]
    return value


def review_batches(candidates, output, batch_size=30):
    from .cleaning import content_hash

    if type(batch_size) is not int or not 1 <= batch_size <= 100:
        raise ValueError("Batch size must range from one to one hundred")
    out = Path(output)
    if out.exists():
        raise ValueError("Use a new review packet directory")
    rows = read_jsonl(candidates)
    if len({row["id"] for row in rows}) != len(rows) or any(row.get("content_sha256") != content_hash(row) for row in rows):
        raise ValueError("Review packets require unique, unchanged candidates")
    representatives, suppressed = {}, []
    for row in sorted(rows, key=lambda r: (r.get("occurred_at", 0), r["id"]), reverse=True):
        key = digest(_decision(row))
        if key in representatives:
            suppressed.append({"candidate_id": row["id"], "candidate_sha256": row["content_sha256"],
                               "reviewer": "deterministic-diversity-selection", "decision": "quarantine", "kind": row["kind"],
                               "short_reasoning": "", "reason_codes": ["redundancy_downsampled_not_semantic_failure"],
                               "duplicate_of": representatives[key]["id"], "checks": {}})
        else:
            representatives[key] = row
    selected = sorted(representatives.values(), key=lambda r: (r["kind"], r.get("occurred_at", 0), r["id"]))
    packets = []
    for index, row in enumerate(selected):
        context, truncated = [], False
        for message in row["messages"][:-1]:
            content = message.get("content", "")
            parsed = _json_content(content)
            view = json.dumps(_view(parsed), ensure_ascii=False) if parsed is not None else content
            limit = 6500 if message["role"] == "system" else 12000
            truncated |= len(view) > limit
            context.append({"role": message["role"], "content": view[:limit],
                            **({"tool_calls": message["tool_calls"]} if message.get("tool_calls") else {})})
        target = row["messages"][-1]
        packets.append({"candidate_id": row["id"], "candidate_sha256": row["content_sha256"], "kind": row["kind"],
                        "review_required": row["review_required"], "context": context, "context_view_truncated": truncated,
                        "answer": target["content"], "raw_reasoning": target.get("reasoning_content", ""),
                        "tools": row.get("tools", []), "full_candidate_file": str(Path(candidates).resolve())})
    write_jsonl(out / "selected.jsonl", selected)
    write_jsonl(out / "suppressed-reviews.jsonl", suppressed)
    for i in range(0, len(packets), batch_size):
        write_jsonl(out / ("batch-" + str(i // batch_size).zfill(3) + ".jsonl"), packets[i:i + batch_size])
    report = {"candidates": len(rows), "semantic_review_candidates": len(selected), "redundancy_quarantined": len(suppressed),
              "batches": (len(selected) + batch_size - 1) // batch_size,
              "kinds": dict(collections.Counter(r["kind"] for r in selected)), "selection_sha256": digest([r["id"] for r in selected])}
    write_json(out / "manifest.json", report)
    return report


def collect_reviews(candidates, review_files, output):
    from .cleaning import apply_review

    out = Path(output)
    if out.exists():
        raise ValueError("Use a new combined review file")
    originals = {row["id"]: row for row in read_jsonl(candidates)}
    reviews = {}
    for path in review_files:
        for review in read_jsonl(path):
            identity = review.get("candidate_id")
            if identity not in originals or identity in reviews:
                raise ValueError("Review identity is unknown or repeated")
            _, reasons = apply_review(originals[identity], review)
            if review["decision"] == "accept" and reasons:
                raise ValueError("Accepted review failed checks: " + identity + ": " + ",".join(reasons))
            reviews[identity] = review
    if set(reviews) != set(originals):
        raise ValueError("Every candidate needs a review or an explicit quarantine decision")
    ordered = [reviews[key] for key in sorted(reviews)]
    write_jsonl(out, ordered)
    return {"reviews": len(ordered), "decisions": dict(collections.Counter(r["decision"] for r in ordered)),
            "review_file_sha256": file_hash(out)}


def budget_reviews(dataset, reviews_path, token_report, output):
    """Quarantine overlength targets without truncation or semantic relabeling."""
    from .finetune import inspect_dataset

    out = Path(output)
    if out.exists():
        raise ValueError("Use a new budget review file")
    splits, inspected = inspect_dataset(dataset)
    report = read_json(token_report)
    if report.get("dataset", {}).get("manifest_sha256") != inspected["manifest_sha256"]:
        raise ValueError("Token report belongs to a different dataset")
    limit = report.get("max_length")
    if type(limit) is not int or limit < 128:
        raise ValueError("Token report needs a context budget")
    rows = {r["id"]: r for values in splits.values() for r in values}
    expected = {(r["id"], i) for r in rows.values() for i in r["train_indices"]}
    seen, excluded = set(), set()
    for detail in report.get("rows", []):
        key = (detail.get("id"), detail.get("target_index"))
        if key not in expected or key in seen or type(detail.get("tokens")) is not int:
            raise ValueError("Token report target coverage changed")
        seen.add(key)
        over = detail["tokens"] > limit
        if over:
            if detail.get("status") != "rejected" or detail.get("reason") != "sequence_exceeds_configured_context":
                raise ValueError("Token report has an unresolved template error")
            excluded.add(detail["id"])
        elif detail.get("status") != "accepted":
            raise ValueError("Token report has an unresolved template error")
    if seen != expected:
        raise ValueError("Token report target coverage is incomplete")
    reviews = read_jsonl(reviews_path)
    accepted = [r for r in reviews if r.get("decision") == "accept"]
    if len(accepted) != len(rows) or {r["candidate_id"] for r in accepted} != set(rows):
        raise ValueError("Reviews do not describe the tokenized selection")
    for review in accepted:
        row = rows[review["candidate_id"]]
        if row.get("review", {}).get("original_candidate_sha256") != review.get("candidate_sha256"):
            raise ValueError("Budget review is not bound to the reviewed source")
        if review["candidate_id"] in excluded:
            primary = digest(review)
            review.update(reviewer="deterministic-context-budget", decision="quarantine", short_reasoning="",
                          reason_codes=["native_sequence_exceeds_" + str(limit) + "_tokens"], checks={}, resolved_issues=[],
                          primary_review_sha256=primary)
    write_jsonl(out, reviews)
    return {"max_length": limit, "selected": len(rows) - len(excluded), "quarantined_for_context": len(excluded),
            "truncated": 0, "source_manifest_sha256": inspected["manifest_sha256"], "token_report_sha256": file_hash(token_report)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    sub = commands.add_parser("snapshot")
    sub.add_argument("--collections", required=True)
    sub.add_argument("--output", required=True)
    sub = commands.add_parser("extract")
    for name in ("snapshot", "output", "scope"):
        sub.add_argument("--" + name, required=True)
    sub.add_argument("--policy")
    sub = commands.add_parser("batches")
    sub.add_argument("--candidates", required=True)
    sub.add_argument("--output", required=True)
    sub.add_argument("--batch-size", type=int, default=30)
    sub = commands.add_parser("collect-reviews")
    sub.add_argument("--candidates", required=True)
    sub.add_argument("--reviews", required=True, action="append")
    sub.add_argument("--output", required=True)
    sub = commands.add_parser("budget-reviews")
    for name in ("dataset", "reviews", "token-report", "output"):
        sub.add_argument("--" + name, required=True)
    sub = commands.add_parser("finalize")
    for name in ("candidates", "reviews", "output"):
        sub.add_argument("--" + name, required=True)
    sub.add_argument("--min-groups", type=int, default=10)
    args = parser.parse_args()
    if args.command == "snapshot":
        result = snapshot(read_json(args.collections), args.output)
    elif args.command == "extract":
        from .cleaning import extract
        result = extract(args.snapshot, args.output, read_json(args.scope), policy=read_json(args.policy) if args.policy else None)
    elif args.command == "batches":
        if not 1 <= args.batch_size <= 100:
            parser.error("Batch size must range from one to one hundred")
        result = review_batches(args.candidates, args.output, args.batch_size)
    elif args.command == "collect-reviews":
        result = collect_reviews(args.candidates, args.reviews, args.output)
    elif args.command == "budget-reviews":
        result = budget_reviews(args.dataset, args.reviews, args.token_report, args.output)
    else:
        from .cleaning import finalize
        result = finalize(args.candidates, args.reviews, args.output, min_groups=args.min_groups)
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
