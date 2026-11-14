"""Compare a local Gemma 4 base and adapter on static behavior fixtures."""

import argparse
import json
import re
import statistics
import time
import unicodedata
from pathlib import Path

from .common import canonical, digest, file_hash, read_json, read_jsonl, tree_hash, write_json
from .finetune import _base_identity, _versions, configuration, load_base_model
from .replay import ReplaySession, parse_calls
from .trajectory import replay_receipt


THOUGHT_START = "<|channel>thought\n"
THOUGHT_END = "<channel|>"
TOOL_STOP = "<|tool_response>"
TURN_STOP = "<turn|>"
EOS = "<eos>"
HAN = r"[\u3400-\u9fff]"
LETTERS = r"[A-Za-z\u3400-\u9fff]"
URL_TEXT = r"https?://[^\s)\]。，！？；]+"
THOUGHT_BUDGET = 120
JSON_ENVELOPE = re.compile(r"^```(?:json)?[ \t]*\r?\n([\s\S]*)\n```$", re.I)
TERMINAL_ACT = re.compile(r"\s*<\|ACT\s[\s\S]*?\|>\s*$")


def split_completion(raw, *, starts_in_thought=False):
    text = THOUGHT_START + raw if starts_in_thought else raw
    text = text.strip()
    thought = ""
    if text.startswith(THOUGHT_START):
        thought, end, text = text[len(THOUGHT_START):].partition(THOUGHT_END)
        if not end:
            raise ValueError("Native thought did not close")
    stop_token = next((token for token in (TURN_STOP, EOS, TOOL_STOP) if text.endswith(token)), "")
    if stop_token:
        text = text[:-len(stop_token)]
    calls = parse_calls(text)
    text = re.sub(r"<\|tool_call>.*?<tool_call\|>", "", text, flags=re.S).strip()
    if re.search(r"<\|[^>]*>|<[^<]*\|>", TERMINAL_ACT.sub("", text)):
        raise ValueError("Unexpected native markup in visible output")
    if calls and (not stop_token or text):
        raise ValueError("Native tool turn did not end at the tool-response boundary")
    if not calls and stop_token not in (TURN_STOP, EOS):
        raise ValueError("Visible response did not end at a model stop token")
    return thought.strip(), calls, text


def chinese_ratio(text):
    return len(re.findall(HAN, text)) / max(1, len(re.findall(LETTERS, text)))


def unique_keys(pairs):
    fields = dict(pairs)
    if len(fields) != len(pairs):
        raise ValueError("Duplicate decision key")
    return fields


def normalized_text(text):
    return re.sub(r"\s+", "", unicodedata.normalize("NFKC", text)).casefold()


def reflection_json(visible):
    """Read a JSON object after unwrapping the AIRI output envelope."""
    body = TERMINAL_ACT.sub("", visible.strip()).strip()
    envelope = JSON_ENVELOPE.fullmatch(body)
    if envelope:
        body = envelope[1]
    try:
        return json.loads(body, object_pairs_hook=unique_keys)
    except ValueError:
        return None


def reflection_decision(visible):
    """Check the initiative fields accepted by AIRI."""
    decision = reflection_json(visible)
    if (isinstance(decision, dict) and set(decision) == {"thought", "speak", "text"}
            and isinstance(decision["thought"], str) and len(decision["thought"]) <= 4000
            and isinstance(decision["text"], str) and len(decision["text"]) <= 1000
            and type(decision["speak"]) is bool):
        return decision
    return None


def sentence_count(text):
    """Count Chinese sentence endings and explicit separate answer lines."""
    text = re.sub(URL_TEXT, "URL", text)
    return sum(bool(part.strip()) for part in re.split(r"[。！？!?\n]+|(?<!\d)\.(?=\s|$)", text))


def reply_chinese_ratio(visible, expected):
    text = re.sub(URL_TEXT, "", visible)
    for literal in expected.get("verbatim_fields", []):
        text = text.replace(literal, "")
    return chinese_ratio(text)


