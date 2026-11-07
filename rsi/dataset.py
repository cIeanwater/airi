"""Content-bound verification, causal loss masks, and group-isolated datasets."""

import copy
import hashlib
import json
import math
import re
from pathlib import Path

from .common import digest, write_json, write_jsonl
from .episodes import _arguments, episode_hash, validate_episode


SECRET = re.compile(r"sk-[A-Za-z0-9_-]{16,}|Bearer\s+\S+|-----BEGIN .*PRIVATE KEY-----", re.I)


def verify_episode(episode, evidence):
    validate_episode(episode)
    reasons = []
    if not isinstance(evidence, dict) or evidence.get("episode_sha256") != episode["content_sha256"]:
        reasons.append("verification_missing_or_stale")
    elif evidence.get("independent") is not True or not isinstance(evidence.get("reviewer"), str) or not evidence["reviewer"].strip():
        reasons.append("independent_verifier_required")
    blocked = set(episode["context"].get("issues", []))
    resolved = set(evidence.get("resolved_issues", [])) if isinstance(evidence, dict) else set()
    if blocked - resolved:
        reasons.append("unresolved_capture_issues")
    if SECRET.search(json.dumps(episode, ensure_ascii=False)):
        reasons.append("secret")
    indices = []
    checks = evidence.get("checks", []) if isinstance(evidence, dict) else []
    if not isinstance(checks, list) or not checks:
        reasons.append("independent_checks_required")
        checks = []
    messages = episode["messages"]
    by_id = {m["id"]: (i, m) for i, m in enumerate(messages)}
    for check in checks:
        if not isinstance(check, dict) or check.get("message_id") not in by_id:
            reasons.append("check_message_missing")
            continue
        index, message = by_id[check["message_id"]]
        if message["role"] != "assistant" or message["status"] != "finished":
            reasons.append("unfinished_or_external_target")
            continue
        kind, expected = check.get("kind"), check.get("expected")
        passed = False
        if message.get("reasoning_content") and check.get("reasoning_expected") != message["reasoning_content"]:
            reasons.append("reasoning_unverified")
        if kind == "exact":
            passed = isinstance(expected, str) and bool(expected.strip()) and message["content"] == expected
        elif kind == "contains":
            passed = isinstance(expected, list) and bool(expected) and all(isinstance(x, str) and x.strip() and x in message["content"] for x in expected)
        elif kind == "json":
            try:
                parsed = json.loads(message["content"], parse_constant=lambda _: (_ for _ in ()).throw(ValueError("Nonfinite JSON")))
                passed = expected is not None and parsed == expected
            except (ValueError, TypeError):
                passed = False
        elif kind == "tool_result" and isinstance(expected, (dict, list)):
            calls = {call["id"]: call for call in message.get("tool_calls", [])}
            assertions = expected if isinstance(expected, list) else [expected]
            passed = bool(calls) and len(assertions) == len(calls) and {a.get("call_id") for a in assertions if isinstance(a, dict)} == set(calls)
            for assertion in assertions:
                if not isinstance(assertion, dict):
                    passed = False
                    continue
                call_id = assertion.get("call_id")
                call = calls.get(call_id)
                receipt = next((m for m in messages[index + 1:] if m.get("tool_call_id") == call_id), None)
                passed = bool(passed and call and receipt is not None and receipt["status"] == "finished"
                              and assertion.get("name") == call["function"]["name"]
                              and assertion.get("arguments") == _arguments(call["function"]["arguments"])
                              and assertion.get("status") == "finished" and assertion.get("content_sha256") == digest(receipt["content"]))
        else:
            reasons.append("unsupported_check")
        if not passed:
            reasons.append("independent_check_failed")
        else:
            indices.append(index)
    if len(set(indices)) != len(indices):
        reasons.append("duplicate_target_check")
    # No outgoing call is trained as successful until every call has an observed result.
    for index in indices:
        for call in messages[index].get("tool_calls", []):
            if not any(m.get("tool_call_id") == call["id"] and m["status"] == "finished" for m in messages[index + 1:]):
                reasons.append("tool_result_unverified")
    return {"eligible": bool(indices) and not reasons, "train_indices": sorted(set(indices)) if not reasons else [],
            "reasons": sorted(set(reasons)), "episode_sha256": episode["content_sha256"], "evidence_sha256": digest(evidence)}


