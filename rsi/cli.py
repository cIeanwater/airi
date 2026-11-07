"""Local commands. Mutation, model calls, and training require --execute."""

import argparse
import json
import time
from pathlib import Path

from .common import digest, read_json, read_jsonl, write_json, write_jsonl


def rows(path):
    value = read_jsonl(path) if str(path).endswith(".jsonl") else read_json(path)
    return value if isinstance(value, list) else [value]


def _output(args, value):
    if getattr(args, "result", None):
        write_json(args.result, value)
    print(json.dumps(value, ensure_ascii=False, allow_nan=False))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("preflight")
    sub = commands.add_parser("demo")
    sub.add_argument("--output", required=True)
    for name in ("audit", "import"):
        sub = commands.add_parser(name)
        sub.add_argument("--records", required=True)
        sub.add_argument("--root", default="rsi/private")
        sub.add_argument("--kind", choices=["dialogue", "reflection", "initiative"], default="dialogue")
        sub.add_argument("--scope")
        if name == "import":
            sub.add_argument("--execute", action="store_true")
    sub = commands.add_parser("capture")
    sub.add_argument("--episode", required=True)
    sub.add_argument("--root", default="rsi/private")
    sub.add_argument("--execute", action="store_true")
    sub = commands.add_parser("dataset")
    for name in ("root", "evidence", "mix", "output"):
        sub.add_argument("--" + name, required=True)
    sub.add_argument("--character-budget", type=int, required=True)
    sub.add_argument("--execute", action="store_true")
    sub = commands.add_parser("diagnose")
    sub.add_argument("--gaps", required=True)
    sub.add_argument("--usable-samples", type=int, default=0)
    for name in ("resources-ready", "cooldown-ready", "budget-ready"):
        sub.add_argument("--" + name, action="store_true")
    sub = commands.add_parser("evaluate")
    for name in ("suite", "baseline", "candidate", "output"):
        sub.add_argument("--" + name, required=True)
    for name in ("policy", "judgments", "evidence", "training-episodes"):
        sub.add_argument("--" + name)
    sub.add_argument("--demo", action="store_true")
    sub = commands.add_parser("train")
    for name in ("config", "dataset", "output"):
        sub.add_argument("--" + name, required=True)
    sub.add_argument("--parent")
    sub.add_argument("--role", choices=["actor", "proposer"], default="actor")
    sub.add_argument("--result")
    sub.add_argument("--execute", action="store_true")
    sub = commands.add_parser("infer")
    for name in ("config", "suite", "output"):
        sub.add_argument("--" + name, required=True)
    sub.add_argument("--adapter")
    sub.add_argument("--actor-version")
    sub.add_argument("--execute", action="store_true")
    sub = commands.add_parser("merge")
    for name in ("config", "adapter", "output"):
        sub.add_argument("--" + name, required=True)
    sub.add_argument("--execute", action="store_true")
    sub = commands.add_parser("cycle")
    for name in ("config", "plan", "dataset", "suite", "output", "registry"):
        sub.add_argument("--" + name, required=True)
    sub.add_argument("--execute", action="store_true")
    sub = commands.add_parser("registry")
    sub.add_argument("action", choices=["show", "initialize", "rollback"])
    sub.add_argument("--root", required=True)
    sub.add_argument("--role", choices=["actor", "proposer"], default="actor")
    sub.add_argument("--config")
    sub.add_argument("--adapter")
    sub.add_argument("--execute", action="store_true")
    sub = commands.add_parser("release-actor")
    sub.add_argument("--run", required=True)
    sub.add_argument("--registry", required=True)
    sub.add_argument("--execute", action="store_true")
    sub = commands.add_parser("propose")
    for name in ("connection", "context", "output"):
        sub.add_argument("--" + name, required=True)
    sub.add_argument("--execute", action="store_true")
    sub = commands.add_parser("meta-build")
    for name in ("archive", "actor-version", "output"):
        sub.add_argument("--" + name, required=True)
    sub.add_argument("--execute", action="store_true")
    sub = commands.add_parser("meta-evaluate")
    for name in ("trials", "actor-version", "budget-sha256", "output"):
        sub.add_argument("--" + name, required=True)
    sub.add_argument("--training-task-ids")
    sub = commands.add_parser("meta-promote")
    for name in ("trials", "actor-version", "budget-sha256", "training-task-ids", "adapter", "registry"):
        sub.add_argument("--" + name, required=True)
    sub.add_argument("--execute", action="store_true")
    sub = commands.add_parser("watch")
    sub.add_argument("--config", required=True)
    sub.add_argument("--once", action="store_true")
    sub.add_argument("--interval-seconds", type=int, default=30)
    sub.add_argument("--execute", action="store_true")
    sub = commands.add_parser("goals")
    sub.add_argument("action", choices=["list", "select", "tick", "interest", "create", "revise", "transition", "reserve", "settle"])
    sub.add_argument("--root", required=True)
    sub.add_argument("--scope", required=True)
    sub.add_argument("--input")
    sub.add_argument("--command-id")
    sub.add_argument("--execute", action="store_true")
    sub.add_argument("--allow-artifact-writes", action="store_true")
    args = parser.parse_args(argv)
    if args.command == "preflight":
        from .training import preflight
        value = preflight()
    elif args.command == "demo":
        from .demo import run_demo
        value = run_demo(args.output)
    elif args.command in {"audit", "import"}:
        from .episodes import EpisodeStore, import_request
        files = sorted(Path(args.records).glob("*.json"))
        store = EpisodeStore(args.root)
        counts, issues = {}, {}
        for path in files:
            try:
                record = read_json(path)
                episode = import_request(record, scope=read_json(args.scope) if args.scope else None, kind=args.kind, source_id="request:" + digest(record))
                if args.command == "import" and args.execute:
                    store.append(episode)
                counts[episode["kind"]] = counts.get(episode["kind"], 0) + 1
                for issue in episode["context"].get("issues", []):
                    issues[issue] = issues.get(issue, 0) + 1
            except ValueError as error:
                issues[str(error)] = issues.get(str(error), 0) + 1
        value = {"files": len(files), "normalized": counts, "issues": issues,
                 "saved": args.command == "import" and args.execute}
    elif args.command == "capture":
        from .episodes import EpisodeStore, validate_episode
        episode = validate_episode(read_json(args.episode))
        value = {"execute": args.execute, "episode_id": episode["id"]}
        if args.execute:
            value["appended"] = EpisodeStore(args.root).append(episode)
    elif args.command == "dataset":
        from .episodes import EpisodeStore
        from .dataset import build_dataset
        value = {"execute": args.execute, "output": args.output}
        if args.execute:
            value = build_dataset(EpisodeStore(args.root).list(), args.output, verifications=read_json(args.evidence),
                                  mix=read_json(args.mix), character_budget=args.character_budget)
    elif args.command == "diagnose":
        from .evaluation import diagnose
        value = diagnose(rows(args.gaps), args.usable_samples, args.resources_ready, args.cooldown_ready, args.budget_ready)
    elif args.command == "evaluate":
        from .evaluation import evaluate, gate
        value = evaluate(rows(args.suite), rows(args.baseline), rows(args.candidate),
                         judgments=rows(args.judgments) if args.judgments else None,
                         evidence=rows(args.evidence) if args.evidence else None,
                         policy=read_json(args.policy) if args.policy else None,
                         training_episodes=rows(args.training_episodes) if args.training_episodes else None, demo=args.demo)
        write_json(args.output, value)
        value = {"output": args.output, "decision": gate(value)}
    elif args.command == "train":
        from .training import train
        value = train(read_json(args.config), args.dataset, args.output, parent=args.parent, role=args.role, execute=args.execute)
    elif args.command == "infer":
        from .training import infer
        value = infer(read_json(args.config), rows(args.suite), args.output, adapter=args.adapter, actor_version=args.actor_version, execute=args.execute)
    elif args.command == "merge":
        from .training import merge
        value = merge(read_json(args.config), args.adapter, args.output, execute=args.execute)
    elif args.command == "registry":
        from .controller import Registry
        registry = Registry(args.root)
        if args.action == "show":
            value = registry.state()
        elif not args.execute:
            value = {"execute": False, "action": args.action, "role": args.role}
        elif args.action == "rollback":
            value = registry.rollback(args.role)
        else:
            from .common import tree_hash
            from .training import model_identity
            identity = model_identity(read_json(args.config)["model"]) if not args.adapter else None
            value = registry.initialize(args.role, tree_hash(args.adapter) if args.adapter else digest(identity), args.adapter)
    elif args.command == "cycle":
        from .controller import Registry, run_cycle
        value = run_cycle(read_json(args.config), read_json(args.plan), args.dataset, rows(args.suite), args.output,
                          Registry(args.registry), execute=args.execute)
    elif args.command == "release-actor":
        from .controller import Registry, release_actor
        value = release_actor(args.run, Registry(args.registry)) if args.execute else {"execute": False, "run": args.run}
    elif args.command == "propose":
        from .client import propose
        value = propose(read_json(args.connection), read_json(args.context), execute=args.execute)
        if args.execute:
            write_json(args.output, value)
    elif args.command == "meta-build":
        from .strategies import StrategyArchive, build_meta_dataset
        value = build_meta_dataset(StrategyArchive(args.archive).records(), args.actor_version)
        if args.execute and value.get("ready_for_training"):
            from .dataset import split_episodes
            from .common import file_hash
            out = Path(args.output)
            if out.exists():
                raise ValueError("Use a new meta dataset directory")
            splits = split_episodes(value["episodes"])
            for split, episodes in splits.items():
                write_jsonl(out / (split + ".jsonl"), episodes)
            write_json(out / "manifest.json", {"schema_version": 1, "kind": "meta", "actor_version": args.actor_version,
                                               "measurement_sha256": value["measurement_sha256"],
                                               "files": {s: file_hash(out / (s + ".jsonl")) for s in splits}})
        value = {k: v for k, v in value.items() if k != "episodes"}
    elif args.command == "meta-evaluate":
        from .strategies import compare_meta
        value = compare_meta(rows(args.trials), args.actor_version, args.budget_sha256,
                             training_task_ids=read_json(args.training_task_ids) if args.training_task_ids else None)
        write_json(args.output, value)
    elif args.command == "meta-promote":
        from .common import tree_hash
        from .controller import Registry
        value = {"execute": args.execute, "role": "proposer"}
        if args.execute:
            registry = Registry(args.registry)
            value = registry.promote_proposer({"artifact": args.adapter, "version": tree_hash(args.adapter)}, rows(args.trials),
                                              args.actor_version, args.budget_sha256, read_json(args.training_task_ids),
                                              expected=registry.state()["proposer"])
    elif args.command == "watch":
        from .controller import poll
        if not 1 <= args.interval_seconds <= 60:
            parser.error("Watch intervals range from one to sixty seconds")
        while True:
            value = poll(read_json(args.config), execute=args.execute)
            if args.once or not args.execute:
                break
            _output(args, value)
            time.sleep(args.interval_seconds)
    elif args.command == "goals":
        from .goals import GoalStore
        store = GoalStore(args.root, read_json(args.scope))
        if args.action == "list":
            value = {"goals": store.list_goals(), "interests": store.list_interests()}
        elif args.action == "select":
            value = store.select()
        elif args.action == "tick":
            from .runtime import ArtifactWriter, GoalRunner
            adapters = {"write_artifact": ArtifactWriter(store.artifacts)} if args.allow_artifact_writes else {}
            value = GoalRunner(store, adapters).tick(execute=args.execute)
        elif not args.execute:
            value = {"execute": False, "action": args.action}
        else:
            if not args.input or not args.command_id:
                parser.error("Goal mutations need --input and --command-id")
            data = read_json(args.input)
            if args.action == "interest":
                value = store.create_interest(data, args.command_id)
            elif args.action == "create":
                value = store.create_goal(data, args.command_id)
            elif args.action == "revise":
                value = store.revise(data["goal_id"], data["changes"], data["expected_version"], args.command_id)
            elif args.action == "transition":
                value = store.transition(data["goal_id"], data["status"], data["reason"], data["expected_version"], args.command_id)
            elif args.action == "reserve":
                value = store.reserve(data["goal_id"], data["expected_version"], command_id=args.command_id)
            else:
                value = store.settle(data["reservation_id"], data["outcome"], args.command_id)
    _output(args, value)