def document_checks(output, expected):
    """Evaluate consolidation writes against frozen source and revision expectations."""
    docs = output.get("documents") if isinstance(output, dict) and set(output) == {"documents"} else None
    fields = {"path", "text", "nature", "sources", "expectedRevision"}
    shape = isinstance(docs, list) and len(docs) <= 4 and all(
        isinstance(doc, dict) and set(doc) == fields and isinstance(doc["path"], str)
        and isinstance(doc["text"], str) and 0 < len(doc["text"].strip()) <= 12000
        and doc["nature"] in ("reported", "observed", "inferred", "feeling") and isinstance(doc["sources"], list)
        and all(isinstance(source, str) for source in doc["sources"])
        and type(doc["expectedRevision"]) is int and doc["expectedRevision"] >= 0 for doc in docs
    )
    updates = expected["documents"]
    by_path = {doc["path"]: doc for doc in docs} if shape else {}
    count = shape and len(docs) == len(by_path) == len(updates)
    same_paths = count and set(by_path) == {doc["path"] for doc in updates}
    bindings = bool(same_paths and all(
        by_path[item["path"]]["expectedRevision"] == item["expectedRevision"]
        and by_path[item["path"]]["nature"] == item["nature"]
        and sorted(by_path[item["path"]]["sources"]) == sorted(item["sources"]) for item in updates
    ))
    content = bool(same_paths and all(
        all(any(normalized_text(value) in normalized_text(by_path[item["path"]]["text"]) for value in options)
            for options in item.get("groups", []))
        and not any(re.search(pattern, normalized_text(by_path[item["path"]]["text"])) for pattern in item.get("none_regex", []))
        for item in updates
    ))
    return {"airi_consolidation_fields": bool(shape), "consolidation_paths": bool(same_paths),
            "consolidation_source_and_revision": bindings, "consolidation_text_grounded": content}


def score(case, prediction):
    expected = case["expect"]
    budget = expected.get("thought_max_chars", THOUGHT_BUDGET)
    visible = prediction["text"]
    checks = {"complete": prediction["complete"]}
    reasoning = prediction["native_thoughts"]
    steps = case.get("environment", {}).get("tool_replay", [])
    if case["suite"] == "tool" or steps:
        checks["exact_calls_and_replay_receipts"] = (
            len(prediction["replay_receipts"]) == len(prediction["calls"]) == len(steps)
        )
    else:
        checks["no_tool_call"] = not prediction["calls"]
    strict_json = None
    if case["suite"] == "reflection":
        parsed = reflection_json(visible)
        checks.update(document_checks(parsed, expected))
        strict_json = checks["airi_consolidation_fields"] and visible.lstrip().startswith("{") and visible.rstrip().endswith("}")
    elif case["suite"] == "initiative":
        decision = reflection_decision(visible)
        shape = decision is not None
        strict_json = shape and visible.lstrip().startswith("{") and visible.rstrip().endswith("}")
        checks["airi_decision_fields"] = shape
        checks["speak_matches_policy"] = bool(shape and decision["speak"] is expected["speak"])
        checks["brief_chinese_reflection"] = bool(
            shape and 0 < len(decision["thought"].strip()) <= budget
            and chinese_ratio(decision["thought"]) >= 0.7
        )
        visible = decision["text"] if shape else ""
        if not expected["speak"]:
            checks["silent_text"] = bool(shape and visible == "")
    normalized = normalized_text(visible)
    checks["forbidden_terms_absent"] = not (
        any(normalized_text(value) in normalized for value in expected.get("none", []))
        or any(re.search(pattern, normalized, re.I) for pattern in expected.get("none_regex", []))
    )
    if case["suite"] in {"dialog", "tool"} or (case["suite"] == "initiative" and expected["speak"]):
        checks["chinese_reply"] = reply_chinese_ratio(visible, expected) >= 0.7
        checks["required_facts"] = all(any(normalized_text(value) in normalized for value in options)
                                       for options in expected.get("groups", []))
        checks["concise_reply"] = 0 < len(visible.strip()) <= expected.get("max_chars", 120)
        if "max_sentences" in expected:
            checks["sentence_limit"] = sentence_count(visible) <= expected["max_sentences"]
    thought_chars = sum(len(part) for part in reasoning)
    short_thought = (bool(reasoning) and len(reasoning) == len(prediction["generations"])
                     and all(0 < len(part) <= budget and chinese_ratio(part) >= 0.7 for part in reasoning))
    return {
        "passed": all(checks.values()), "checks": checks,
        "category": case.get("category", case["suite"]), "strict_json": strict_json,
        "thought_chars": thought_chars,
        "thought_segments": len(reasoning),
        "thought_chinese_ratio": round(chinese_ratio("\n".join(reasoning)), 3) if reasoning else None,
        "brief_chinese_native_thought": short_thought,
    }


