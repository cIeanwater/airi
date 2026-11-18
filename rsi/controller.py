"""Bounded experiments with immutable inputs and leased champion updates."""

import copy
import datetime
import shutil
import subprocess
import sys
import time
from pathlib import Path

from .common import digest, exclusive, read_json, read_jsonl, tree_hash, utc_now, write_json


class Registry:
    def __init__(self, root):
        self.root = Path(root)
        self.path = self.root / "registry.json"

    def state(self):
        return read_json(self.path) if self.path.exists() else {"schema_version": 1, "actor": None, "proposer": None, "history": []}

    def initialize(self, role, version, artifact=None):
        if role not in {"actor", "proposer"}:
            raise ValueError("Unknown model role")
        if artifact and tree_hash(artifact) != version:
            raise ValueError("Initial artifact hash mismatch")
        with exclusive(self.root / "registry.lock"):
            state = self.state()
            if state[role] is not None:
                raise ValueError("Role already has a registered version")
            state[role] = {"version": version, "artifact": str(Path(artifact).resolve()) if artifact else None}
            write_json(self.path, state)
        return state

    def promote(self, role, candidate, report, *, expected):
        from .evaluation import gate

        decision = gate(report)
        if not decision["promote"]:
            return decision
        if role != "actor":
            raise ValueError("Proposer promotion requires independent adaptation trials")
        if tree_hash(candidate["artifact"]) != candidate["version"] or report["candidate_version"] != candidate["version"]:
            raise ValueError("Evaluation does not match the candidate artifact")
        if report["baseline_version"] != expected["version"]:
            raise ValueError("Evaluation does not match the champion")
        with exclusive(self.root / "registry.lock"):
            state = self.state()
            if state[role] != expected:
                raise ValueError("Champion changed. Evaluate again")
            state["history"].append({"role": role, "previous": expected, "next": candidate, "report_sha256": digest(report), "at": utc_now()})
            state[role] = {"version": candidate["version"], "artifact": str(Path(candidate["artifact"]).resolve())}
            write_json(self.path, state)
        return {"promote": True, "version": candidate["version"]}

    def rollback(self, role):
        with exclusive(self.root / "registry.lock"):
            state = self.state()
            history = next((r for r in reversed(state["history"]) if r["role"] == role and r.get("active", True)), None)
            if not history:
                raise ValueError("No prior version exists")
            previous = history["previous"]
            if previous.get("artifact") and tree_hash(previous["artifact"]) != previous["version"]:
                raise ValueError("Prior artifact changed")
            history["active"] = False
            state[role] = previous
            write_json(self.path, state)
        return state

    def promote_proposer(self, candidate, trials, actor_version, budget_sha256, training_task_ids, *, expected):
        from .strategies import compare_meta

        if tree_hash(candidate["artifact"]) != candidate["version"]:
            raise ValueError("Proposer artifact changed")
        if any(t.get("baseline_proposer_version") != expected["version"] or t.get("candidate_proposer_version") != candidate["version"] for t in trials):
            raise ValueError("Meta trials target different proposer artifacts")
        report = compare_meta(trials, actor_version, budget_sha256, training_task_ids=training_task_ids)
        if not report["promote"]:
            return report
        with exclusive(self.root / "registry.lock"):
            state = self.state()
            if state["proposer"] != expected or state["actor"]["version"] != actor_version:
                raise ValueError("Actor or proposer changed during adaptation trials")
            state["history"].append({"role": "proposer", "previous": expected, "next": candidate,
                                     "report_sha256": digest(report), "at": utc_now()})
            state["proposer"] = {"artifact": str(Path(candidate["artifact"]).resolve()), "version": candidate["version"]}
            write_json(self.path, state)
        return report


class HFBackend:
    """Child processes release GPU allocations between training and evaluation."""

    def train(self, config, dataset, output, parent, role="actor"):
        output = Path(output)
        output.parent.mkdir(parents=True, exist_ok=True)
        configuration = output.parent / (output.name + "-config.json")
        result = output.parent / (output.name + "-result.json")
        write_json(configuration, config)
        command = [sys.executable, "-m", "rsi", "train", "--config", str(configuration), "--dataset", str(dataset),
                   "--output", str(output), "--role", role, "--result", str(result), "--execute"]
        if parent:
            command.extend(["--parent", str(parent)])
        with (output.parent / (output.name + ".log")).open("w", encoding="utf-8") as log:
            subprocess.run(command, check=True, stdout=log, stderr=subprocess.STDOUT)
        value = read_json(result)
        return {"artifact": value["artifact"], "version": value["artifact_sha256"],
                "training_seconds": value["training_seconds"], "tokens": value["target_tokens"], "lineage": value["lineage"]}

    def predict(self, config, cases, output, actor):
        output = Path(output)
        configuration = output.with_suffix(".config.json")
        suite = output.with_suffix(".suite.json")
        write_json(configuration, config)
        write_json(suite, cases)
        command = [sys.executable, "-m", "rsi", "infer", "--config", str(configuration), "--suite", str(suite),
                   "--output", str(output), "--actor-version", actor["version"], "--execute"]
        if actor.get("artifact"):
            command.extend(["--adapter", actor["artifact"]])
        with output.with_suffix(".log").open("w", encoding="utf-8") as log:
            subprocess.run(command, check=True, stdout=log, stderr=subprocess.STDOUT)
        return {"predictions": read_jsonl(output), "evidence": read_jsonl(output.with_suffix(".evidence.jsonl"))}


