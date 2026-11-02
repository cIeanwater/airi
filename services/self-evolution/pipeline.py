"""Offline archive audit, explicit candidate export, and reviewed SFT assembly."""

import argparse
import collections
import datetime
import hashlib
import json
import math
import re
from pathlib import Path


KINDS = {"base", "dialogue", "persona", "reasoning"}
SECRET = re.compile(r"sk-[A-Za-z0-9_-]{16,}|Bearer\s+\S+|-----BEGIN .*PRIVATE KEY-----", re.I)
PII = re.compile(r"[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}|(?<!\d)1[3-9]\d{9}(?!\d)")


def read(path):
    return json.loads(Path(path).read_text(encoding="utf-8-sig"))


def digest(value):
    data = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(data.encode()).hexdigest()


def write(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")


def lines(path):
    with Path(path).open(encoding="utf-8-sig") as stream:
        for line in stream:
            if line.strip():
                yield json.loads(line)


def write_lines(path, values):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="\n") as stream:
        for value in values:
            stream.write(json.dumps(value, ensure_ascii=False, allow_nan=False) + "\n")


def snapshot(roots):
    """Read each event once. Duplicate IDs retain one original or stop on conflict."""
    events, copies, hashes = {}, 0, []
    for root in roots:
        for path in sorted(Path(root).glob("*/archive/events/*.json")):
            data = path.read_bytes()
            event = json.loads(data)
            required = {"id", "scope", "kind", "actor", "status", "sessionId", "channel", "text", "occurredAt"}
            if not required <= event.keys() or not isinstance(event["text"], str):
                raise ValueError("Invalid archive event")
            key = digest([event["scope"], event["id"]])
            # Model attribution was added after some archives were copied.
            content = {k: v for k, v in event.items() if k != "provenance"}
            if key in events:
                previous = {k: v for k, v in events[key].items() if k != "provenance"}
                if content != previous:
                    raise ValueError("Conflicting event copies. Select one authoritative archive")
                copies += 1
                continue
            events[key] = event
            hashes.append(hashlib.sha256(data).hexdigest())
    return list(events.values()), copies, digest(sorted(hashes))


def reasoning(event):
    raw = event.get("raw") or {}
    message = raw.get("message", raw)
    value = message.get("reasoning_content", "")
    return value if isinstance(value, str) else ""


def audit(roots, requests):
    events, copies, sha = snapshot(roots)
    counts = collections.Counter((e["kind"], e["actor"], e["status"], e["channel"]) for e in events)
    request_counts = collections.Counter()
    for root in requests:
        for path in sorted(Path(root).glob("*.json")):
            value = read(path)
            request_counts["files"] += 1
            try:
                candidate = request_candidate(value)
            except ValueError as error:
                request_counts[str(error)] += 1
            else:
                request_counts["text_candidates"] += 1
                request_counts["candidates_with_tool_context" if candidate.get("tools") else "candidates_without_tools"] += 1
    return {
        "captured_at_utc": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "snapshot_sha256": sha,
        "events": len(events), "duplicate_copies": copies,
        "events_by_kind_actor_status_channel": [{"key": list(k), "count": n} for k, n in sorted(counts.items())],
        "provenance": dict(collections.Counter(e.get("provenance", {}).get("origin", "missing") for e in events)),
        "nonempty_reasoning": sum(bool(reasoning(e)) for e in events),
        "time_range_ms": [min((e["occurredAt"] for e in events), default=0), max((e["occurredAt"] for e in events), default=0)],
        "cognition_heads": sum(len(list(Path(r).glob("*/cognition/**/*.md"))) for r in roots),
        "requests": dict(request_counts),
        "note": "Metadata only. No model calls, candidate export, or training.",
    }


def check_messages(messages):
    if not isinstance(messages, list) or len(messages) < 2:
        raise ValueError("At least one prompt and one answer are required")
    roles = []
    for message in messages:
        if set(message) != {"role", "content"} or message["role"] not in {"system", "user", "assistant"}:
            raise ValueError("Unsupported message protocol")
        if not isinstance(message["content"], str) or not message["content"].strip():
            raise ValueError("Empty or multimodal message")
        roles.append(message["role"])
    start = 1 if roles[0] == "system" else 0
    if roles[start:] != ["user" if i % 2 == 0 else "assistant" for i in range(len(roles) - start)]:
        raise ValueError("Messages must alternate user and assistant")
    if roles[-1] != "assistant":
        raise ValueError("Missing final answer")


