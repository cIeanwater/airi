"""Bounded self-edits, version-bound strategy archives, and proposer learning data."""

import copy
import json
import math
import re
from pathlib import Path

from .common import digest, exclusive, identifier, read_json, utc_now, write_json
from .evaluation import gate


PLAN_FIELDS = {"schema_version", "id", "actor_version", "task_id", "target", "source_ids", "data_instruction",
               "selection", "mix", "training", "expected_gain", "context"}
MIX_KINDS = {"base", "dialogue", "reflection", "initiative"}


def _identifier(value, field):
    if not isinstance(value, str) or not value.strip() or len(value) > 256:
        raise ValueError("Missing or invalid strategy field: " + field)
    return value


def _finite(value, field, minimum, maximum):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or not minimum <= value <= maximum:
        raise ValueError("Strategy value outside bounds: " + field)


def validate_plan(plan, fixed_rank=8):
    if set(plan) != PLAN_FIELDS or plan["schema_version"] != 1:
        raise ValueError("Unknown or missing self-edit fields")
    for key in ("id", "actor_version", "task_id", "target", "data_instruction", "expected_gain"):
        _identifier(plan[key], key)
    if not isinstance(plan["source_ids"], list) or not plan["source_ids"] or any(not isinstance(s, str) or not s for s in plan["source_ids"]):
        raise ValueError("Self-edits require explicit evidence source IDs")
    if not isinstance(plan["context"], dict):
        raise ValueError("Self-edit context must be an aggregate object")
    selection = plan["selection"]
    if set(selection) != {"kinds", "source_ids"} or not selection["kinds"] or set(selection["kinds"]) - {"dialogue", "reflection", "initiative"}:
        raise ValueError("Invalid self-edit data selection")
    if not isinstance(selection["source_ids"], list) or not selection["source_ids"] or not set(selection["source_ids"]) <= set(plan["source_ids"]):
        raise ValueError("Data selection must reference the declared evidence sources")
    mix = plan["mix"]
    if set(mix) != MIX_KINDS:
        raise ValueError("Self-edit mix needs base, dialogue, reflection, and initiative")
    for key, value in mix.items():
        _finite(value, "mix." + key, 0, 1)
    if not math.isclose(sum(mix.values()), 1, abs_tol=1e-9) or mix["base"] < 0.3:
        raise ValueError("Mixture must sum to one and retain at least 30% base replay")
    training = plan["training"]
    if set(training) != {"learning_rate", "max_steps", "max_length", "rank"}:
        raise ValueError("Self-edit contains unsupported training settings")
    _finite(training["learning_rate"], "learning_rate", 0.000001, 0.0003)
    for key, minimum, maximum in (("max_steps", 1, 1000), ("max_length", 128, 4096)):
        _finite(training[key], key, minimum, maximum)
        if not isinstance(training[key], int):
            raise ValueError("Training counts must be integers")
    if training["rank"] != fixed_rank:
        raise ValueError("Self-edit cannot change the fixed adapter rank")
    digest(plan)
    return copy.deepcopy(plan)


def _record(record):
    required = {"schema_version", "actor_version", "task_id", "plan", "dataset_sha256", "suite_sha256", "report",
                "origin", "cost", "adaptation_split", "context", "source_ids"}
    if not required <= set(record) or record["schema_version"] != 1:
        raise ValueError("Incomplete strategy measurement")
    plan = validate_plan(record["plan"])
    report = record["report"]
    if record["actor_version"] != plan["actor_version"] or record["task_id"] != plan["task_id"]:
        raise ValueError("Strategy task or actor version mismatch")
    if record["origin"] not in {"model", "manual", "no_update"} or record["adaptation_split"] not in {"development", "meta_test"}:
        raise ValueError("Unknown strategy origin or adaptation split")
    for key in ("dataset_sha256", "suite_sha256"):
        if not isinstance(record[key], str) or not re.fullmatch(r"[a-f0-9]{64}", record[key]):
            raise ValueError("Strategy measurements need content hashes")
    if report.get("schema_version") != 1 or report.get("baseline_version") != record["actor_version"] or report.get("suite_sha256") != record["suite_sha256"]:
        raise ValueError("Report is not bound to the measured actor and evaluation suite")
    if not report.get("candidate_version") or not report.get("candidate_predictions_sha256") or not report.get("baseline_predictions_sha256"):
        raise ValueError("Strategy report lacks measured prediction bindings")
    if report.get("dataset_sha256") != record["dataset_sha256"]:
        raise ValueError("Report is not bound to the training dataset")
    if not isinstance(record["context"], dict) or not isinstance(record["cost"], dict):
        raise ValueError("Invalid strategy context or measured cost")
    for key in ("training_seconds", "tokens"):
        _finite(record["cost"].get(key), key, 0, 1000000000)
    if not isinstance(record["source_ids"], list) or not record["source_ids"]:
        raise ValueError("Strategy measurements need source lineage")
    if not set(plan["source_ids"]) <= set(record["source_ids"]):
        raise ValueError("Strategy measurement omitted evidence lineage")
    decision = gate(report)
    result = copy.deepcopy(record)
    result["decision"] = decision
    result["status"] = "success" if decision["promote"] else "demo" if report.get("demo") else "failed"
    result["gain"] = (report.get("target") or report["overall"])["mean_gain"]
    result["measurement_sha256"] = digest({"actor_version": record["actor_version"], "dataset": record["dataset_sha256"],
                                           "suite": record["suite_sha256"], "report": report, "cost": record["cost"]})
    return result


