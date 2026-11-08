"""Reproducible private corpus extraction and content-bound semantic review."""

import collections
import copy
import datetime
import html
import json
import math
import re
from pathlib import Path

from .common import digest, file_hash, read_json, read_jsonl, utc_now, write_json, write_jsonl
from .episodes import validate_tools


KINDS = {"dialogue", "reflection", "initiative"}
SECRET = re.compile(r"sk-[A-Za-z0-9_-]{16,}|Bearer\s+[A-Za-z0-9._-]{16,}|-----BEGIN .*PRIVATE KEY-----", re.I)
URL_CREDENTIAL = re.compile(r"[?&](?:rkey|access_token|refresh_token|api_key|x-amz-signature)=[^\s&#\"<>]{8,}", re.I)
TEST = re.compile(r"(?:synthetic[-_ ](?:test|fixture)|合成测试|仅用于测试|测试专用|独立的部署诊断|独立的合成验收|本地 JSON 记录验收用的合成消息|fixture[-_](?:user|assistant))", re.I)


def content_hash(row):
    return digest({k: v for k, v in row.items() if k != "content_sha256"})


def _time(value):
    if isinstance(value, (int, float)) and math.isfinite(value):
        return int(value)
    if isinstance(value, str):
        try:
            return int(datetime.datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp() * 1000)
        except ValueError:
            return 0
    return 0


def _arguments(value):
    value = json.loads(value) if isinstance(value, str) else value
    if not isinstance(value, dict):
        raise ValueError("tool_arguments_not_object")
    digest(value)
    return value


def _messages(values, tools):
    if not isinstance(values, list) or not values:
        raise ValueError("missing_exact_messages")
    allowed_names = validate_tools(tools)
    result, pending, call_ids = [], {}, set()
    for original in values:
        if not isinstance(original, dict) or original.get("role") not in {"system", "user", "assistant", "tool"}:
            raise ValueError("unsupported_message_role")
        role = original["role"]
        text = original.get("content")
        if text is None and role == "assistant":
            text = ""
        if not isinstance(text, str):
            raise ValueError("multimodal_input_requires_separate_pipeline")
        message = {"role": role, "content": text}
        if original.get("reasoning_content"):
            if role != "assistant" or not isinstance(original["reasoning_content"], str):
                raise ValueError("invalid_reasoning_channel")
            message["reasoning_content"] = original["reasoning_content"]
        if role == "tool":
            call_id = original.get("tool_call_id")
            if call_id not in pending:
                raise ValueError("orphan_tool_result")
            if original.get("name", pending[call_id]) != pending[call_id]:
                raise ValueError("tool_result_name_mismatch")
            message["tool_call_id"] = call_id
            if original.get("name"):
                message["name"] = original["name"]
            del pending[call_id]
        else:
            if pending:
                raise ValueError("missing_tool_result")
            if original.get("tool_calls"):
                if role != "assistant":
                    raise ValueError("nonassistant_tool_call")
                message["tool_calls"] = copy.deepcopy(original["tool_calls"])
                for call in message["tool_calls"]:
                    if call.get("type") != "function" or call.get("id") in call_ids or not call.get("id"):
                        raise ValueError("invalid_tool_call")
                    function = call.get("function", {})
                    if function.get("name") not in allowed_names:
                        raise ValueError("undefined_tool_call")
                    _arguments(function.get("arguments"))
                    pending[call["id"]] = function["name"]
                    call_ids.add(call["id"])
        result.append(message)
    if pending:
        raise ValueError("pending_final_tool_call")
    return result


def _json_text(text):
    source = re.sub(r"^```(?:json)?\s*", "", text.strip(), flags=re.I)
    source = re.sub(r"\s*```$", "", source)
    return json.loads(source)


