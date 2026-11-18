"""Outcome evidence, paired evaluation, and update diagnosis."""

import collections
import json
import math
import random
import re

from .common import digest


SLICES = {"base", "persona", "memory", "tools", "initiative", "reasoning"}
DEFAULT_POLICY = {
    "min_cases": 100,
    "min_groups": 30,
    "min_per_slice": 20,
    "min_groups_per_slice": 10,
    "required_slices": ["base", "persona", "memory", "tools", "initiative"],
    "protected_slices": ["base", "memory", "tools"],
    "target_slices": ["persona", "initiative"],
    "regression_margin": 0.02,
    "minimum_gain": 0.0,
    "maximum_token_ratio": 1.2,
    "maximum_latency_ratio": 1.5,
    "bootstrap_samples": 2000,
    "trusted_evidence_sources": ["replay", "tool-runtime"],
    "judges": {},
}


def _number(value, name, minimum=0, maximum=None):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError("Invalid numeric field: " + name)
    if value < minimum or maximum is not None and value > maximum:
        raise ValueError("Numeric field outside bounds: " + name)
    return value


def policy_config(policy=None):
    supplied = policy or {}
    if set(supplied) - set(DEFAULT_POLICY):
        raise ValueError("Unknown evaluation policy field")
    result = {**DEFAULT_POLICY, **supplied}
    for name in ("min_cases", "min_groups", "min_per_slice", "min_groups_per_slice", "bootstrap_samples"):
        if not isinstance(result[name], int) or isinstance(result[name], bool) or result[name] < 1:
            raise ValueError("Evaluation counts must be positive integers")
    if not 100 <= result["bootstrap_samples"] <= 10000:
        raise ValueError("Bootstrap count must be between 100 and 10000")
    for name in ("required_slices", "protected_slices", "target_slices"):
        if not isinstance(result[name], list) or not result[name] or set(result[name]) - SLICES:
            raise ValueError("Invalid evaluation slice list")
    _number(result["regression_margin"], "regression_margin", maximum=0.1)
    _number(result["minimum_gain"], "minimum_gain", maximum=1)
    for name in ("maximum_token_ratio", "maximum_latency_ratio"):
        _number(result[name], name, minimum=0.1, maximum=10)
    if not isinstance(result["judges"], dict):
        raise ValueError("Judge calibration configuration must be an object")
    return result


def prediction_hash(prediction):
    """Bind semantic output and completion state, including all model-proposed actions."""
    return digest({k: v for k, v in prediction.items() if k not in {"latency_ms", "tokens"}})


def case_hash(case):
    return digest(case)


def _lineage(value):
    keys = {"source:" + s for s in value.get("source_ids", [])}
    for name in ("group", "group_id", "session_id", "goal_id", "task_id"):
        if value.get(name):
            keys.add(name + ":" + str(value[name]))
    return keys


def check_suite(cases, training_episodes=None):
    if not cases or len({c["id"] for c in cases}) != len(cases):
        raise ValueError("Evaluation cases must have unique IDs")
    training_keys = set()
    training_prompts = set()
    for episode in training_episodes or []:
        training_keys.update(_lineage(episode))
        messages = episode.get("messages", [])
        if messages:
            training_prompts.add(digest([{k: m.get(k) for k in ("role", "content")} for m in messages[:-1]]))
    for case in cases:
        if case.get("slice") not in SLICES or not case.get("group") or not case.get("source_ids") or not case.get("task_id"):
            raise ValueError("Evaluation cases need slice, group, task, and source metadata")
        if _lineage(case) & training_keys:
            raise ValueError("Evaluation lineage overlaps training data")
        prompt = [{k: m.get(k) for k in ("role", "content")} for m in case.get("messages", [])]
        if digest(prompt) in training_prompts:
            raise ValueError("Evaluation prompt overlaps training data")
        expect = case.get("expect", {})
        if not any(expect.get(k) for k in ("exact", "all", "any", "effects")) and not case.get("rubric_sha256"):
            raise ValueError("Case needs a positive assertion or independent quality rubric")
        if case["slice"] in {"tools", "memory", "initiative"} and not expect.get("effects"):
            raise ValueError("Action cases require verifiable effect expectations")
        if expect.get("effects") and not case.get("environment_sha256"):
            raise ValueError("Action cases need a frozen environment hash")


def _subset(expected, actual):
    if isinstance(expected, dict):
        return isinstance(actual, dict) and all(k in actual and _subset(v, actual[k]) for k, v in expected.items())
    if isinstance(expected, list):
        return isinstance(actual, list) and expected == actual
    return type(expected) is type(actual) and expected == actual