def case_steps(case):
    started = time.monotonic()
    messages = list(case["messages"])
    session = ReplaySession(case)
    receipts, emitted, thoughts, traces = [], [], [], []
    tools, error, text = case.get("tools", []), None, ""
    steps = len(session.steps) + 1
    try:
        for step in range(steps):
            raw, count, starts_in_thought = yield messages, tools
            traces.append({"raw": raw, "tokens": count, "prompt_ends_in_thought": starts_in_thought})
            thought, calls, text = split_completion(raw, starts_in_thought=starts_in_thought)
            if thought:
                thoughts.append(thought)
            if not calls:
                if session.index != len(session.steps):
                    raise ValueError("Required tool replay step was not called")
                break
            for index, call in enumerate(calls):
                call["id"] = f"call-{step}-{index}"
            emitted.extend(calls)
            if not tools:
                raise ValueError("No tool was available in this case")
            messages.append({"role": "assistant", "content": "", "reasoning_content": thought, "tool_calls": calls})
            for call in calls:
                message, receipt = replay_receipt(session, call, source_id=case["id"])
                message.pop("loss_weight")
                messages.append(message)
                receipts.append({**receipt, "result": message["content"]})
        else:
            raise ValueError("Expected a final reply after the tool response")
    except ValueError as failure:
        error = str(failure)
    prediction = {
        "id": case["id"], "suite": case["suite"], "text": text,
        "complete": error is None, "calls": emitted, "replay_receipts": receipts,
        "native_thoughts": thoughts, "generations": traces, "error": error,
        "latency_ms": round((time.monotonic() - started) * 1000),
    }
    return {**prediction, **score(case, prediction)}


def run_case(case, generate):
    steps = case_steps(case)
    request = next(steps)
    while True:
        try:
            request = steps.send(generate(*request))
        except StopIteration as finished:
            return finished.value


def run_suite(cases, generate_batch, batch_size):
    for start in range(0, len(cases), batch_size):
        batch = cases[start:start + batch_size]
        outcomes = {}
        pending = [case_steps(case) for case in batch]
        requests = [(steps, next(steps)) for steps in pending]
        while requests:
            responses = generate_batch([request for _, request in requests])
            remaining = []
            for (steps, _), response in zip(requests, responses, strict=True):
                try:
                    remaining.append((steps, steps.send(response)))
                except StopIteration as finished:
                    outcomes[finished.value["id"]] = finished.value
            requests = remaining
        yield from (outcomes[case["id"]] for case in batch)


def generation_function(model, tokenizer, config, max_new_tokens):
    import torch

    stop_ids = tokenizer.convert_tokens_to_ids([TURN_STOP, EOS, TOOL_STOP])

    def generate(requests):
        prompts = [tokenizer.apply_chat_template(messages, tools=tools or None, tokenize=False,
                                                 add_generation_prompt=True, enable_thinking=True)
                   for messages, tools in requests]
        inputs = tokenizer(prompts, add_special_tokens=False, padding=True, padding_side="left",
                           return_tensors="pt").to(model.device)
        prompt_length = inputs["input_ids"].shape[1]
        if prompt_length > config["max_length"]:
            raise ValueError("Evaluation prompt exceeds configured context budget")
        with torch.inference_mode():
            output = model.generate(**inputs, max_new_tokens=max_new_tokens, do_sample=False, use_cache=True,
                                    eos_token_id=stop_ids, pad_token_id=tokenizer.pad_token_id)
        completions = []
        for prompt, tail in zip(prompts, output[:, prompt_length:].tolist(), strict=True):
            for index, token in enumerate(tail):
                if token in stop_ids:
                    tail = tail[:index + 1]
                    break
            raw = tokenizer.decode(tail, skip_special_tokens=False, clean_up_tokenization_spaces=False)
            completions.append((raw, len(tail), prompt.endswith(THOUGHT_START)))
        return completions

    return generate