def _task(messages):
    system = "\n".join(m["content"] for m in messages if m["role"] == "system")
    try:
        output = _json_text(messages[-1]["content"])
    except ValueError:
        output = None
    if isinstance(output, dict) and {"thought", "speak", "text"} <= output.keys():
        return "initiative"
    if "【即时思考】" in system:
        return "initiative"
    if isinstance(output, dict) and "documents" in output:
        return "reflection"
    if any(term in system for term in ("整理记忆", "认知修订", "提取稳定", "认知整理", "记忆整理", "文档修订", "长期记忆", "NO_UPDATE")):
        return "reflection"
    return "dialogue"


def _hard_checks(messages, kind, policy):
    spoken = messages[-1]["content"]
    if kind == "initiative":
        try:
            output = _json_text(spoken)
        except ValueError:
            raise ValueError("malformed_initiative_json") from None
        if not isinstance(output, dict) or not {"thought", "speak", "text"} <= output.keys() or type(output["speak"]) is not bool or not isinstance(output["text"], str) or not isinstance(output["thought"], str):
            raise ValueError("invalid_initiative_schema")
        for message in messages[:-1]:
            if message["role"] != "user":
                continue
            try:
                context = json.loads(message["content"])
            except ValueError:
                continue
            if isinstance(context, dict) and isinstance(context.get("engagement"), dict):
                if context["engagement"].get("canSpeak") is False and output["speak"]:
                    raise ValueError("forbidden_initiative")
        if not output["speak"] and output["text"].strip():
            raise ValueError("silent_initiative_has_spoken_text")
        spoken = output["text"]
    if kind in {"initiative", "dialogue"}:
        for name in policy.get("person_names", []):
            if name != policy.get("character_name") and re.search(r"我(?:是|叫|的名字是|的名字叫)\s*[“「\"']?" + re.escape(name), spoken):
                raise ValueError("spoken_identity_inversion")


def gather(snapshot):
    root = Path(snapshot)
    result = []
    for category in ("events", "companion", "gateway"):
        for path in sorted((root / category).rglob("*.json")):
            result.append({"path": str(path), "relative_path": path.relative_to(root).as_posix(), "category": category,
                           "sha256": file_hash(path), "bytes": path.stat().st_size})
    return result


def _event_index(inventory):
    events, message_ids = {}, collections.defaultdict(list)
    for source in inventory:
        if source["category"] != "events":
            continue
        value = read_json(source["path"])
        if not isinstance(value, dict) or not {"id", "scope", "kind", "text", "status"} <= value.keys():
            continue
        events[value["id"]] = value
        if isinstance(value.get("messageId"), str) and value["messageId"]:
            message_ids[value["messageId"]].append(value)
        nested = (value.get("raw") or {}).get("message", {}) if isinstance(value.get("raw"), dict) else {}
        if isinstance(nested, dict) and nested.get("id"):
            message_ids[nested["id"]].append(value)
    return events, message_ids


def _context_sources(messages, events):
    selected = set()
    def walk(value):
        if isinstance(value, dict):
            if isinstance(value.get("id"), str) and value["id"] in events:
                selected.add(value["id"])
            for key in ("sources", "sourceIds", "source_ids", "eventIds", "parents"):
                if isinstance(value.get(key), list):
                    selected.update(x for x in value[key] if isinstance(x, str) and x in events)
            for child in value.values():
                if isinstance(child, (dict, list)):
                    walk(child)
        elif isinstance(value, list):
            for child in value:
                walk(child)
    for message in messages:
        if message["role"] in {"system", "user", "tool"}:
            try:
                walk(json.loads(message["content"]))
            except (ValueError, RecursionError):
                pass
    return selected