def _text_score(case, prediction):
    text = prediction["text"]
    expect = case.get("expect", {})
    checks = [prediction["complete"]]
    for key in ("exact", "all", "any", "none"):
        values = expect.get(key)
        if values:
            if not isinstance(values, list) or any(not isinstance(v, str) for v in values):
                raise ValueError("Text assertions must be string lists")
            checks.append(text.strip() in values if key == "exact" else all(v in text for v in values) if key == "all"
                          else any(v in text for v in values) if key == "any" else not any(v in text for v in values))
    if expect.get("max_chars"):
        checks.append(len(text) <= expect["max_chars"])
    if expect.get("chinese"):
        chinese = len(re.findall(r"[\u4e00-\u9fff]", text))
        letters = len(re.findall(r"[A-Za-z\u4e00-\u9fff]", text))
        checks.append(chinese / max(1, letters) >= 0.5)
    if expect.get("reasoning_max_chars"):
        thought = prediction.get("reasoning_summary", "")
        chinese = len(re.findall(r"[\u4e00-\u9fff]", thought))
        letters = len(re.findall(r"[A-Za-z\u4e00-\u9fff]", thought))
        checks.append(bool(thought) and len(thought) <= expect["reasoning_max_chars"] and chinese / max(1, letters) >= 0.5)
    return float(all(checks))


def _effect_score(case, prediction, evidence, policy):
    effects = case.get("expect", {}).get("effects", [])
    reasons = []
    used = set()
    for expected in effects:
        if expected.get("kind") not in {"tool", "memory", "goal"} or not expected.get("name") or "output" not in expected:
            raise ValueError("Effect assertion needs kind, name, and an expected output")
        if expected["kind"] == "tool" and not isinstance(expected.get("arguments"), dict):
            raise ValueError("Tool effect expectations need the actual argument object")
        matches = [(i, receipt) for i, receipt in enumerate(evidence)
                   if i not in used and receipt.get("case_id") == case["id"]
                   and receipt.get("actor_version") == prediction["actor_version"]
                   and receipt.get("kind") == expected["kind"] and receipt.get("name") == expected["name"]
                   and receipt.get("prediction_sha256") == prediction_hash(prediction)
                   and receipt.get("environment_sha256") == case.get("environment_sha256")
                   and receipt.get("source") in policy["trusted_evidence_sources"]]
        if not matches:
            reasons.append("missing_effect_evidence:" + expected["name"])
            continue
        index, receipt = matches[0]
        used.add(index)
        if expected["kind"] == "tool":
            calls = [call for call in prediction.get("tool_calls", []) if call.get("id") == receipt.get("call_id")]
            valid_call = len(calls) == 1 and calls[0].get("function", {}).get("name") == expected["name"]
            arguments = calls[0].get("function", {}).get("arguments") if valid_call else None
            if isinstance(arguments, str):
                try:
                    arguments = json.loads(arguments, parse_constant=lambda _: None)
                except ValueError:
                    arguments = None
            if (not isinstance(arguments, dict) or arguments != expected["arguments"]
                    or receipt.get("arguments_sha256") != digest(arguments)):
                reasons.append("tool_arguments_or_call_mismatch:" + expected["name"])
        if not _subset(expected["output"], receipt.get("output")):
            reasons.append("effect_mismatch:" + expected["name"])
    return float(not reasons), reasons


def _judge_score(case, prediction, judgments, policy):
    required = case["slice"] in {"persona", "reasoning"} or bool(case.get("rubric_sha256"))
    if not required:
        return 1.0, []
    bound = [r for r in judgments if r.get("case_id") == case["id"] and r.get("case_sha256") == case_hash(case)
             and r.get("prediction_sha256") == prediction_hash(prediction)]
    if len(bound) != 1:
        return 0.0, ["missing_or_duplicate_independent_judgment"]
    review = bound[0]
    calibration = policy["judges"].get(review.get("reviewer"), {})
    checks = bool(calibration) and all(calibration.get(k) and calibration.get(k) == review.get(k)
                                      for k in ("judge_version", "calibration_sha256", "rubric_sha256"))
    checks = checks and review.get("independent") is True and review.get("judge_version") != prediction["actor_version"]
    checks = checks and review.get("rubric_sha256") == case.get("rubric_sha256")
    if not checks:
        return 0.0, ["uncalibrated_or_nonindependent_judgment"]
    return _number(review.get("score"), "judge score", maximum=1), []


