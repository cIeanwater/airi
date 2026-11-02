"""Bounded experiment proposals, sequential cycles, and reversible champion registry."""

import argparse
import hashlib
import json
import math
import os
import subprocess
import sys
from pathlib import Path

from evaluation import paired_report, promotion_gate, suite_check
from pipeline import KINDS, assemble, digest, lines, read, write, write_lines
from training import load_dataset, validate_config


def proposal_check(proposal):
    if set(proposal) != {"training", "mix", "character_budget", "rationale"}:
        raise ValueError("Proposal has unknown or missing fields")
    validate_config(proposal["training"])
    allowed = {"model", "device", "qlora", "rank", "max_length", "learning_rate", "epochs", "gradient_accumulation", "seed", "reasoning_format", "target_modules"}
    if set(proposal["training"]) - allowed:
        raise ValueError("Training proposal contains unsupported settings")
    mix = proposal["mix"]
    if set(mix) != KINDS or any(not isinstance(v, (int, float)) or not math.isfinite(v) or v < 0 for v in mix.values()) or mix["base"] < 0.3 or abs(sum(mix.values()) - 1) > 1e-9:
        raise ValueError("Invalid proposal mix. Base replay must remain at least 30%")
    if not isinstance(proposal["character_budget"], int) or not 1000 <= proposal["character_budget"] <= 10000000:
        raise ValueError("Proposal exceeds the data budget")


def artifact_hash(path):
    root = Path(path)
    if not root.is_dir():
        raise ValueError("Adapter directory is missing")
    entries = []
    for file in sorted(root.iterdir()):
        if file.is_file():
            sha = hashlib.sha256()
            with file.open("rb") as stream:
                for block in iter(lambda: stream.read(1024 * 1024), b""):
                    sha.update(block)
            entries.append([file.name, sha.hexdigest()])
    if not any(name == "adapter_model.safetensors" for name, _ in entries):
        raise ValueError("Adapter weights are missing")
    return digest(entries)


def publish_registry(path, value):
    path = Path(path)
    temporary = path.with_suffix(".tmp")
    write(temporary, value)
    os.replace(temporary, path)


def promote(registry, adapter, report, expected_champion):
    gate = promotion_gate(report)
    if not gate["promote"]:
        return gate
    if report.get("candidate_artifact_sha256") != artifact_hash(adapter) or report.get("baseline_champion_sha256") != digest(expected_champion):
        raise ValueError("Evaluation is not bound to these candidate and champion artifacts")
    path = Path(registry)
    lock = path.with_suffix(".lock")
    path.parent.mkdir(parents=True, exist_ok=True)
    # Exclusive file creation prevents concurrent rounds from replacing the same champion.
    with lock.open("x"):
        pass
    try:
        state = read(path) if path.exists() else {"champion": None, "history": []}
        if state["champion"] != expected_champion:
            raise ValueError("Champion changed during evaluation. Evaluate again")
        artifact = {"path": str(Path(adapter).resolve()), "sha256": artifact_hash(adapter), "report_sha256": digest(report)}
        state["history"].append(state["champion"])
        state["champion"] = artifact
        publish_registry(path, state)
    finally:
        lock.unlink()
    return {"promote": True, "champion": artifact}


def rollback(registry):
    path = Path(registry)
    lock = path.with_suffix(".lock")
    with lock.open("x"):
        pass
    try:
        state = read(path)
        if not state["history"]:
            raise ValueError("No prior champion exists")
        previous = state["history"][-1]
        if previous and artifact_hash(previous["path"]) != previous["sha256"]:
            raise ValueError("Prior champion artifact changed")
        state["champion"] = state["history"].pop()
        publish_registry(path, state)
    finally:
        lock.unlink()
    return state


