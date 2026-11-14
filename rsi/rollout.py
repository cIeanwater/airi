"""Replay tool fixtures with a local model and compile refined trajectories."""

import argparse
import json
from collections import Counter
from concurrent.futures import ProcessPoolExecutor
from functools import lru_cache
from pathlib import Path
from urllib.parse import urlparse
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

from .cleaning import _hard_checks
from .common import digest, file_hash, read_json, read_jsonl, write_json, write_jsonl
from .finetune import CHAT_SFT_KIND, inspect_dataset
from .replay import ReplaySession
from .trajectory import build_trajectory, capture_request, compile_trajectory, replay_receipt


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, msg, headers, newurl):
        return None


def rollout(seed, endpoint, model, output, *, max_tokens=512):
    address = urlparse(endpoint)
    if (address.scheme != "http" or address.hostname not in {"127.0.0.1", "::1", "localhost"}
            or address.username or address.password or address.query or address.fragment):
        raise ValueError("Use a local HTTP model endpoint")
    path = Path(output)
    if max_tokens < 1:
        raise ValueError("Set a positive generation token budget")
    if path.exists():
        raise ValueError("Use a new rollout output file")
    session = ReplaySession(seed)
    messages = [{"role": "system", "content": seed["system"]}]
    evidence, responses = [], []
    opener = build_opener(ProxyHandler({}), NoRedirect())
    run = {"status": "running", "seed_sha256": digest(seed), "model": model,
           "messages": messages, "evidence": evidence, "responses": responses}
    try:
        for user_turn in seed["user_turns"]:
            messages.append({"role": "user", "content": user_turn})
            for _ in range(6):
                payload = {"model": model, "messages": messages, "tools": seed["tools"],
                           "chat_template_kwargs": seed.get("template_kwargs", {"enable_thinking": True}),
                           "max_tokens": max_tokens, "temperature": 0, "stream": False}
                request = Request(endpoint.rstrip("/") + "/chat/completions",
                                  data=json.dumps(payload).encode(), headers={"Content-Type": "application/json"})
                with opener.open(request, timeout=90) as response:
                    result = json.loads(response.read(2 * 1024 * 1024))
                responses.append(result)
                if result.get("model") != model:
                    raise ValueError("The server returned a different model")
                choice = result["choices"][0]
                message = {k: v for k, v in choice["message"].items()
                           if k in {"role", "content", "reasoning_content", "tool_calls"}}
                if message.get("role") != "assistant":
                    raise ValueError("The server did not return an assistant message")
                messages.append(message)
                calls = message.get("tool_calls") or []
                if calls and choice["finish_reason"] == "tool_calls":
                    for call in calls:
                        receipt, proof = replay_receipt(session, call, source_id=seed["source_id"])
                        receipt.pop("loss_weight", None)
                        messages.append(receipt)
                        evidence.append(proof)
                elif not calls and choice["finish_reason"] == "stop" and message.get("content"):
                    break
                else:
                    raise ValueError("The model stopped before completing the turn")
            else:
                raise ValueError("The model exceeded the tool round budget")
        if session.index != len(session.steps):
            raise ValueError("The model did not consume each recorded tool step")
        run["status"] = "finished"
    except Exception as error:
        run["status"] = "failed"
        run["error"] = str(error)
        raise
    finally:
        write_json(path, run)
    return run


def _native_target_size(sequence, tokenizer):
    labels = sequence["labels"]
    first = next(index for index, token in enumerate(labels) if token != -100)
    if not first or labels[first:] != sequence["input_ids"][first:]:
        raise ValueError("Training targets do not match the native suffix")
    target = tokenizer.decode(labels[first:], skip_special_tokens=False)
    if "<channel|>" not in target or "<tool_response|>" in target:
        raise ValueError("Expected native thinking and answer tokens without the tool receipt body")
    return len(labels) - first


