"""Compare a local Gemma 4 base and adapter on static behavior fixtures."""

import argparse
import json
import re
import statistics
import time
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
THOUGHT_BUDGET = 120


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
    if re.search(r"<\|[^>]*>|<[^<]*\|>", text):
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


def score(case, prediction):
    expected = case["expect"]
    visible = prediction["text"]
    checks = {"complete": prediction["complete"]}
    reasoning = prediction["native_thoughts"]
    if case["suite"] == "tool":
        checks["exact_calls_and_replay_receipts"] = (
            len(prediction["replay_receipts"]) == len(prediction["calls"])
            == len(case["environment"]["tool_replay"])
        )
    else:
        checks["no_tool_call"] = not prediction["calls"]
    if case["suite"] == "initiative":
        try:
            decision = json.loads(visible, object_pairs_hook=unique_keys)
        except ValueError:
            decision = None
        shape = (isinstance(decision, dict) and set(decision) == {"thought", "speak", "text"}
                 and isinstance(decision["thought"], str) and isinstance(decision["text"], str)
                 and type(decision["speak"]) is bool)
        checks["strict_json"] = shape
        checks["speak_matches_policy"] = bool(shape and decision["speak"] is expected["speak"])
        checks["brief_chinese_reflection"] = bool(
            shape and 0 < len(decision["thought"].strip()) <= THOUGHT_BUDGET
            and chinese_ratio(decision["thought"]) >= 0.7
        )
        visible = decision["text"] if shape else ""
        if not expected["speak"]:
            checks["silent_text"] = bool(shape and visible == "")
    if case["suite"] != "initiative" or expected["speak"]:
        checks["chinese_reply"] = chinese_ratio(visible) >= 0.7
        normalized = visible.replace("：", ":").replace(" ", "")
        checks["required_facts"] = all(any(value.replace(" ", "") in normalized for value in options)
                                       for options in expected.get("groups", []))
        checks["forbidden_terms_absent"] = not any(value in visible for value in expected.get("none", []))
        checks["concise_reply"] = 0 < len(visible.strip()) <= expected.get("max_chars", 120)
    thought_chars = sum(len(part) for part in reasoning)
    short_thought = all(0 < len(part) <= THOUGHT_BUDGET and chinese_ratio(part) >= 0.7 for part in reasoning)
    return {
        "passed": all(checks.values()), "checks": checks,
        "thought_chars": thought_chars,
        "thought_segments": len(reasoning),
        "thought_chinese_ratio": round(chinese_ratio("\n".join(reasoning)), 3) if reasoning else None,
        "brief_chinese_native_thought": bool(reasoning) and short_thought,
    }


def run_case(case, generate):
    started = time.monotonic()
    messages = list(case["messages"])
    session = ReplaySession(case)
    receipts, emitted, thoughts, traces = [], [], [], []
    tools, error, text = case.get("tools", []), None, ""
    steps = len(session.steps) + 1
    try:
        for step in range(steps):
            raw, count, starts_in_thought = generate(messages, tools)
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


def generation_function(model, tokenizer, config, max_new_tokens):
    import torch

    stop_ids = tokenizer.convert_tokens_to_ids([TURN_STOP, EOS, TOOL_STOP])

    def generate(messages, tools):
        prompt = tokenizer.apply_chat_template(messages, tools=tools or None, tokenize=False,
                                               add_generation_prompt=True, enable_thinking=True)
        inputs = tokenizer(prompt, add_special_tokens=False, return_tensors="pt").to(model.device)
        prompt_length = inputs["input_ids"].shape[1]
        if prompt_length > config["max_length"]:
            raise ValueError("Evaluation prompt exceeds configured context budget")
        with torch.inference_mode():
            output = model.generate(**inputs, max_new_tokens=max_new_tokens, do_sample=False, use_cache=True,
                                    eos_token_id=stop_ids, pad_token_id=tokenizer.pad_token_id)
        generated = output[0, prompt_length:]
        raw = tokenizer.decode(generated, skip_special_tokens=False, clean_up_tokenization_spaces=False)
        return raw, len(generated), prompt.endswith(THOUGHT_START)

    return generate