def check_tools(tools):
    if not isinstance(tools, list):
        raise ValueError("Tool definitions must be a list")
    names = set()
    for tool in tools:
        if not isinstance(tool, dict):
            raise ValueError("Invalid tool definition")
        function = tool.get("function", {})
        name = function.get("name", "")
        if tool.get("type") != "function" or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", name) or name in names:
            raise ValueError("Invalid or duplicate tool definition")
        parameters = function.get("parameters", {})
        if not isinstance(parameters, dict) or parameters.get("type") != "object":
            raise ValueError("Tool parameters must describe an object")
        names.add(name)


def make_candidate(messages, kind, group, source, at=0, thought="", issues=None):
    check_messages(messages)
    row = {"schema_version": 1, "messages": messages, "kind": kind,
           "group": group, "source_ids": sorted(source), "occurred_at": at,
           "reasoning_source": thought, "short_reasoning": "", "issues": issues or []}
    row["id"] = digest([messages, kind, sorted(source)])
    row["content_sha256"] = digest(row)
    return row


def request_candidate(value, event_index=None):
    gateway = "schema_version" in value and "model_input" in value
    request = value.get("model_input") if gateway else value.get("request")
    request = request or {}
    response = value.get("response") or {}
    choice = (response.get("choices") or [{}])[0]
    output = choice.get("message", {})
    successful = value.get("status") == "ok" and value.get("http_status") == 200 if gateway else value.get("status") == 200
    if not successful or choice.get("finish_reason") != "stop":
        raise ValueError("incomplete_response")
    prompt = request.get("messages", [])
    tools = request.get("tools", [])
    check_tools(tools)
    if request.get("response_format") or (value.get("request") or {}).get("response_format"):
        raise ValueError("structured_output_requires_dedicated_training")
    if (value.get("request") or {}).get("tool_choice") not in (None, "auto"):
        raise ValueError("forced_tool_choice_requires_dedicated_training")
    if any(m.get("tool_calls") or m.get("role") == "tool" for m in [*prompt, output]):
        raise ValueError("tool_protocol")
    if any(not isinstance(m.get("content"), str) for m in [*prompt, output]):
        raise ValueError("multimodal_or_empty")
    messages = [{"role": m["role"], "content": m["content"]} for m in [*prompt, output]]
    check_messages(messages)
    matches = (event_index or {}).get(digest(output.get("content")), [])
    # Unlinked logs share one group. This prevents an invented session boundary from leaking data.
    groups = {event_group(e) for e in matches}
    group = next(iter(groups)) if len(groups) == 1 else "unlinked-request-logs"
    source = ["request:" + digest(value)] + ["event:" + digest([e["scope"], e["id"]]) for e in matches]
    issues = [] if len(groups) == 1 else ["session_link_requires_review"]
    if any(e["kind"] != "message" for e in matches) or not matches:
        issues.append("task_classification_requires_review")
    if tools:
        issues.append("tool_context_requires_review")
    row = make_candidate(messages, "dialogue", group, source,
                         max((e["occurredAt"] for e in matches), default=0),
                         output.get("reasoning_content") or "", issues)
    row["generation"] = {"requested_model": request.get("model"), "resolved_model": response.get("model"),
                         "fingerprint": response.get("system_fingerprint"), "provenance": value.get("provenance"),
                         "chat_template_kwargs": request.get("chat_template_kwargs"), "format": "gateway" if gateway else "companion"}
    row["tools"] = tools
    row["content_sha256"] = content_hash(row)
    return row


def event_group(event):
    return digest([event["scope"], event["channel"], event["sessionId"], event.get("person", {}).get("id")])


def export_candidates(roots, requests, output):
    events, _, _ = snapshot(roots)
    index = collections.defaultdict(list)
    for event in events:
        if event["kind"] in {"message", "reflection"} and event["actor"] == "assistant" and event["status"] == "finished":
            index[digest(event["text"])].append(event)
    rows, rejected = {}, collections.Counter()
    for root in requests:
        for path in sorted(Path(root).glob("*.json")):
            try:
                row = request_candidate(read(path), index)
            except ValueError as error:
                rejected[str(error)] += 1
                continue
            rows[row["id"]] = row
    # Event-only histories and cognition revisions need curated prompts. They never become invented SFT pairs.
    write_lines(output, rows.values())
    return {"candidates": len(rows), "rejected": dict(rejected)}


def content_hash(row):
    return digest({k: v for k, v in row.items() if k != "content_sha256"})