class StrategyArchive:
    """Keep immutable, measured successes and failures without mixing actor versions."""

    def __init__(self, directory):
        self.directory = Path(directory)

    def append(self, record):
        value = _record(record)
        value.setdefault("id", digest({k: v for k, v in value.items() if k != "created_at"}))
        identifier(value["id"])
        path = self.directory / (value["id"] + ".json")
        with exclusive(self.directory / ".archive.lock"):
            if path.exists():
                previous = read_json(path)
                if {k: v for k, v in previous.items() if k != "created_at"} != {k: v for k, v in value.items() if k != "created_at"}:
                    raise ValueError("Strategy record ID already has different content")
                return previous
            value["created_at"] = utc_now()
            write_json(path, value)
        return value

    def records(self, actor_version=None):
        result = []
        for path in sorted(self.directory.glob("*.json")):
            value = read_json(path)
            checked = _record(value)
            for key in ("decision", "status", "gain", "measurement_sha256"):
                if checked[key] != value.get(key):
                    raise ValueError("Stored strategy measurement changed")
            if actor_version is None or value["actor_version"] == actor_version:
                result.append(value)
        return result

    def select(self, actor_version, limit=4):
        if not isinstance(limit, int) or not 1 <= limit <= 20:
            raise ValueError("Archive limit must be between 1 and 20")
        records = [r for r in self.records(actor_version) if r["adaptation_split"] == "development" and r["status"] != "demo"]
        selected = []
        for choices in ([r for r in records if r["origin"] == "manual"],
                        sorted([r for r in records if r["status"] == "success"], key=lambda r: -r["gain"]),
                        sorted([r for r in records if r["status"] == "failed"], key=lambda r: r["gain"])):
            if choices and choices[0] not in selected:
                selected.append(choices[0])
        remaining = [r for r in records if r not in selected]
        while remaining and len(selected) < limit:
            def novelty(record):
                words = set(re.findall(r"\w+", record["plan"]["data_instruction"].casefold()))
                similarities = []
                for previous in selected:
                    other = set(re.findall(r"\w+", previous["plan"]["data_instruction"].casefold()))
                    similarities.append(len(words & other) / max(1, len(words | other)))
                return 1 - max(similarities, default=0), record["gain"]
            chosen = max(remaining, key=novelty)
            selected.append(chosen)
            remaining.remove(chosen)
        return [{"id": r["id"], "actor_version": r["actor_version"], "task_id": r["task_id"], "plan": r["plan"],
                 "status": r["status"], "gain": r["gain"], "origin": r["origin"], "cost": r["cost"],
                 "measurement_sha256": r["measurement_sha256"]} for r in selected[:limit]]