def verify_suite(cases, dataset):
    if not cases or len({case["id"] for case in cases}) != len(cases):
        raise ValueError("Use distinct evaluation case IDs")
    for case in cases:
        roles = [message["role"] for message in case["messages"]]
        system_only = case["suite"] in ("initiative", "reflection") and roles == ["system"]
        if case["suite"] not in ("dialog", "tool", "initiative", "reflection") or not roles or (roles[-1] != "user" and not system_only):
            raise ValueError("Invalid case kind or missing final input")
        ReplaySession(case)
    report = {"fixture_sha256": digest(cases), "cases": len(cases), "split_overlap_check": None}
    if dataset:
        training_manifest = read_json(dataset / "manifest.json")
        user_turns = {message["content"].strip() for case in cases for message in case["messages"]
                      if message["role"] == "user"}
        system_only_turns = {case["messages"][0]["content"].strip() for case in cases
                             if len(case["messages"]) == 1 and case["messages"][0]["role"] == "system"}
        seen, manifest = 0, []
        for split in ("train", "validation", "test"):
            path = dataset / f"{split}.jsonl"
            sha = file_hash(path)
            if training_manifest["files"][split] != sha:
                raise ValueError("Dataset file changed after its manifest was written")
            rows = read_jsonl(path)
            for row in rows:
                for message in row["messages"]:
                    if message["role"] == "user" and message["content"].strip() in user_turns:
                        raise ValueError("An evaluation turn repeats in the " + split + " split")
                    if message["role"] == "system" and message["content"].strip() in system_only_turns:
                        raise ValueError("A system-only evaluation turn repeats in the " + split + " split")
            seen += len(rows)
            manifest.append({"file": path.name, "sha256": sha, "rows": len(rows)})
        report["split_overlap_check"] = {"dataset_manifest_sha256": digest(training_manifest), "rows": seen,
                                         "splits": manifest, "exact_user_turn_overlap": 0, "exact_system_only_turn_overlap": 0}
    return report