def _candidate(record, source, scope, events, message_ids, policy):
    if not isinstance(record, dict):
        raise ValueError("request_record_not_object")
    gateway = "model_input" in record
    request = record.get("model_input") if gateway else record.get("request")
    if not isinstance(request, dict):
        raise ValueError("missing_effective_model_input")
    response = record.get("response") or {}
    choice = (response.get("choices") or [{}])[0] if isinstance(response, dict) else {}
    successful = record.get("status") == "ok" and record.get("http_status") == 200 if gateway else record.get("status") == 200
    if not successful or choice.get("finish_reason") != "stop":
        raise ValueError("incomplete_or_failed_response")
    output = choice.get("message")
    if not isinstance(output, dict) or output.get("role", "assistant") != "assistant":
        raise ValueError("missing_assistant_output")
    tools = request.get("tools") or []
    messages = _messages([*(request.get("messages") or []), {**output, "role": "assistant"}], tools)
    target = messages[-1]
    if not target["content"].strip():
        raise ValueError("empty_final_answer")
    serialized = json.dumps([messages, tools], ensure_ascii=False)
    if SECRET.search(serialized):
        raise ValueError("secret_in_source")
    if TEST.search(serialized):
        raise ValueError("test_fixture_source")
    correlation = record.get("correlation") or {}
    annotations = record.get("rsi") or {}
    session = annotations.get("session_id") or correlation.get("session_id")
    linked = message_ids.get(correlation.get("message_id"), [])
    linked_sessions = {e.get("sessionId") for e in linked if e.get("sessionId")}
    if not session and len(linked_sessions) == 1:
        session = next(iter(linked_sessions))
    explicit_sources = _context_sources(messages, events) | {e["id"] for e in linked}
    issues = ["semantic_quality_review"]
    if not session:
        issues.append("session_unlinked")
    if any(m["role"] == "user" for m in messages):
        issues.append("input_origin_review")
    if request.get("response_format") or (record.get("request") or {}).get("response_format"):
        issues.append("structured_output_constraints_review")
    if request.get("tool_choice") not in (None, "auto"):
        issues.append("forced_tool_choice_review")
    if any(m.get("reasoning_content") for m in messages):
        issues.append("reasoning_compression_review")
    if any(m.get("tool_calls") for m in messages):
        issues.append("tool_trace_review")
    kind = _task(messages)
    _hard_checks(messages, kind, policy)
    at = _time(record.get("startedAt", record.get("started_at")))
    group = digest([scope, "session", session]) if session else digest([scope, "all-unlinked-requests"])
    semantic = digest([messages, tools, request.get("chat_template_kwargs") or {}, scope])
    row = {"schema_version": 1, "id": "clean-" + semantic, "group": group,
           "source_ids": ["request:" + source["sha256"], *sorted("event:" + x for x in explicit_sources)],
           "scope": copy.deepcopy(scope), "kind": kind, "messages": messages, "tools": copy.deepcopy(tools),
           "train_indices": [len(messages) - 1], "template_kwargs": copy.deepcopy(request.get("chat_template_kwargs") or {}),
           "occurred_at": at, "origin_model": {"requested": request.get("model"), "resolved": response.get("model"), "provenance": record.get("provenance")},
           "review_required": sorted(issues), "request_format": "gateway" if gateway else "companion",
           "source_locations": [source["relative_path"]], "session_id": session,
           "decoding_constraints": {"response_format": request.get("response_format"), "tool_choice": request.get("tool_choice")}}
    row["content_sha256"] = content_hash(row)
    return row