def run_cycle(settings, plan, dataset, cases, output, registry, *, execute=False, backend=None):
    from .evaluation import check_suite, evaluate, gate
    from .strategies import StrategyArchive, validate_plan
    from .training import load_dataset, validate_config

    validate_config(settings["training"])
    plan = validate_plan(plan, fixed_rank=settings["training"]["rank"])
    candidate_config = {**settings["training"], **plan["training"]}
    validate_config(candidate_config)
    if not execute:
        return {"execute": False, "steps": ["bind-inputs", "baseline", "train-candidate", "evaluate", "archive", "publish-if-supported"],
                "plan_id": plan["id"], "role": "actor"}
    out = Path(output)
    if out.exists():
        raise ValueError("Use a new cycle directory")
    champion = registry.state()["actor"]
    if not champion or plan["actor_version"] != champion["version"]:
        raise ValueError("Plan targets a different registered actor")
    if champion.get("artifact") and tree_hash(champion["artifact"]) != champion["version"]:
        raise ValueError("Champion artifact changed")
    rows, dataset_sha = load_dataset(dataset)
    check_suite(cases, training_episodes=rows["train"] + rows["validation"])
    if settings.get("required_dataset_sha256") and settings["required_dataset_sha256"] != dataset_sha:
        raise ValueError("Dataset does not match the selected learning plan")
    if settings.get("data_instruction") != plan["data_instruction"]:
        raise ValueError("Plan materializer does not support this data instruction")
    if read_json(Path(dataset) / "manifest.json").get("mix") != plan["mix"]:
        raise ValueError("Dataset mix differs from the learning plan")
    allowed_sources = set(plan["selection"]["source_ids"])
    actual_sources = {s for row in rows["train"] for s in row.get("source_ids", []) if row.get("context", {}).get("dataset_kind") != "base"}
    if not actual_sources <= allowed_sources:
        raise ValueError("Dataset includes sources outside the plan")
    if any(row["kind"] not in plan["selection"]["kinds"] for row in rows["train"] if row.get("context", {}).get("dataset_kind") != "base"):
        raise ValueError("Dataset includes kinds outside the plan")
    scope = settings.get("actor_scope")
    if not isinstance(scope, dict) or set(scope) != {"user_id", "character_id"} or not all(isinstance(v, str) and v for v in scope.values()):
        raise ValueError("Configure one fixed actor scope before training")
    if any(row["scope"] != scope for split in rows.values() for row in split if row.get("context", {}).get("dataset_kind") != "base"):
        raise ValueError("Dataset contains experiences from a different actor scope")
    candidate_config["scope"] = scope
    archive = StrategyArchive(settings["archive"])
    if any(record["plan"] == plan and record["dataset_sha256"] == dataset_sha and record["suite_sha256"] == digest(cases)
           for record in archive.records(champion["version"])):
        raise ValueError("This plan, dataset and suite were already measured for the current actor")
    executor = backend or HFBackend()
    out.mkdir(parents=True)
    frozen = out / "dataset"
    frozen.mkdir()
    for name in ("train.jsonl", "validation.jsonl", "test.jsonl", "manifest.json"):
        shutil.copyfile(Path(dataset) / name, frozen / name)
    if load_dataset(frozen)[1] != dataset_sha:
        raise ValueError("Dataset changed while taking its snapshot")
    write_json(out / "plan.json", plan)
    write_json(out / "baseline.json", champion)
    write_json(out / "suite.json", cases)
    write_json(out / "settings.json", settings)
    started = time.monotonic()
    with exclusive(registry.root / "cycle.lock"):
        today = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=8))).date().isoformat()
        attempts_file = registry.root / "attempts.json"
        attempts = read_json(attempts_file) if attempts_file.exists() else {}
        limit = settings.get("rounds_per_day", 1)
        if type(limit) is not int or not 1 <= limit <= 3:
            raise ValueError("Daily round budget must range from one to three")
        if attempts.get(today, 0) >= limit:
            raise ValueError("Daily experiment budget is exhausted")
        attempts[today] = attempts.get(today, 0) + 1
        write_json(attempts_file, attempts)
        baseline = executor.predict(settings["training"], cases, out / "baseline-predictions.jsonl", champion)
        config = candidate_config
        candidate = executor.train(config, frozen, out / "training", champion.get("artifact"))
        lineage = read_json(Path(candidate["artifact"]) / "lineage.json")
        expected_parent = tree_hash(champion["artifact"]) if champion.get("artifact") else None
        if lineage["dataset_sha256"] != dataset_sha or lineage["parent_sha256"] != expected_parent or lineage["role"] != "actor" or lineage.get("scope") != scope:
            raise ValueError("Trained artifact does not match the frozen dataset and parent")
        result = executor.predict(config, cases, out / "candidate-predictions.jsonl", candidate)
        judgments = []
        if settings.get("judge"):
            from .client import judge

            for predictions in (baseline["predictions"], result["predictions"]):
                judged_cases = [c for c in cases if c.get("rubric_sha256")]
                judgments.extend(judge(settings["judge"]["connection"], judged_cases, predictions,
                                       settings["judge"]["identity"], execute=True, max_records=settings.get("max_judgments", 100)))
        receipts = baseline.get("evidence", []) + result.get("evidence", [])
        write_json(out / "receipts.json", receipts)
        write_json(out / "judgments.json", judgments)
        report = evaluate(cases, baseline["predictions"], result["predictions"], judgments=judgments,
                          evidence=receipts,
                          policy=settings.get("evaluation"), training_episodes=rows["train"] + rows["validation"],
                          demo=bool(getattr(executor, "demonstration", False)))
        report["dataset_sha256"] = dataset_sha
        write_json(out / "evaluation.json", report)
        decision = gate(report)
        record = {"schema_version": 1, "id": "trial-" + digest([plan, dataset_sha, digest(cases)]), "actor_version": champion["version"], "task_id": plan["task_id"],
                  "plan": plan, "dataset_sha256": dataset_sha, "suite_sha256": digest(cases), "report": report,
                  "origin": settings.get("plan_origin", "model"), "adaptation_split": "development",
                  "cost": {"training_seconds": candidate.get("training_seconds", time.monotonic() - started), "tokens": candidate.get("tokens", 0)},
                  "context": plan["context"], "source_ids": plan["source_ids"]}
        archive.append(record)
        if decision["promote"] and settings.get("auto_publish", True):
            decision = registry.promote("actor", candidate, report, expected=champion)
        elif decision["promote"]:
            decision = {"promote": False, "eligible": True, "candidate": candidate, "reasons": ["cohort_actor_remains_frozen"]}
        write_json(out / "decision.json", decision)
    return {"decision": decision, "run": str(out), "report_sha256": digest(report)}