def summarize(results):
    reasoning = [row["thought_chars"] for row in results if row["native_thoughts"]]
    checks = {}
    for key in sorted({key for row in results for key in row["checks"]}):
        applied = [row["checks"][key] for row in results if key in row["checks"]]
        checks[key] = {"passed": sum(applied), "total": len(applied)}
    categories = {}
    for category in sorted({row.get("category", row["suite"]) for row in results}):
        group = [row for row in results if row.get("category", row["suite"]) == category]
        categories[category] = {"passed": sum(row["passed"] for row in group), "total": len(group),
                                "complete": sum(row["complete"] for row in group),
                                "brief_chinese_native_thought": sum(row["brief_chinese_native_thought"] for row in group),
                                "forbidden_reply_matches": sum(not row["checks"]["forbidden_terms_absent"] for row in group)}
    return {
        "passed": sum(row["passed"] for row in results),
        "complete": sum(row["complete"] for row in results),
        "brief_chinese_native_thought": sum(row["brief_chinese_native_thought"] for row in results),
        "native_thought_cases": len(reasoning),
        "native_thought_median_chars": statistics.median(reasoning) if reasoning else None,
        "strict_json": sum(row.get("strict_json") is True for row in results),
        "checks": checks,
        "by_category": categories,
        "median_latency_ms": statistics.median(row["latency_ms"] for row in results),
        "generated_tokens": sum(trace["tokens"] for row in results for trace in row["generations"]),
        "by_suite": {
            name: {"passed": sum(row["passed"] for row in results if row["suite"] == name),
                   "total": sum(row["suite"] == name for row in results)}
            for name in ("dialog", "tool", "initiative", "reflection")
            if name != "reflection" or any(row["suite"] == name for row in results)
        },
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cases", type=Path, required=True, help="Path to a local behavior fixture array")
    parser.add_argument("--dataset", type=Path, help="Check user-turn overlap with the three training data splits")
    parser.add_argument("--check", action="store_true", help="Check fixture integrity without loading weights")
    parser.add_argument("--config", type=Path)
    parser.add_argument("--adapter", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--max-new-tokens", type=int, default=384)
    parser.add_argument("--batch-size", type=int, default=1)
    parser.add_argument("--candidate-only", action="store_true", help="Run just the adapter on an already benchmarked suite")
    args = parser.parse_args()
    cases = read_json(args.cases)
    verified = verify_suite(cases, args.dataset)
    if args.check:
        print(canonical(verified).decode("utf-8"))
        return
    if not args.config or not args.adapter or not args.output:
        parser.error("Inference requires --config, --adapter and --output")
    if args.max_new_tokens < 32 or args.batch_size < 1 or args.output.exists():
        parser.error("Use at least 32 generation tokens and a new output path")

    import torch
    from peft import PeftModel
    from transformers import AutoTokenizer, set_seed

    config = configuration(read_json(args.config))
    if read_json(Path(config["model"]) / "config.json").get("model_type") != "gemma4":
        raise ValueError("Native smoke fixtures use the Gemma 4 chat template")
    if not torch.cuda.is_available() or torch.cuda.device_count() != 1:
        raise ValueError("Select one CUDA device, as used in training")
    lineage = read_json(args.adapter / "finetune-lineage.json")
    overlap = verified["split_overlap_check"]
    if overlap and overlap["dataset_manifest_sha256"] != lineage["dataset_sha256"]:
        raise ValueError("Run the overlap check on the dataset used to train this adapter")
    identity = _base_identity(config["model"])
    if identity != lineage["base"]:
        raise ValueError("Evaluation base differs from the trained adapter")
    if digest(config) != lineage["config_sha256"]:
        raise ValueError("Use the same training config for the comparison")
    torch.cuda.set_device(config["device"])
    set_seed(config["seed"])
    tokenizer = AutoTokenizer.from_pretrained(config["tokenizer"], local_files_only=True, trust_remote_code=False)
    if tokenizer.pad_token_id is None:
        tokenizer.pad_token = tokenizer.eos_token
    markers = tokenizer.convert_tokens_to_ids([TURN_STOP, EOS, TOOL_STOP])
    if len(set(markers)) != 3 or tokenizer.unk_token_id in markers:
        raise ValueError("Tokenizer lacks Gemma 4 native stop tokens")
    token_root = Path(config["tokenizer"])
    token_files = {name: file_hash(token_root / name) for name in lineage["tokenizer_files"]}
    token_identity = {"files": token_files, "chat_template_sha256": digest(str(tokenizer.chat_template))}
    if digest(token_identity) != lineage["tokenizer_sha256"]:
        raise ValueError("The evaluation tokenizer differs from training")
    model = load_base_model(config).eval()
    print("Loaded local base. Starting paired evaluation.", flush=True)
    results = {}
    for variant in (("adapter",) if args.candidate_only else ("baseline", "adapter")):
        if variant == "adapter":
            model = PeftModel.from_pretrained(model, args.adapter, is_trainable=False, local_files_only=True).eval()
        generate = generation_function(model, tokenizer, config, args.max_new_tokens)
        results[variant] = []
        for outcome in run_suite(cases, generate, args.batch_size):
            results[variant].append(outcome)
            print(variant, outcome["id"], "PASS" if outcome["passed"] else "FAIL",
                  sum(trace["tokens"] for trace in outcome["generations"]), "tokens", flush=True)

    report = {
        **verified, "mode": "paired_local_greedy_smoke_test",
        "interpretation": "Static lexical and structural checks on a small fixture set.",
        "base": identity, "adapter_sha256": tree_hash(args.adapter), "config_sha256": digest(config),
        "quantization": "nf4-double" if config["method"] == "qlora" else None,
        "precision": config["precision"], "gpu": torch.cuda.get_device_name(config["device"]),
        "max_new_tokens_per_generation": args.max_new_tokens, "packages": _versions(),
        "enable_thinking": True,
        "generation": {"do_sample": False, "use_cache": True, "seed": config["seed"], "eos_token_ids": markers,
                       "batch_size": args.batch_size},
        "summary": {name: summarize(rows) for name, rows in results.items()},
        "identical_generated_cases": sum(
            left["generations"] == right["generations"]
            for left, right in zip(results["baseline"], results["adapter"], strict=True)
        ) if "baseline" in results else None,
        "observations": results,
    }
    write_json(args.output, report)
    print(canonical(report["summary"]).decode("utf-8"))


if __name__ == "__main__":
    main()