def verify_suite(cases, dataset):
    if not cases or len({case["id"] for case in cases}) != len(cases):
        raise ValueError("Use distinct evaluation case IDs")
    for case in cases:
        if case["suite"] not in ("dialog", "tool", "initiative") or case["messages"][-1]["role"] != "user":
            raise ValueError("Invalid case kind or missing final input")
        ReplaySession(case)
    report = {"fixture_sha256": digest(cases), "cases": len(cases), "split_overlap_check": None}
    if dataset:
        training_manifest = read_json(dataset / "manifest.json")
        user_turns = {message["content"].strip() for case in cases for message in case["messages"]
                      if message["role"] == "user"}
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
            seen += len(rows)
            manifest.append({"file": path.name, "sha256": sha, "rows": len(rows)})
        report["split_overlap_check"] = {"dataset_manifest_sha256": digest(training_manifest), "rows": seen,
                                         "splits": manifest, "exact_user_turn_overlap": 0}
    return report


def summarize(results):
    reasoning = [row["thought_chars"] for row in results if row["native_thoughts"]]
    return {
        "passed": sum(row["passed"] for row in results),
        "complete": sum(row["complete"] for row in results),
        "brief_chinese_native_thought": sum(row["brief_chinese_native_thought"] for row in results),
        "native_thought_cases": len(reasoning),
        "native_thought_median_chars": statistics.median(reasoning) if reasoning else None,
        "median_latency_ms": statistics.median(row["latency_ms"] for row in results),
        "generated_tokens": sum(trace["tokens"] for row in results for trace in row["generations"]),
        "by_suite": {
            name: {"passed": sum(row["passed"] for row in results if row["suite"] == name),
                   "total": sum(row["suite"] == name for row in results)}
            for name in ("dialog", "tool", "initiative")
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
    args = parser.parse_args()
    cases = read_json(args.cases)
    verified = verify_suite(cases, args.dataset)
    if args.check:
        print(canonical(verified).decode("utf-8"))
        return
    if not args.config or not args.adapter or not args.output:
        parser.error("Inference requires --config, --adapter and --output")
    if args.max_new_tokens < 32 or args.output.exists():
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
    for variant in ("baseline", "adapter"):
        if variant == "adapter":
            model = PeftModel.from_pretrained(model, args.adapter, is_trainable=False, local_files_only=True).eval()
        generate = generation_function(model, tokenizer, config, args.max_new_tokens)
        results[variant] = []
        for case in cases:
            outcome = run_case(case, generate)
            results[variant].append(outcome)
            print(variant, case["id"], "PASS" if outcome["passed"] else "FAIL",
                  sum(trace["tokens"] for trace in outcome["generations"]), "tokens", flush=True)

    report = {
        **verified, "mode": "paired_local_greedy_smoke_test",
        "interpretation": "Static lexical and structural checks on a small fixture set.",
        "base": identity, "adapter_sha256": tree_hash(args.adapter), "config_sha256": digest(config),
        "quantization": "nf4-double" if config["method"] == "qlora" else None,
        "precision": config["precision"], "gpu": torch.cuda.get_device_name(config["device"]),
        "max_new_tokens_per_generation": args.max_new_tokens, "packages": _versions(),
        "enable_thinking": True,
        "generation": {"do_sample": False, "use_cache": True, "seed": config["seed"], "eos_token_ids": markers},
        "summary": {name: summarize(rows) for name, rows in results.items()},
        "identical_generated_cases": sum(
            left["generations"] == right["generations"]
            for left, right in zip(results["baseline"], results["adapter"], strict=True)
        ),
        "observations": results,
    }
    write_json(args.output, report)
    print(canonical(report["summary"]).decode("utf-8"))


if __name__ == "__main__":
    main()