def build_meta_dataset(records, actor_version, min_tasks=3):
    """Create proposer targets from measured wins across independent adaptation tasks."""
    from .episodes import seal_episode

    winners = []
    seen_sources = set()
    seen_tasks = set()
    for raw in sorted(records, key=lambda r: (r["task_id"], -(r.get("gain") or 0))):
        record = _record(raw)
        if record["actor_version"] != actor_version or record["status"] != "success" or record["origin"] != "model" or record["adaptation_split"] != "development":
            continue
        if record["task_id"] in seen_tasks or set(record["source_ids"]) & seen_sources:
            continue
        winners.append(record)
        seen_tasks.add(record["task_id"])
        seen_sources.update(record["source_ids"])
    if len(winners) < min_tasks:
        return {"eligible": False, "ready_for_training": False, "episodes": [], "tasks": len(winners), "reason": "More independent, measured winning adaptation tasks are required"}
    episodes, verifications = [], {}
    for record in winners:
        context = {"actor_version": actor_version, "task_id": record["task_id"], "learning_context": record["context"]}
        episode = {"schema_version": 1, "id": "meta-" + digest([record["measurement_sha256"], context])[:40],
                   "kind": "reflection", "scope": {"user_id": "rsi", "character_id": "proposer"},
                   "session_id": "adaptation-" + digest(record["task_id"])[:24], "source_ids": record["source_ids"],
                   "occurred_at": 0, "context": {"dataset_kind": "meta", **context}, "model": {"actor_version": actor_version},
                   "tools": [], "template_kwargs": {},
                   "messages": [{"id": "instruction", "role": "system", "origin": "application", "status": "finished",
                                 "content": "根据当前能力缺口、可用数据与预算，提出一次可验证的学习方案。只输出结构化 self-edit。"},
                                {"id": "context", "role": "user", "origin": "application", "status": "finished",
                                 "content": json.dumps(context, ensure_ascii=False, sort_keys=True)},
                                {"id": "plan", "role": "assistant", "origin": "model", "status": "finished",
                                 "content": json.dumps(record["plan"], ensure_ascii=False, sort_keys=True)}],
                   "outcome": {"status": "verified", "measurement_sha256": record["measurement_sha256"],
                               "actor_version": actor_version, "target_gain": record["gain"]}}
        sealed = seal_episode(episode)
        episodes.append(sealed)
        verifications[sealed["id"]] = {"episode_sha256": sealed["content_sha256"], "reviewer": "measured-strategy-verifier",
                                         "independent": True, "measurement_sha256": record["measurement_sha256"],
                                         "checks": [{"message_id": "plan", "kind": "json", "expected": record["plan"]}]}
    return {"eligible": True, "ready_for_training": len(winners) >= 10, "episodes": episodes, "verifications": verifications, "tasks": len(winners), "actor_version": actor_version,
            "measurement_sha256": digest([r["measurement_sha256"] for r in winners]),
            "note": "Split by adaptation task. Proposer release still requires held-out adaptation trials"}


def compare_meta(trials, actor_version, budget_sha256, min_tasks=3, training_task_ids=None):
    """Compare proposer versions through actual adaptation results under equal budgets."""
    tasks = set()
    deltas = []
    proposer_versions = None
    for trial in trials:
        if trial.get("task_id") in tasks:
            raise ValueError("Meta comparison needs independent task IDs")
        if trial.get("actor_version") != actor_version or trial.get("budget_sha256") != budget_sha256:
            raise ValueError("Proposer trials must use the same actor and budget")
        if trial.get("split") != "meta_test" or not trial.get("task_id"):
            raise ValueError("Proposer evaluation must use held-out adaptation tasks")
        if trial["task_id"] in set(training_task_ids or []):
            raise ValueError("Meta test task overlaps proposer training tasks")
        budget = trial.get("budget")
        if not isinstance(budget, dict) or set(budget) != {"max_training_seconds", "max_tokens", "max_steps"} or digest(budget) != budget_sha256:
            raise ValueError("Meta trial lacks the shared budget definition")
        for key, limit in budget.items():
            _finite(limit, key, 1, 1000000000)
        versions = (trial.get("baseline_proposer_version"), trial.get("candidate_proposer_version"))
        if not all(isinstance(v, str) and v for v in versions) or versions[0] == versions[1]:
            raise ValueError("Meta trials need distinct, fixed proposer versions")
        if proposer_versions is not None and proposer_versions != versions:
            raise ValueError("Proposer versions changed across held-out tasks")
        proposer_versions = versions
        before, after = _record(trial["baseline"]), _record(trial["candidate"])
        if any(r["adaptation_split"] != "meta_test" or r["actor_version"] != actor_version or r["task_id"] != trial["task_id"]
               or not r["report"].get("production") for r in (before, after)):
            raise ValueError("Meta trial lacks measured held-out adaptations")
        if before["suite_sha256"] != after["suite_sha256"]:
            raise ValueError("Proposer trials used different adaptation evaluation suites")
        for record, version in zip((before, after), versions):
            if record["context"].get("proposer_version") != version:
                raise ValueError("Measured plan is not bound to its proposer version")
            if (record["cost"]["training_seconds"] > budget["max_training_seconds"] or record["cost"]["tokens"] > budget["max_tokens"]
                    or record["plan"]["training"]["max_steps"] > budget["max_steps"]):
                raise ValueError("Proposer trial exceeded the shared budget")
        tasks.add(trial["task_id"])
        deltas.append(after["gain"] - before["gain"])
    if len(tasks) < min_tasks:
        return {"promote": False, "reason": "More independent held-out adaptation trials are required", "tasks": len(tasks)}
    if training_task_ids is None:
        return {"promote": False, "reason": "Proposer training-task lineage is required", "tasks": len(tasks)}
    # A conservative bound uses independent tasks as the statistical unit.
    mean = sum(deltas) / len(deltas)
    radius = math.sqrt(8 * math.log(20) / len(deltas))
    lower = mean - radius
    return {"promote": lower > 0, "tasks": len(tasks), "mean_gain": mean, "gain_lower_95": lower,
            "actor_version": actor_version, "budget_sha256": budget_sha256,
            "trials_sha256": digest(trials), "method": "bounded independent-task Hoeffding lower bound"}