def reviewed(rows, reviews):
    decisions = {}
    for review in reviews:
        if review["id"] in decisions:
            raise ValueError("Duplicate review")
        decisions[review["id"]] = review
    accepted, rejected = [], collections.Counter()
    seen = set()
    for row in rows:
        if row["id"] in seen:
            raise ValueError("Duplicate candidate ID")
        seen.add(row["id"])
        if row.get("schema_version") != 1 or row.get("kind") not in KINDS or not row.get("group") or not row.get("source_ids"):
            raise ValueError("Invalid candidate metadata")
        check_messages(row["messages"])
        check_tools(row.get("tools", []))
        sha = content_hash(row)
        if row.get("content_sha256") != sha:
            raise ValueError("Candidate content changed without a new hash")
        decision = decisions.get(row["id"], {})
        if decision.get("content_sha256") != sha or decision.get("approved") is not True:
            rejected["unreviewed"] += 1
            continue
        if not decision.get("reviewer") or decision.get("privacy_checked") is not True or decision.get("evidence_checked") is not True:
            rejected["incomplete_review"] += 1
            continue
        if not set(row.get("issues", [])) <= set(decision.get("resolved_issues", [])):
            rejected["unresolved_issues"] += 1
            continue
        if SECRET.search(json.dumps(row, ensure_ascii=False)):
            rejected["secret"] += 1
            continue
        training_text = [row["messages"], row.get("reasoning_source", ""), row.get("short_reasoning", ""), row.get("tools", [])]
        if PII.search(json.dumps(training_text, ensure_ascii=False)) and not decision.get("pii_allowed"):
            rejected["pii"] += 1
            continue
        short = row.get("short_reasoning", "")
        if row["kind"] == "reasoning":
            cjk = len(re.findall(r"[\u4e00-\u9fff]", short))
            letters = len(re.findall(r"[A-Za-z\u4e00-\u9fff]", short))
            if not short or len(short) > 320 or cjk / max(1, letters) < 0.5:
                rejected["reasoning_language_or_budget"] += 1
                continue
            if not decision.get("faithfulness_checked") or not decision.get("answer_checked"):
                rejected["reasoning_unverified"] += 1
                continue
        accepted.append(row)
    return accepted, dict(rejected)