def _interval(records, samples):
    groups = collections.defaultdict(list)
    for row in records:
        groups[row["group"]].append(row["gain"])
    values = list(groups.values())
    rng = random.Random(42)
    resamples = []
    for _ in range(samples):
        selected = [score for group in rng.choices(values, k=len(values)) for score in group]
        resamples.append(sum(selected) / len(selected))
    resamples.sort()
    return {"n": len(records), "groups": len(groups), "mean_gain": sum(r["gain"] for r in records) / len(records),
            "gain_lower_95": resamples[int(samples * 0.025)], "gain_upper_95": resamples[min(samples - 1, int(samples * 0.975))],
            "baseline_score": sum(r["baseline_score"] for r in records) / len(records),
            "candidate_score": sum(r["candidate_score"] for r in records) / len(records)}


def evaluate(cases, baseline_predictions, candidate_predictions, judgments=None, policy=None,
             training_episodes=None, evidence=None, demo=False):
    """Compare complete predictions with separately supplied runtime and judge evidence."""
    policy = policy_config(policy)
    check_suite(cases, training_episodes)
    evidence, judgments = evidence or [], judgments or []
    expected = {case["id"] for case in cases}
    predictions = {}
    missing = []
    versions = {}
    for side, rows in (("baseline", baseline_predictions), ("candidate", candidate_predictions)):
        index = {row["id"]: row for row in rows}
        if len(index) != len(rows) or set(index) - expected:
            raise ValueError("Duplicate or unexpected prediction ID")
        missing.extend({"id": case_id, "side": side} for case_id in sorted(expected - set(index)))
        side_versions = {row.get("actor_version") for row in rows}
        if len(side_versions) != 1 or not next(iter(side_versions), None):
            raise ValueError("Each prediction set needs exactly one actor version")
        versions[side] = next(iter(side_versions))
        predictions[side] = index
    scored, findings = [], []
    metrics = {s: {"tokens": [], "latency_ms": []} for s in predictions}
    for case in cases:
        if any(case["id"] not in rows for rows in predictions.values()):
            continue
        scores = {}
        for side, index in predictions.items():
            prediction = index[case["id"]]
            if not isinstance(prediction.get("text"), str) or not isinstance(prediction.get("complete"), bool):
                raise ValueError("Prediction needs text and an explicit completion state")
            for name in ("tokens", "latency_ms"):
                metrics[side][name].append(_number(prediction.get(name), name))
            effect, effect_reasons = _effect_score(case, prediction, evidence, policy)
            quality, quality_reasons = _judge_score(case, prediction, judgments, policy)
            reasons = effect_reasons + quality_reasons
            findings.extend({"id": case["id"], "side": side, "reason": reason} for reason in reasons)
            scores[side] = _text_score(case, prediction) * effect * quality
        scored.append({"id": case["id"], "group": case["group"], "slice": case["slice"],
                       "baseline_score": scores["baseline"], "candidate_score": scores["candidate"],
                       "gain": scores["candidate"] - scores["baseline"]})
    if not scored:
        raise ValueError("No paired predictions are available")
    slices = {s: _interval([r for r in scored if r["slice"] == s], policy["bootstrap_samples"])
              for s in sorted({r["slice"] for r in scored})}
    targets = [r for r in scored if r["slice"] in policy["target_slices"]]
    return {"schema_version": 1, "production": not demo, "demo": demo, "baseline_version": versions["baseline"],
            "candidate_version": versions["candidate"], "suite_sha256": digest(cases),
            "baseline_predictions_sha256": digest(baseline_predictions), "candidate_predictions_sha256": digest(candidate_predictions),
            "evidence_sha256": digest(evidence), "judgments_sha256": digest(judgments), "policy": policy,
            "n": len(scored), "expected_cases": len(cases), "coverage": len(scored) / len(cases),
            "missing_cases": missing, "findings": findings, "overall": _interval(scored, policy["bootstrap_samples"]),
            "target": _interval(targets, policy["bootstrap_samples"]) if targets else None, "slices": slices,
            "metrics": {side: {k: sum(v) / len(v) for k, v in values.items()} for side, values in metrics.items()},
            "method": "paired episode-group bootstrap, percentile interval", "case_scores": scored}