def _native(messages, tokenizer):
    allowed = {"role", "content", "reasoning_content", "tool_calls", "tool_call_id", "name"}
    result = [{k: copy.deepcopy(v) for k, v in m.items() if k in allowed} for m in messages]
    if "<|channel>" in str(getattr(tokenizer, "chat_template", "")):
        for message in result:
            for call in message.get("tool_calls", []):
                call["function"]["arguments"] = _arguments(call["function"]["arguments"])
    return result


def compile_messages(messages, tokenizer, max_length, tools=None, train_indices=None, template_kwargs=None):
    """Compile one selected decision with its exact causal prefix and native turn ending."""
    if not isinstance(max_length, int) or max_length < 1:
        raise ValueError("Context limit must be positive")
    indices = train_indices if train_indices is not None else [len(messages) - 1]
    if not isinstance(indices, list) or len(indices) != 1:
        raise ValueError("Compile one selected assistant decision at a time")
    index = indices[0]
    if not isinstance(index, int) or not 0 <= index < len(messages) or messages[index].get("role") != "assistant":
        raise ValueError("Loss target must be an assistant decision")
    if messages[index].get("status", "finished") != "finished":
        raise ValueError("Incomplete assistant output is not a loss target")
    kwargs = copy.deepcopy(template_kwargs or {})
    if any(k in kwargs for k in ("tokenize", "add_generation_prompt", "continue_final_message", "tools", "messages")):
        raise ValueError("Template settings cannot replace messages or compilation controls")
    clean = _native(messages[:index + 1], tokenizer)
    prefix = tokenizer.apply_chat_template(clean[:-1], tools=tools or None, tokenize=False, add_generation_prompt=True, **kwargs)
    complete = tokenizer.apply_chat_template(clean, tools=tools or None, tokenize=False, add_generation_prompt=False, **kwargs)
    if not complete.startswith(prefix) and len(clean) > 1 and clean[-2]["role"] == "assistant":
        # Consecutive self-authored phases can continue a native model turn without a fabricated user message.
        prefix = tokenizer.apply_chat_template(clean[:-1], tools=tools or None, tokenize=False,
                                               add_generation_prompt=False, continue_final_message=True, **kwargs)
    if not isinstance(prefix, str) or not isinstance(complete, str) or not complete.startswith(prefix):
        raise ValueError("Native template changed the causal response boundary")
    prompt_ids = tokenizer.encode(prefix, add_special_tokens=False)
    ids = tokenizer.encode(complete, add_special_tokens=False)
    if ids[:len(prompt_ids)] != prompt_ids or len(ids) <= len(prompt_ids):
        raise ValueError("Tokenizer cannot preserve the assistant loss boundary")
    if len(ids) > max_length:
        raise ValueError("Causal episode exceeds context. Do not truncate tool or answer dependencies")
    return {"input_ids": ids, "attention_mask": [1] * len(ids), "labels": [-100] * len(prompt_ids) + ids[len(prompt_ids):]}


def compile_episode(episode, tokenizer, max_length, train_indices=None):
    validate_episode(episode)
    indices = train_indices if train_indices is not None else episode.get("train_indices")
    if not isinstance(indices, list) or not indices or len(indices) != len(set(indices)):
        raise ValueError("Verified assistant train_indices are required")
    for index in indices:
        if not isinstance(index, int) or not 0 <= index < len(episode["messages"]):
            raise ValueError("Loss target index is invalid")
        for call in episode["messages"][index].get("tool_calls", []):
            if not any(m.get("tool_call_id") == call["id"] and m["status"] == "finished" for m in episode["messages"][index + 1:]):
                raise ValueError("A tool decision needs completed receipt evidence")
    return [compile_messages(episode["messages"], tokenizer, max_length, episode["tools"], [i], episode["template_kwargs"]) for i in indices]