def cycle(proposal, candidates, reviews, suite, output, registry, execute=False, judgments=None):
    proposal_check(proposal)
    out = Path(output)
    if out.exists():
        raise ValueError("Use a new cycle directory")
    if not execute:
        return {"execute": False, "proposal": proposal, "steps": ["build", "train", "paired-infer", "evaluate", "gate", "register"],
                "note": "One bounded round. No model, API, or deployment changes"}
    state = read(registry) if Path(registry).exists() else {"champion": None}
    champion = state["champion"]
    if champion and artifact_hash(champion["path"]) != champion["sha256"]:
        raise ValueError("Champion artifact changed")
    assemble(candidates, reviews, out / "dataset", proposal["mix"], proposal["character_budget"])
    dataset = load_dataset(out / "dataset")
    cases = list(lines(suite))
    suite_check(cases, dataset["train"] + dataset["validation"])
    write(out / "proposal.json", proposal)
    write(out / "baseline-champion.json", champion)
    write_lines(out / "suite.jsonl", cases)
    write(out / "training-config.json", proposal["training"])
    # Every candidate starts from the same original base with replay. Adapter deltas never accumulate accidentally.
    script = Path(__file__).with_name("training.py")
    subprocess.run([sys.executable, str(script), "train", "--config", str(out / "training-config.json"),
                    "--dataset", str(out / "dataset"), "--output", str(out / "training"), "--execute"], check=True)
    for label, adapter in (("baseline", champion["path"] if champion else None), ("candidate", str(out / "training/adapter"))):
        command = [sys.executable, str(script), "infer", "--model", proposal["training"]["model"], "--suite", str(out / "suite.jsonl"),
                   "--output", str(out / (label + ".jsonl")), "--device", str(proposal["training"].get("device", 0)), "--execute"]
        if proposal["training"]["qlora"]:
            command.append("--qlora")
        if adapter:
            command.extend(["--adapter", adapter])
        # A child process releases all inference allocations before the next model loads.
        subprocess.run(command, check=True)
    report = paired_report(cases, list(lines(out / "baseline.jsonl")), list(lines(out / "candidate.jsonl")), judgments)
    report.update({"candidate_artifact_sha256": artifact_hash(out / "training/adapter"), "baseline_champion_sha256": digest(champion)})
    write(out / "evaluation.json", report)
    gate = promote(registry, out / "training/adapter", report, champion)
    write(out / "decision.json", gate)
    return gate


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    sub = commands.add_parser("cycle")
    for name in ("proposal", "candidates", "reviews", "suite", "output", "registry"):
        sub.add_argument("--" + name, required=True)
    sub.add_argument("--judgments")
    sub.add_argument("--execute", action="store_true")
    sub = commands.add_parser("evaluate")
    for name in ("suite", "baseline", "candidate", "dataset", "output"):
        sub.add_argument("--" + name, required=True)
    sub.add_argument("--judgments")
    sub.add_argument("--adapter", required=True)
    sub.add_argument("--baseline-champion", required=True)
    sub = commands.add_parser("promote")
    for name in ("cycle", "registry"):
        sub.add_argument("--" + name, required=True)
    sub = commands.add_parser("rollback")
    sub.add_argument("--registry", required=True)
    args = parser.parse_args()
    if args.command == "cycle":
        result = cycle(read(args.proposal), args.candidates, args.reviews, args.suite, args.output, args.registry, args.execute,
                       list(lines(args.judgments)) if args.judgments else None)
    elif args.command == "evaluate":
        dataset = load_dataset(args.dataset)
        cases = list(lines(args.suite))
        suite_check(cases, dataset["train"] + dataset["validation"])
        result = paired_report(cases, list(lines(args.baseline)), list(lines(args.candidate)),
                               list(lines(args.judgments)) if args.judgments else None)
        result.update({"candidate_artifact_sha256": artifact_hash(args.adapter), "baseline_champion_sha256": digest(read(args.baseline_champion))})
        write(args.output, result)
    elif args.command == "promote":
        # Evaluation remains separate from registry mutation. The registry points to recoverable artifacts only.
        root = Path(args.cycle)
        result = promote(args.registry, root / "training/adapter", read(root / "evaluation.json"), read(root / "baseline-champion.json"))
    else:
        result = rollback(args.registry)
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