def gate(report, policy=None):
    """Require measured target gain and protected-slice noninferiority."""
    policy = policy_config(policy or report.get("policy"))
    if digest(policy) != digest(policy_config(report.get("policy"))):
        raise ValueError("Evaluation policy changed after candidate measurement")
    reasons = []
    if report.get("production") is not True or report.get("demo") is not False:
        reasons.append("demonstration_results_cannot_promote")
    if report.get("baseline_version") == report.get("candidate_version"):
        reasons.append("candidate_version_not_changed")
    if report.get("coverage") != 1 or report.get("missing_cases"):
        reasons.append("incomplete_evaluation_coverage")
    if report["n"] < policy["min_cases"] or report["overall"]["groups"] < policy["min_groups"]:
        reasons.append("insufficient_independent_cases")
    for name in policy["required_slices"]:
        item = report["slices"].get(name)
        if not item or item["n"] < policy["min_per_slice"] or item["groups"] < policy["min_groups_per_slice"]:
            reasons.append("insufficient_slice:" + name)
    for name in policy["protected_slices"]:
        item = report["slices"].get(name)
        if not item or item["gain_lower_95"] < -policy["regression_margin"]:
            reasons.append("noninferiority_not_established:" + name)
    target = report.get("target")
    if not target or target["gain_lower_95"] <= policy["minimum_gain"]:
        reasons.append("target_gain_not_established")
    for finding in report.get("findings", []):
        if finding["reason"].startswith(("missing_", "uncalibrated_")):
            reasons.append("missing_trusted_evidence")
            break
    for metric, limit in (("tokens", "maximum_token_ratio"), ("latency_ms", "maximum_latency_ratio")):
        before, after = report["metrics"]["baseline"][metric], report["metrics"]["candidate"][metric]
        if after > max(before, 1e-9) * policy[limit]:
            reasons.append("cost_regression:" + metric)
    return {"promote": not reasons, "reasons": reasons}


def gaps_from_report(cases, report):
    """Extract recurring diagnostic inputs from a bound evaluation report."""
    if digest(cases) != report.get("suite_sha256"):
        raise ValueError("Gap diagnosis needs the measured evaluation suite")
    by_id = {case["id"]: case for case in cases}
    rows = []
    missing = {item["id"] for item in report.get("missing_cases", []) if item["side"] == "baseline"}
    for item in report.get("case_scores", []):
        if item["baseline_score"] >= 1:
            continue
        case = by_id[item["id"]]
        diagnostics = case.get("diagnostic", {})
        baseline_findings = [f["reason"] for f in report.get("findings", []) if f["side"] == "baseline" and f["id"] == item["id"]]
        cause = "capture" if any(r.startswith(("missing_", "uncalibrated_")) for r in baseline_findings) else diagnostics.get("cause", "unknown")
        rows.append({"episode_id": item["id"], "group": case["group"], "evidence_sha256": digest([case_hash(case), report]),
                     "cause": cause, "category": diagnostics.get("category", case["slice"]),
                     "context_sufficient": diagnostics.get("context_sufficient") is True and not baseline_findings})
    for case_id in sorted(missing):
        case = by_id[case_id]
        rows.append({"episode_id": case_id, "group": case["group"], "evidence_sha256": digest([case_hash(case), report]),
                     "cause": "capture", "category": case["slice"], "context_sufficient": False})
    return rows


def diagnose(gaps, usable_samples=0, resources_ready=False, cooldown_ready=True, budget_ready=True, minimum_episodes=3):
    """Route faults to their owner before considering a weight update."""
    groups = collections.defaultdict(set)
    reasons = collections.defaultdict(set)
    mapping = {"capture": "repair_capture", "tool": "repair_tool", "memory": "update_memory", "skill": "update_skill"}
    for gap in gaps:
        if not gap.get("episode_id") or not gap.get("group") or not gap.get("evidence_sha256"):
            continue
        cause = gap.get("cause")
        if cause in mapping:
            reasons[mapping[cause]].add(gap["episode_id"])
        elif cause in {"parameter", "stable_preference"} and gap.get("context_sufficient") is True:
            groups[gap.get("category", cause)].add(gap["group"])
    for action in ("repair_capture", "repair_tool", "update_memory", "update_skill"):
        if reasons[action]:
            return {"action": action, "episode_ids": sorted(reasons[action]), "reason": "Fix the observed prerequisite first"}
    recurrent = {k: len(v) for k, v in groups.items() if len(v) >= minimum_episodes}
    if not recurrent:
        return {"action": "defer", "reason": "No recurring, evidence-backed parameter gap"}
    if usable_samples < minimum_episodes or not all((resources_ready, cooldown_ready, budget_ready)):
        return {"action": "defer", "reason": "Data, resources, cooldown, or budget is not ready", "recurring": recurrent}
    return {"action": "train", "reason": "Recurring gaps remain after prerequisites are available", "recurring": recurrent}