def split_episodes(episodes, min_groups=10):
    """Connect identity sessions, goals, source lineage, and duplicate inputs before temporal splitting."""
    roots = list(range(len(episodes)))

    def find(index):
        while roots[index] != index:
            roots[index] = roots[roots[index]]
            index = roots[index]
        return index

    seen = {}
    for index, episode in enumerate(episodes):
        scope = digest(episode["scope"])
        keys = ["session:" + digest([scope, episode["session_id"]])]
        keys.extend("source:" + source for source in episode["source_ids"])
        if episode.get("goal_id"):
            keys.append("goal:" + digest([scope, episode["goal_id"]]))
        # Shared person facts need an explicit evidence source ID. A person's entire history is not one task.
        if episode["context"].get("fact_ids"):
            keys.extend("fact:" + digest([scope, episode.get("person_id"), fact]) for fact in episode["context"]["fact_ids"])
        prompt = [{k: v for k, v in m.items() if k not in {"id", "status", "available_at"}} for m in episode["messages"][:-1]]
        keys.append("prompt:" + digest([prompt, episode["tools"]]))
        for key in keys:
            if key in seen:
                roots[find(index)] = find(seen[key])
            seen[key] = index
    groups = {}
    for index, episode in enumerate(episodes):
        groups.setdefault(find(index), []).append(episode)
    ordered = sorted(groups.values(), key=lambda group: (max(e["occurred_at"] for e in group), min(e["id"] for e in group)))
    if len(ordered) < min_groups:
        raise ValueError("Too few independent episode groups for held-out evaluation")
    result = {"train": [], "validation": [], "test": []}
    for index, group in enumerate(ordered):
        split = "test" if index >= math.floor(len(ordered) * 0.9) else "validation" if index >= math.floor(len(ordered) * 0.8) else "train"
        result[split].extend(group)
    if any(not rows for rows in result.values()):
        raise ValueError("All dataset splits need independent groups")
    return result


def _category(episode):
    return episode["context"].get("dataset_kind", episode["kind"])


def _mix(episodes, weights, budget):
    categories = {"base", "dialogue", "reflection", "initiative"}
    if set(weights) != categories or any(isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) or v < 0 for v in weights.values()):
        raise ValueError("Mix needs four finite nonnegative category weights")
    if not math.isclose(sum(weights.values()), 1) or weights["base"] < 0.3:
        raise ValueError("Mix must sum to one and retain at least 30% base replay")
    if not isinstance(budget, int) or budget < 1:
        raise ValueError("Character budget must be positive")
    selected, usage = [], {}
    for category, weight in weights.items():
        quota, used = int(budget * weight), 0
        for episode in sorted((e for e in episodes if _category(e) == category), key=lambda e: e["id"]):
            cost = len(json.dumps([episode["messages"], episode["tools"]], ensure_ascii=False))
            if used + cost <= quota:
                selected.append(episode)
                used += cost
        if weight and (not used or used < quota * 0.8):
            raise ValueError("Insufficient unique data for mix quota: " + category)
        usage[category] = {"characters": used, "quota": quota}
    return selected, usage


def build_dataset(episodes, output, verifications=None, mix=None, character_budget=None, min_groups=10):
    output = Path(output)
    if output.exists():
        raise ValueError("Dataset output must be a new directory")
    if mix is None:
        raise ValueError("An explicit replay mix is required")
    accepted, rejected, ids = [], {}, set()
    proofs = verifications or {}
    for episode in episodes:
        validate_episode(episode)
        if episode["id"] in ids:
            raise ValueError("Duplicate episode ID")
        ids.add(episode["id"])
        result = verify_episode(episode, proofs.get(episode["id"]))
        if not result["eligible"]:
            rejected[episode["id"]] = result["reasons"]
            continue
        accepted.append({**copy.deepcopy(episode), "train_indices": result["train_indices"], "verification_sha256": result["evidence_sha256"]})
    splits = split_episodes(accepted, min_groups)
    for split, rows in splits.items():
        unique = {}
        for row in rows:
            messages = [{k: v for k, v in m.items() if k not in {"id", "available_at"}} for m in row["messages"]]
            content = digest([messages, row["tools"], row["train_indices"]])
            unique.setdefault(content, row)
        splits[split] = list(unique.values())
    splits["train"], usage = _mix(splits["train"], mix, character_budget)
    for split, rows in splits.items():
        write_jsonl(output / (split + ".jsonl"), rows)
    manifest = {"schema_version": 1, "kind": "behavior", "mix": mix, "mix_usage": usage, "rejected": rejected,
                "records": [{"id": e["id"], "split": split, "content_sha256": e["content_sha256"], "verification_sha256": e["verification_sha256"]} for split, rows in splits.items() for e in rows],
                "files": {split: hashlib.sha256((output / (split + ".jsonl")).read_bytes()).hexdigest() for split in splits}}
    write_json(output / "manifest.json", manifest)
    return manifest
