"""Replay tool fixtures with a local model and compile refined trajectories."""

import argparse
import json
from pathlib import Path
from urllib.parse import urlparse
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

from .cleaning import _hard_checks
from .common import digest, file_hash, read_json, write_json, write_jsonl
from .finetune import CHAT_SFT_KIND
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


def assemble(specs, tokenizer, max_length, output):
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
                       "targets": [{"tokens": len(s["input_ids"]),
                                    "loss_tokens": sum(label != -100 for label in s["labels"])}
                                   for s in encoded]})
        rows.append(row)
    out.mkdir(parents=True, exist_ok=False)
    files = {}
    for split in ("train", "validation", "test"):
        path = out / (split + ".jsonl")
        write_jsonl(path, rows if split == "train" else [])
        files[split] = file_hash(path)
    write_json(out / "manifest.json", {"kind": CHAT_SFT_KIND, "schema_version": 1, "scope": scope,
                                      "records": records, "files": files,
                                      "readiness": {"ready": False, "reasons": ["pilot_without_holdout"]}})
    write_jsonl(out / "provenance.jsonl", proofs)
    report = {"max_length": max_length, "trajectories": checks}
    write_json(out / "report.json", report)
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
    else:
        from transformers import AutoTokenizer

        tokenizer = AutoTokenizer.from_pretrained(args.tokenizer, local_files_only=True, trust_remote_code=False)
        report = assemble(read_json(args.input), tokenizer, args.max_length, args.output)
        print(json.dumps(report))


if __name__ == "__main__":
    main()