def split_rows(rows):
    """Connect sessions, source lineage, and normalized duplicate prompts before splitting."""
    parent = list(range(len(rows)))

    def find(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    seen = {}
    for i, row in enumerate(rows):
        prompt = [{"role": m["role"], "content": re.sub(r"\s+", "", m["content"]).casefold()} for m in row["messages"][:-1]]
        keys = ["g:" + row["group"], "p:" + digest(prompt), *["s:" + x for x in row["source_ids"]]]
        for key in keys:
            if key in seen:
                parent[find(i)] = find(seen[key])
            seen[key] = i
    components = collections.defaultdict(list)
    for i, row in enumerate(rows):
        components[find(i)].append(row)
    groups = sorted(components.values(), key=lambda g: (max(r["occurred_at"] for r in g), min(r["id"] for r in g)))
    result = {"train": [], "validation": [], "test": []}
    for i, group in enumerate(groups):
        # Whole connected groups, ordered by time. Small datasets never receive fake holdouts.
        label = "test" if len(groups) >= 10 and i >= math.floor(len(groups) * 0.9) else "validation" if len(groups) >= 10 and i >= math.floor(len(groups) * 0.8) else "train"
        result[label].extend(group)
    return result


def mix_rows(rows, weights, budget):
    if set(weights) != KINDS or any(not isinstance(v, (int, float)) or not math.isfinite(v) or v < 0 for v in weights.values()):
        raise ValueError("Provide four finite, nonnegative mix weights")
    if not math.isclose(sum(weights.values()), 1.0) or weights["base"] < 0.3:
        raise ValueError("Mix weights must sum to one and retain at least 30% base replay")
    pools = {k: sorted([r for r in rows if r["kind"] == k], key=lambda r: r["id"]) for k in KINDS}
    selected, usage = [], {}
    for kind, weight in weights.items():
        limit, used = int(budget * weight), 0
        for row in pools[kind]:
            # Character quotas are a planning proxy. The trainer reports exact tokenizer counts.
            cost = sum(len(m["content"]) for m in row["messages"]) + len(row.get("short_reasoning", ""))
            if used + cost <= limit:
                selected.append(row)
                used += cost
        usage[kind] = {"characters": used, "quota": limit, "available": len(pools[kind])}
        if weight and not used:
            raise ValueError("A nonzero mix category has no usable records: " + kind)
        if limit and used < limit * 0.8:
            raise ValueError("Insufficient data for the requested mix budget: " + kind)
    return selected, usage


def assemble(candidates, reviews, output, weights, budget):
    output = Path(output)
    if output.exists():
        raise ValueError("Use a new dataset output directory")
    accepted, rejected = reviewed(list(lines(candidates)), list(lines(reviews)))
    splits = split_rows(accepted)
    for split, values in splits.items():
        unique = {}
        for row in values:
            key = digest([row["messages"], row.get("short_reasoning", ""), row.get("tools", [])])
            if key in unique and unique[key]["kind"] != row["kind"]:
                raise ValueError("Duplicate content has conflicting category labels")
            unique.setdefault(key, row)
        splits[split] = list(unique.values())
    splits["train"], usage = mix_rows(splits["train"], weights, budget)
    if not splits["validation"] or not splits["test"]:
        raise ValueError("At least ten independent groups are required for held-out evaluation")
    selected = []
    for split, values in splits.items():
        seen = set()
        unique = []
        for row in values:
            key = digest([row["messages"], row.get("short_reasoning", ""), row.get("tools", [])])
            if key in seen:
                continue
            seen.add(key)
            unique.append(row)
            selected.append({"id": row["id"], "split": split, "sha256": row["content_sha256"], "kind": row["kind"]})
        write_lines(output / (split + ".jsonl"), unique)
    manifest = {"schema_version": 1, "candidate_sha256": hashlib.sha256(Path(candidates).read_bytes()).hexdigest(),
                "review_sha256": hashlib.sha256(Path(reviews).read_bytes()).hexdigest(),
                "mix": weights, "mix_usage": usage, "rejected": rejected, "records": selected,
                "files": {s: hashlib.sha256((output / (s + ".jsonl")).read_bytes()).hexdigest() for s in splits}}
    write(output / "manifest.json", manifest)
    return {"splits": dict(collections.Counter(r["split"] for r in selected)), "rejected": rejected}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("audit", "export"):
        sub = commands.add_parser(name)
        sub.add_argument("--archive", action="append", required=True)
        sub.add_argument("--requests", action="append", default=[])
        sub.add_argument("--output", required=True)
    sub = commands.add_parser("build")
    sub.add_argument("--candidates", required=True)
    sub.add_argument("--reviews", required=True)
    sub.add_argument("--output", required=True)
    sub.add_argument("--mix", required=True, help="JSON file with base/dialogue/persona/reasoning weights")
    sub.add_argument("--character-budget", type=int, required=True)
    sub = commands.add_parser("curate", help="Import authored or repaired prompt-answer records as unapproved candidates")
    sub.add_argument("--input", required=True)
    sub.add_argument("--output", required=True)
    sub = commands.add_parser("review-template")
    sub.add_argument("--candidates", required=True)
    sub.add_argument("--output", required=True)
    args = parser.parse_args()
    if args.command == "audit":
        result = audit(args.archive, args.requests)
        write(args.output, result)
    elif args.command == "export":
        result = export_candidates(args.archive, args.requests, args.output)
    elif args.command == "curate":
        if Path(args.output).exists():
            parser.error("Use a new candidate output file")
        values = [make_candidate(r["messages"], r["kind"], r["group"], r["source_ids"], r.get("occurred_at", 0),
                                 r.get("reasoning_source", ""), r.get("issues", [])) for r in lines(args.input)]
        for row, source in zip(values, lines(args.input)):
            row["short_reasoning"] = source.get("short_reasoning", "")
            if source.get("tools"):
                check_tools(source["tools"])
                row["tools"] = source["tools"]
            row["content_sha256"] = content_hash(row)
        write_lines(args.output, values)
        result = {"candidates": len(values), "approved": 0}
    elif args.command == "review-template":
        if Path(args.output).exists():
            parser.error("Use a new review output file")
        values = [{"id": r["id"], "content_sha256": r["content_sha256"], "approved": False, "reviewer": "",
                   "privacy_checked": False, "evidence_checked": False, "faithfulness_checked": False,
                   "answer_checked": False, "resolved_issues": []} for r in lines(args.candidates)]
        write_lines(args.output, values)
        result = {"pending_reviews": len(values)}
    else:
        if args.character_budget <= 0:
            parser.error("Character budget must be positive")
        result = assemble(args.candidates, args.reviews, args.output, read(args.mix), args.character_budget)
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