def assemble(specs, tokenizer, max_length, output, *, verify_native=False):
    out = Path(output)
    if not specs:
        raise ValueError("Supply at least one refined trajectory")
    rows, proofs, records, checks = [], [], [], []
    identities = set()
    scope = specs[0]["scope"]
    for spec in specs:
        if spec["scope"] != scope or not spec["source_ids"] or spec["id"] in identities:
            raise ValueError("Each trajectory needs a unique ID, source, and shared scope")
        identities.add(spec["id"])
        row = build_trajectory(spec["messages"], spec["tools"], evidence=spec["evidence"],
                               template_kwargs=spec.get("template_kwargs"))
        if spec["kind"] == "initiative":
            _hard_checks(row["messages"], "initiative", spec.get("policy", {}))
        encoded = compile_trajectory(row, tokenizer, max_length)
        records.append({"split": "train", "content_sha256": digest(row),
                        **{k: spec[k] for k in ("id", "group", "kind", "source_ids")}})
        proofs.append({"id": spec["id"], "row_sha256": digest(row), "evidence": spec["evidence"],
                       "refinement": spec["refinement"]})
        checks.append({"id": spec["id"], "user_turns": sum(m["role"] == "user" for m in row["messages"]),
                       "messages": len(row["messages"]), "receipts": len(spec["evidence"]),
                       "targets": [{"tokens": len(s["input_ids"]), "loss_tokens": (
                           _native_target_size(s, tokenizer) if verify_native else sum(label != -100 for label in s["labels"])
                       )}
                                   for s in encoded]})
        rows.append(row)
    out.mkdir(parents=True, exist_ok=False)
    files = {}
    for split in ("train", "validation", "test"):
        path = out / (split + ".jsonl")
        write_jsonl(path, rows if split == "train" else [])
        files[split] = file_hash(path)
    write_jsonl(out / "provenance.jsonl", proofs)
    write_json(out / "manifest.json", {"kind": CHAT_SFT_KIND, "schema_version": 1, "scope": scope,
                                      "records": records, "files": files,
                                      "provenance_sha256": file_hash(out / "provenance.jsonl"),
                                      "readiness": {"ready": False, "reasons": ["pilot_without_holdout"]}})
    report = {"max_length": max_length, "trajectories": checks}
    write_json(out / "report.json", report)
    return report


def audit(dataset, tokenizer, max_length):
    """Recheck the exported rows, receipt evidence, and native loss boundaries."""
    root = Path(dataset)
    splits, data = inspect_dataset(root)
    if not data["pilot_ready"] and not data["ready"]:
        raise ValueError("The dataset has unresolved manifest or split findings")
    compact = read_jsonl(root / "train.jsonl")
    proofs = read_jsonl(root / "provenance.jsonl")
    checked = {"rows": len(compact), "sequences": 0, "supervised_tokens": 0, "longest_sequence": 0,
               "tool_receipts": 0, "assistant_only_rows": 0, "native_thinking_targets": 0}
    seen = set()
    for row, restored, proof in zip(compact, splits["train"], proofs, strict=True):
        identity = digest(row)
        if identity in seen or proof["id"] != restored["id"] or proof["row_sha256"] != identity:
            raise ValueError("Duplicate row or changed provenance: " + restored["id"])
        seen.add(identity)
        verified = build_trajectory(row["messages"], row["tools"], evidence=proof["evidence"],
                                    template_kwargs=row["template_kwargs"])
        if verified != row:
            raise ValueError("Exported trajectory differs from its checked form")
        checked["tool_receipts"] += len(proof["evidence"])
        checked["assistant_only_rows"] += not any(message["role"] == "user" for message in row["messages"])
        for sequence in compile_trajectory(verified, tokenizer, max_length):
            target_size = _native_target_size(sequence, tokenizer)
            checked["native_thinking_targets"] += 1
            checked["sequences"] += 1
            checked["supervised_tokens"] += target_size
            checked["longest_sequence"] = max(checked["longest_sequence"], len(sequence["input_ids"]))
    return {"manifest_sha256": data["manifest_sha256"],
            "provenance_bound": "provenance_sha256" in read_json(root / "manifest.json"),
            "checked": checked, "passed": True}


@lru_cache(maxsize=1)
def _local_tokenizer(path):
    from transformers import AutoTokenizer

    return AutoTokenizer.from_pretrained(path, local_files_only=True, trust_remote_code=False)


def _compile_shard(task):
    source, out, tokenizer_path, tokenizer_sha, max_length = task
    receipt = out / "compile-receipt.json"
    inputs = {"spec_sha256": file_hash(source), "tokenizer_sha256": tokenizer_sha, "max_length": max_length}
    if receipt.is_file():
        previous = read_json(receipt)
        if previous["inputs"] != inputs or previous["manifest_sha256"] != file_hash(out / "manifest.json"):
            raise ValueError("Saved shard does not match current inputs: " + str(source))
        inspect_dataset(out)
        return previous

    tokenizer = _local_tokenizer(tokenizer_path)
    specs = read_jsonl(source)
    report = assemble(specs, tokenizer, max_length, out, verify_native=True)
    targets = [target for row in report["trajectories"] for target in row["targets"]]
    result = {"inputs": inputs, "manifest_sha256": file_hash(out / "manifest.json"), "rows": len(specs),
              "kinds": dict(Counter(row["kind"] for row in specs)),
              "assistant_only_rows": sum(not row["user_turns"] for row in report["trajectories"]),
              "target_sequences": len(targets), "target_tokens": sum(item["loss_tokens"] for item in targets),
              "longest_sequence": max(item["tokens"] for item in targets),
              "tool_receipts": sum(row["receipts"] for row in report["trajectories"])}
    write_json(receipt, result)
    return result