def extract(snapshot, output, scope, policy=None):
    root = Path(output)
    if root.exists():
        raise ValueError("Use a new extraction directory")
    if set(scope) != {"user_id", "character_id"} or not all(isinstance(x, str) and x for x in scope.values()):
        raise ValueError("One explicit corpus scope is required")
    inventory = gather(snapshot)
    events, message_ids = _event_index(inventory)
    candidates, rejected, duplicates = {}, [], 0
    for source in inventory:
        if source["category"] == "events":
            continue
        try:
            candidate = _candidate(read_json(source["path"]), source, scope, events, message_ids, policy or {})
        except (ValueError, TypeError, KeyError) as error:
            rejected.append({"source_id": "request:" + source["sha256"], "source_location": source["relative_path"], "reason": str(error)})
            continue
        existing = candidates.get(candidate["id"])
        if existing:
            duplicates += 1
            existing["source_ids"] = sorted(set(existing["source_ids"]) | set(candidate["source_ids"]))
            existing["source_locations"] = sorted(set(existing["source_locations"]) | set(candidate["source_locations"]))
            existing["review_required"] = sorted(set(existing["review_required"]) | set(candidate["review_required"]))
            existing["occurred_at"] = min(existing["occurred_at"], candidate["occurred_at"])
            if existing["group"] != candidate["group"]:
                existing["group"] = digest([scope, "all-unlinked-requests"])
                existing["review_required"] = sorted(set(existing["review_required"]) | {"duplicate_session_conflict"})
            existing["content_sha256"] = content_hash(existing)
        else:
            candidates[candidate["id"]] = candidate
    values = sorted(candidates.values(), key=lambda x: (x["occurred_at"], x["id"]))
    thoughts = [{"candidate_id": row["id"], "candidate_sha256": row["content_sha256"],
                 "reasoning": row["messages"][-1].get("reasoning_content", ""), "answer": row["messages"][-1]["content"]}
                for row in values if row["messages"][-1].get("reasoning_content")]
    report = {"captured_at": utc_now(), "source_files": len(inventory), "event_records": len(events),
              "request_files": sum(s["category"] != "events" for s in inventory), "candidates": len(values),
              "duplicates": duplicates, "rejected": len(rejected), "rejection_reasons": dict(collections.Counter(r["reason"] for r in rejected)),
              "kinds": dict(collections.Counter(r["kind"] for r in values)), "with_target_reasoning": len(thoughts),
              "groups": len({r["group"] for r in values}), "snapshot_sha256": digest([(s["relative_path"], s["sha256"]) for s in inventory])}
    write_jsonl(root / "candidates.jsonl", values)
    write_jsonl(root / "reasoning-sources.jsonl", thoughts)
    write_jsonl(root / "rejected.jsonl", rejected)
    write_json(root / "inventory.json", inventory)
    write_json(root / "report.json", report)
    return report


def _summary_check(raw, summary):
    if not isinstance(summary, str) or not summary.strip():
        return "missing_chinese_summary"
    summary = summary.strip()
    if len(summary) > 320 or len(summary) > len(raw.strip()):
        return "summary_not_shorter_or_over_budget"
    chinese = len(re.findall(r"[\u4e00-\u9fff]", summary))
    letters = len(re.findall(r"[A-Za-z\u4e00-\u9fff]", summary))
    if chinese / max(letters, 1) < 0.5:
        return "summary_not_chinese"
    if SECRET.search(summary):
        return "secret_in_summary"
    return None


