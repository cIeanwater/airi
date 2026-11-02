"""Paired held-out evaluation with content-bound independent style judgments."""

import collections
import math
import random
import re

from pipeline import digest


def prediction_hash(prediction):
    return digest({"text": prediction["text"], "reasoning_summary": prediction.get("reasoning_summary", "")})


def suite_check(cases, dataset_rows):
    ids = [c["id"] for c in cases]
    if len(ids) != len(set(ids)) or not cases:
        raise ValueError("Evaluation case IDs must be unique and nonempty")
    sources = {s for r in dataset_rows for s in r["source_ids"]}
    groups = {r["group"] for r in dataset_rows}
    prompts = {digest(r["messages"][:-1]) for r in dataset_rows}
    for case in cases:
        if case["kind"] not in {"base", "persona", "reasoning", "memory", "tools"}:
            raise ValueError("Unknown evaluation slice")
        if not case.get("group") or not case.get("source_ids"):
            raise ValueError("Evaluation cases need source lineage and group metadata")
        if case["group"] in groups or set(case["source_ids"]) & sources or digest(case["messages"]) in prompts:
            raise ValueError("Evaluation suite overlaps training or validation data")
        expect = case.get("expect", {})
        if not any(expect.get(k) for k in ("all", "any", "exact")):
            raise ValueError("Every case needs a positive correctness assertion")


def case_score(case, prediction):
    text = prediction["text"]
    expect = case["expect"]
    checks = [prediction.get("complete", True)]
    if expect.get("exact"):
        checks.append(text.strip() in expect["exact"])
    if expect.get("all"):
        checks.append(all(v in text for v in expect["all"]))
    if expect.get("any"):
        checks.append(any(v in text for v in expect["any"]))
    if expect.get("none"):
        checks.append(not any(v in text for v in expect["none"]))
    if expect.get("max_chars"):
        checks.append(len(text) <= expect["max_chars"])
    if expect.get("chinese"):
        chinese = len(re.findall(r"[\u4e00-\u9fff]", text))
        letters = len(re.findall(r"[A-Za-z\u4e00-\u9fff]", text))
        checks.append(chinese / max(1, letters) >= 0.5)
    if expect.get("reasoning_max_chars"):
        summary = prediction.get("reasoning_summary", "")
        checks.append(bool(summary) and len(summary) <= expect["reasoning_max_chars"])
        chinese = len(re.findall(r"[\u4e00-\u9fff]", summary))
        letters = len(re.findall(r"[A-Za-z\u4e00-\u9fff]", summary))
        checks.append(chinese / max(1, letters) >= 0.5)
    return float(all(checks))


def paired_report(cases, baseline, candidate, judgments=None):
    def indexed(values):
        result = {v["id"]: v for v in values}
        if len(result) != len(values) or set(result) != {c["id"] for c in cases}:
            raise ValueError("Predictions must cover every evaluation ID exactly once")
        return result

    before, after = indexed(baseline), indexed(candidate)
    reviewed = {}
    for judgment in judgments or []:
        key = (judgment["id"], judgment["prediction_sha256"])
        if key in reviewed:
            raise ValueError("Duplicate independent judgment")
        reviewed[key] = judgment
    deltas, slices, missing = [], collections.defaultdict(list), []
    group_deltas = collections.defaultdict(list)
    tokens = {"baseline": [], "candidate": []}
    for case in cases:
        values = []
        for label, prediction in (("baseline", before[case["id"]]), ("candidate", after[case["id"]])):
            score = case_score(case, prediction)
            if case["kind"] in {"persona", "reasoning"}:
                review = reviewed.get((case["id"], prediction_hash(prediction)))
                if not review or not review.get("independent") or not review.get("reviewer"):
                    missing.append([case["id"], label])
                else:
                    value = review["score"]
                    if not isinstance(value, (int, float)) or not math.isfinite(value) or not 0 <= value <= 1:
                        raise ValueError("Invalid independent review score")
                    score *= value
            values.append(score)
            token_count = prediction["tokens"]
            if not isinstance(token_count, int) or token_count < 0:
                raise ValueError("Invalid generated token count")
            tokens[label].append(token_count)
        delta = values[1] - values[0]
        deltas.append(delta)
        group_deltas[case.get("group", case["id"])].append(delta)
        slices[case["kind"]].append(delta)
    rng = random.Random(42)
    groups = list(group_deltas.values())
    bootstrap = []
    for _ in range(2000):
        sampled = [v for group in rng.choices(groups, k=len(groups)) for v in group]
        bootstrap.append(sum(sampled) / len(sampled))
    bootstrap.sort()
    return {"n": len(cases), "independent_groups": len(groups), "suite_sha256": digest(cases), "baseline_predictions_sha256": digest(baseline),
            "candidate_predictions_sha256": digest(candidate), "mean_gain": sum(deltas) / len(deltas),
            "gain_lower_95": bootstrap[49],
            "slices": {k: {"n": len(v), "gain": sum(v) / len(v)} for k, v in slices.items()},
            "mean_tokens": {k: sum(v) / len(v) for k, v in tokens.items()},
            "missing_independent_reviews": missing}


def promotion_gate(report):
    reasons = []
    if report["n"] < 100:
        reasons.append("At least 100 held-out cases are required")
    if report.get("independent_groups", 0) < 30:
        reasons.append("At least 30 independent evaluation groups are required")
    for kind in ("base", "persona", "reasoning", "memory", "tools"):
        entry = report["slices"].get(kind, {})
        if entry.get("n", 0) < 20:
            reasons.append("At least 20 cases are required for " + kind)
        if entry.get("gain", -1) < -0.02:
            reasons.append("Regression exceeds two percentage points for " + kind)
    if report["gain_lower_95"] <= 0:
        reasons.append("Paired improvement is not supported by the bootstrap interval")
    if report["missing_independent_reviews"]:
        reasons.append("Persona and reasoning outputs need independent content-bound reviews")
    return {"promote": not reasons, "reasons": reasons}