def assemble_shards(inputs, tokenizer, max_length, output, workers):
    """Compile numbered source files in parallel and retain independent receipts."""
    sources = sorted(Path(inputs).glob("spec-*.jsonl"))
    if not sources or workers < 1:
        raise ValueError("Supply numbered JSONL shards and at least one worker")
    root = Path(output)
    token_root = Path(tokenizer)
    names = ("tokenizer.json", "tokenizer.model", "tokenizer_config.json", "chat_template.jinja", "special_tokens_map.json")
    token_files = {name: file_hash(token_root / name) for name in names if (token_root / name).is_file()}
    if not token_files:
        raise ValueError("Expected local tokenizer files")
    tasks = [(source, root / source.stem, token_root, digest(token_files), max_length) for source in sources]
    with ProcessPoolExecutor(max_workers=workers) as executor:
        results = list(executor.map(_compile_shard, tasks))
    seen_ids, seen_rows = set(), set()
    for _, shard, *_ in tasks:
        for row in read_json(shard / "manifest.json")["records"]:
            if row["id"] in seen_ids or row["content_sha256"] in seen_rows:
                raise ValueError("Duplicate row across shards: " + row["id"])
            seen_ids.add(row["id"])
            seen_rows.add(row["content_sha256"])
    report = {"shards": dict(zip((source.name for source in sources), results, strict=True)),
              "totals": {key: sum(row[key] for row in results) for key in (
                  "rows", "assistant_only_rows", "target_sequences", "target_tokens", "tool_receipts")},
              "longest_sequence": max(row["longest_sequence"] for row in results)}
    write_json(root / "shard-report.json", report)
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    capture = commands.add_parser("capture")
    for option in ("input", "source-id", "output"):
        capture.add_argument("--" + option, required=True)
    replay = commands.add_parser("replay")
    for option in ("seed", "endpoint", "model", "output"):
        replay.add_argument("--" + option, required=True)
    replay.add_argument("--max-tokens", type=int, default=768)
    build = commands.add_parser("assemble")
    for option in ("input", "tokenizer", "output"):
        build.add_argument("--" + option, required=True)
    build.add_argument("--max-length", type=int, required=True)
    verify = commands.add_parser("audit")
    for option in ("dataset", "tokenizer", "output"):
        verify.add_argument("--" + option, required=True)
    verify.add_argument("--max-length", type=int, required=True)
    batch = commands.add_parser("assemble-shards")
    for option in ("input", "tokenizer", "output"):
        batch.add_argument("--" + option, required=True)
    batch.add_argument("--max-length", type=int, required=True)
    batch.add_argument("--workers", type=int, default=2)
    args = parser.parse_args()
    if args.command == "capture":
        output = Path(args.output)
        if output.exists():
            parser.error("Use a new capture output file")
        captured = capture_request(read_json(args.input), source_id=args.source_id)
        write_json(output, captured)
        print(json.dumps({"messages": len(captured["messages"]), "receipts": len(captured["evidence"])}))
    elif args.command == "replay":
        run = rollout(read_json(args.seed), args.endpoint, args.model, args.output, max_tokens=args.max_tokens)
        print(json.dumps({"status": run["status"], "model": run["model"], "responses": len(run["responses"])}))
    elif args.command == "assemble-shards":
        report = assemble_shards(args.input, args.tokenizer, args.max_length, args.output, args.workers)
        print(json.dumps(report["totals"]))
    else:
        from transformers import AutoTokenizer

        tokenizer = AutoTokenizer.from_pretrained(args.tokenizer, local_files_only=True, trust_remote_code=False)
        if args.command == "audit":
            report = audit(args.dataset, tokenizer, args.max_length)
            write_json(args.output, report)
        else:
            report = assemble(read_json(args.input), tokenizer, args.max_length, args.output)
        print(json.dumps(report))


if __name__ == "__main__":
    main()