def apply_review(candidate, review):
    if candidate.get("content_sha256") != content_hash(candidate):
        raise ValueError("Candidate content hash changed")
    if review.get("candidate_id") != candidate["id"] or review.get("candidate_sha256") != candidate["content_sha256"]:
        raise ValueError("Review is not bound to this candidate content")
    if review.get("decision") not in {"accept", "reject", "quarantine"} or not isinstance(review.get("reviewer"), str) or not review["reviewer"].strip():
        raise ValueError("Review needs a decision and reviewer")
    if not isinstance(review.get("reason_codes", []), list) or not all(isinstance(r, str) for r in review.get("reason_codes", [])):
        raise ValueError("Review reason codes must be strings")
    if review["decision"] != "accept":
        return None, list(review.get("reason_codes", ["semantic_" + review["decision"]]))
    if URL_CREDENTIAL.search(html.unescape(json.dumps(candidate, ensure_ascii=False))):
        return None, ["privacy_credential_in_source"]
    if TEST.search(json.dumps(candidate.get("messages", []), ensure_ascii=False)):
        return None, ["fixture_in_source"]
    checks = review.get("checks", {})
    if not isinstance(checks, dict):
        raise ValueError("Review checks must be an object")
    required = {"answer_faithful", "no_future_leak", "task_appropriate", "privacy_checked"}
    if any(checks.get(key) is not True for key in required):
        return None, ["semantic_checks_incomplete"]
    kind = review.get("kind", candidate["kind"])
    if kind not in KINDS:
        return None, ["invalid_review_kind"]
    if kind == "dialogue" and checks.get("persona_consistent") is not True:
        return None, ["persona_quality_unverified"]
    if "tool_trace_review" in candidate["review_required"] and checks.get("tool_trace_valid") is not True:
        return None, ["tool_trace_unverified"]
    resolved = review.get("resolved_issues", [])
    if not isinstance(resolved, list) or not all(isinstance(issue, str) for issue in resolved):
        raise ValueError("Resolved review issues must be a string array")
    if not set(candidate["review_required"]) <= set(resolved):
        return None, ["unresolved_source_issues"]
    row = copy.deepcopy(candidate)
    row["kind"] = kind
    raw = row["messages"][-1].get("reasoning_content", "")
    summary = review.get("short_reasoning", "")
    if raw:
        reason = _summary_check(raw, summary)
        if reason or checks.get("summary_faithful") is not True:
            return None, [reason or "summary_faithfulness_unverified"]
        row["messages"][-1]["reasoning_content"] = summary.strip()
    elif summary:
        return None, ["summary_without_source_reasoning"]
    edits = []
    if raw:
        edits.append("target_reasoning_replaced_by_reviewed_chinese_summary")
    # Historical thinking remains causal context and is outside train_indices.
    row["review"] = {"reviewer": review["reviewer"], "review_sha256": digest(review), "checks": checks,
                     "original_candidate_sha256": candidate["content_sha256"], "quality_stage": "semantic-reviewed", "edits": sorted(set(edits))}
    row["review_required"] = []
    row["content_sha256"] = content_hash(row)
    return row, []


def _split(rows, min_groups):
    parents = list(range(len(rows)))
    def find(index):
        while parents[index] != index:
            parents[index] = parents[parents[index]]
            index = parents[index]
        return index
    seen = {}
    for index, row in enumerate(rows):
        keys = ["group:" + row["group"], *["source:" + x for x in row["source_ids"]]]
        keys.append("prompt:" + digest([row["messages"][:-1], row["tools"]]))
        for key in keys:
            if key in seen:
                parents[find(index)] = find(seen[key])
            seen[key] = index
    groups = collections.defaultdict(list)
    for index, row in enumerate(rows):
        groups[find(index)].append(row)
    ordered = sorted(groups.values(), key=lambda g: (max(r["occurred_at"] for r in g), min(r["id"] for r in g)))
    result = {"train": [], "validation": [], "test": []}
    if len(ordered) < min_groups:
        result["train"] = rows
        return result, len(ordered)
    for i, group in enumerate(ordered):
        key = "test" if i >= math.floor(len(ordered) * 0.9) else "validation" if i >= math.floor(len(ordered) * 0.8) else "train"
        result[key].extend(group)
    return result, len(ordered)