def release_actor(run, registry):
    from .evaluation import evaluate
    from .training import load_dataset

    root = Path(run)
    settings = read_json(root / "settings.json")
    dataset, dataset_sha = load_dataset(root / "dataset")
    report = evaluate(read_json(root / "suite.json"), read_jsonl(root / "baseline-predictions.jsonl"),
                      read_jsonl(root / "candidate-predictions.jsonl"), judgments=read_json(root / "judgments.json"),
                      evidence=read_json(root / "receipts.json"), policy=settings.get("evaluation"),
                      training_episodes=dataset["train"] + dataset["validation"])
    report["dataset_sha256"] = dataset_sha
    if digest(report) != digest(read_json(root / "evaluation.json")):
        raise ValueError("Cycle evidence or policy changed after evaluation")
    result = read_json(root / "training/run.json")
    return registry.promote("actor", {"artifact": result["artifact"], "version": result["artifact_sha256"]}, report,
                            expected=read_json(root / "baseline.json"))


def poll(settings, *, execute=False):
    """Inspect a normalized inbox. Real requests remain unmodified source records."""
    from .episodes import EpisodeStore
    from .evaluation import diagnose

    inbox = Path(settings["inbox"])
    files = sorted(inbox.glob("*.json")) if inbox.exists() else []
    if not execute:
        return {"execute": False, "inbox_files": len(files), "next": "capture-and-diagnose"}
    store = EpisodeStore(settings["episodes"])
    captured, quarantined = 0, []
    for path in files:
        try:
            captured += bool(store.append(read_json(path)))
        except ValueError as error:
            quarantined.append({"record": path.name, "reason": str(error)})
    gaps = read_json(settings["gaps"]) if settings.get("gaps") and Path(settings["gaps"]).exists() else []
    proofs = read_json(settings["verifications"]) if settings.get("verifications") else {}
    from .dataset import verify_episode
    episodes = store.list()
    scope = settings.get("actor_scope")
    if not isinstance(scope, dict) or set(scope) != {"user_id", "character_id"}:
        raise ValueError("Watcher requires one fixed actor scope")
    episodes = [e for e in episodes if e["scope"] == scope or e.get("context", {}).get("dataset_kind") == "base"]
    usable = sum(verify_episode(e, proofs.get(e["id"]))["eligible"] for e in episodes)
    automation = settings.get("automation", {})
    budget_ready = settings.get("budget_ready", False)
    if automation.get("enabled"):
        budget_root = Path(automation["registry"])
        attempts = read_json(budget_root / "attempts.json") if (budget_root / "attempts.json").exists() else {}
        today = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=8))).date().isoformat()
        budget_ready = budget_ready and attempts.get(today, 0) < read_json(automation["cycle_config"]).get("rounds_per_day", 1)
    diagnosis = diagnose(gaps, usable_samples=usable, resources_ready=settings.get("resources_ready", False),
                         cooldown_ready=settings.get("cooldown_ready", False), budget_ready=budget_ready)
    result = {"captured": captured, "quarantined": quarantined, "usable_samples": usable, "diagnosis": diagnosis}
    if settings.get("goals", {}).get("enabled"):
        from .goals import GoalStore
        from .runtime import ArtifactWriter, GoalRunner

        goal_settings = settings["goals"]
        goal_store = GoalStore(goal_settings["root"], goal_settings["scope"])
        adapters = {"write_artifact": ArtifactWriter(goal_store.artifacts)} if goal_settings.get("allow_artifact_writes") else {}
        result["goal"] = GoalRunner(goal_store, adapters).tick(execute=True)
    if diagnosis["action"] == "train" and automation.get("enabled"):
        from .client import propose
        from .dataset import build_dataset
        from .strategies import StrategyArchive, validate_plan

        registry = Registry(automation["registry"])
        actor = registry.state()["actor"]
        if not actor:
            raise ValueError("Initialize the actor before starting automatic experiments")
        cycle_settings = read_json(automation["cycle_config"])
        if cycle_settings.get("actor_scope") != scope:
            raise ValueError("Watcher and actor cycle scopes differ")
        if automation.get("connection") or automation.get("use_registered_proposer"):
            proposer = registry.state()["proposer"]
            if not proposer:
                raise ValueError("Initialize a proposer before requesting learned self-edits")
            context = {"actor_version": actor["version"], "diagnosis": diagnosis,
                       "actor_scope": scope,
                       "proposer_version": proposer["version"],
                       "source_ids": sorted({s for e in episodes for s in e["source_ids"]}),
                       "inventory": {k: sum(e["kind"] == k for e in episodes) for k in ("dialogue", "reflection", "initiative")},
                       "plan_format": read_json(automation["plan_template"]),
                       "strategies": StrategyArchive(cycle_settings["archive"]).select(actor["version"])}
            if automation.get("connection"):
                connection = read_json(automation["connection"])
                if connection["model"] != proposer["version"]:
                    raise ValueError("Remote proposer must serve the pinned registered artifact ID")
                plan = propose(connection, context, execute=True)
            else:
                proposal_config = {**cycle_settings["training"], "max_length": 4096}
                destination = registry.root / "proposals" / (utc_now().replace(":", "-") + ".jsonl")
                destination.parent.mkdir(parents=True, exist_ok=True)
                messages = [{"role": "system", "content": "根据聚合缺口、策略档案和plan_format，提出一次self-edit。只输出JSON，不修改模型、评测器或预算。"},
                            {"role": "user", "content": __import__("json").dumps(context, ensure_ascii=False)}]
                generated = HFBackend().predict(proposal_config, [{"id": "proposal", "messages": messages, "max_new_tokens": 2048}], destination, proposer)
                output = generated["predictions"][0]
                if not output["complete"]:
                    raise ValueError("Registered proposer did not complete its learning plan")
                plan = validate_plan(__import__("json").loads(output["text"]))
            plan["context"]["proposer_version"] = proposer["version"]
            cycle_settings["plan_origin"] = "model"
        else:
            plan = validate_plan(read_json(automation["plan_template"]))
            cycle_settings["plan_origin"] = "manual"
        if plan["actor_version"] != actor["version"]:
            raise ValueError("Automatic plan targets a stale actor version")
        selected = [e for e in episodes if e.get("context", {}).get("dataset_kind") == "base" or
                    e["kind"] in plan["selection"]["kinds"] and set(e["source_ids"]) <= set(plan["selection"]["source_ids"])]
        run = Path(automation["runs"]) / ("cycle-" + utc_now().replace(":", "-").replace("+", "-"))
        dataset = run / "prepared"
        build_dataset(selected, dataset, verifications=proofs, mix=plan["mix"], character_budget=automation["character_budget"])
        result["cycle"] = run_cycle(cycle_settings, plan, dataset, read_json(automation["suite"]), run / "experiment", registry, execute=True)
    return result