def finalize(candidates_path, reviews_path, output, min_groups=10):
    root = Path(output)
    if root.exists():
        raise ValueError("Use a new finalized dataset directory")
    if isinstance(min_groups, bool) or not isinstance(min_groups, int) or min_groups < 1:
        raise ValueError("Minimum groups must be a positive integer")
    candidates, reviews = read_jsonl(candidates_path), read_jsonl(reviews_path)
    originals = {c["id"]: c for c in candidates}
    if len({digest(c["scope"]) for c in candidates}) > 1:
        raise ValueError("A finalized dataset must contain one explicit corpus scope")
    by_id = {}
    for review in reviews:
        if review.get("candidate_id") in by_id:
            raise ValueError("Duplicate semantic review")
        by_id[review["candidate_id"]] = review
    candidate_ids = {c["id"] for c in candidates}
    if len(candidate_ids) != len(candidates):
        raise ValueError("Candidate IDs must be unique")
    if set(by_id) - candidate_ids:
        raise ValueError("Review references an unknown candidate")
    accepted, rejected, quarantine, pairs = [], [], [], []
    for candidate in candidates:
        review = by_id.get(candidate["id"])
        if review is None:
            quarantine.append({"candidate_id": candidate["id"], "reason_codes": ["semantic_review_pending"]})
            continue
        row, reasons = apply_review(candidate, review)
        if row is None:
            ledger = rejected if review["decision"] == "reject" else quarantine
            ledger.append({"candidate_id": candidate["id"], "reason_codes": reasons})
            continue
        accepted.append(row)
        raw = candidate["messages"][-1].get("reasoning_content", "")
        if raw:
            short = row["messages"][-1]["reasoning_content"]
            pairs.append({"candidate_id": candidate["id"], "source_sha256": candidate["content_sha256"], "raw_reasoning": raw,
                          "short_reasoning": short, "raw_characters": len(raw), "short_characters": len(short),
                          "ratio": len(short) / max(1, len(raw)), "review_sha256": digest(review)})
    splits, groups = _split(accepted, min_groups)
    ready = bool(accepted) and all(splits.values()) and groups >= min_groups
    reasons = [] if ready else ["insufficient_independent_groups_for_held_out_evaluation"]
    write_jsonl(root / "sft-all.jsonl", accepted)
    for split, values in splits.items():
        write_jsonl(root / (split + ".jsonl"), values)
    write_jsonl(root / "reasoning-pairs.jsonl", pairs)
    write_jsonl(root / "rejected.jsonl", rejected)
    write_jsonl(root / "quarantine.jsonl", quarantine)
    write_jsonl(root / "reviews.jsonl", reviews)
    manifest = {"schema_version": 1, "kind": "cleaned-sft", "scope": accepted[0]["scope"] if accepted else None,
                "readiness": {"ready": ready, "reasons": reasons, "independent_groups": groups, "minimum_groups": min_groups},
                "source_candidates_sha256": file_hash(candidates_path), "source_reviews_sha256": file_hash(reviews_path),
                "files": {split: file_hash(root / (split + ".jsonl")) for split in splits},
                "records": [{"id": row["id"], "split": split, "content_sha256": row["content_sha256"]} for split, values in splits.items() for row in values]}
    report = {"captured_at": utc_now(), "candidates": len(candidates), "reviewed": len(reviews), "accepted": len(accepted),
              "rejected": len(rejected), "quarantined": len(quarantine), "kinds": dict(collections.Counter(r["kind"] for r in accepted)),
              "splits": {k: len(v) for k, v in splits.items()}, "readiness": manifest["readiness"], "compressed_reasoning": len(pairs),
              "reasoning_raw_characters": sum(p["raw_characters"] for p in pairs), "reasoning_short_characters": sum(p["short_characters"] for p in pairs),
              "shortened_reasoning": sum(p["short_characters"] < p["raw_characters"] for p in pairs),
              "already_concise_reasoning": sum(p["short_characters"] == p["raw_characters"] for p in pairs),
              "unreviewed_raw_target_reasoning": 0,
              "raw_target_reasoning_remaining": sum(p["raw_reasoning"] == p["short_reasoning"] for p in pairs),
              "raw_prefix_reasoning_messages": sum(bool(m.get("reasoning_content")) for r in accepted for m in r["messages"][:-1]),
              "raw_prefix_reasoning_characters": sum(len(m.get("reasoning_content", "")) for r in accepted for m in r["messages"][:-1]),
              "final_answer_changed": sum(r["messages"][-1]["content"] != originals[r["id"]]["messages"][-1]["content"] for r in accepted),
              "tool_definitions_or_arguments_changed": sum(r["tools"] != originals[r["id"]]["tools"] or [m.get("tool_calls") for m in r["messages"]] != [m.get("tool_calls") for m in originals[r["id"]]["messages"]] for r in accepted),
              "model_training_executed": False,
              "note": "Semantic review is not an external factual oracle. Training readiness also requires target-tokenizer validation."}
    write_json(root / "manifest.json", manifest)
    write_json(root / "report.json", report)
    return report
