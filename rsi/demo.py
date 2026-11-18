"""A synthetic walkthrough with real file receipts and no model execution."""

from pathlib import Path

from .common import digest, file_hash, write_json
from .dataset import build_dataset
from .episodes import EpisodeStore, seal_episode
from .evaluation import evaluate, gate
from .goals import GoalStore


def run_demo(output):
    root = Path(output)
    if root.exists():
        raise ValueError("Use a new demonstration directory")
    root.mkdir(parents=True)
    store = EpisodeStore(root / "capture")
    episodes, proofs = [], {}
    for i in range(80):
        category = ["base", "dialogue", "reflection", "initiative"][i % 4]
        kind = "dialogue" if category == "base" else category
        messages = [{"id": "state", "role": "system", "origin": "application", "status": "finished", "content": "合成状态与证据编号：" + str(i)}]
        if kind == "dialogue":
            messages.append({"id": "input", "role": "user", "origin": "human", "status": "received", "content": "合成问题：" + str(i)})
        messages.append({"id": "decision", "role": "assistant", "origin": "model", "status": "finished", "content": "根据合成证据完成独立判断：" + str(i)})
        episode = seal_episode({"schema_version": 1, "id": "demo-" + str(i), "kind": kind,
                                "scope": {"user_id": "demo", "character_id": "demo"}, "session_id": "session-" + str(i),
                                "source_ids": ["source-" + str(i)], "occurred_at": i, "context": {"dataset_kind": category},
                                "model": {"version": "synthetic"}, "tools": [], "template_kwargs": {}, "messages": messages,
                                "outcome": {"status": "unverified"}})
        store.append(episode)
        episodes.append(episode)
        proofs[episode["id"]] = {"episode_sha256": episode["content_sha256"], "reviewer": "synthetic-fixture", "independent": True,
                                 "checks": [{"message_id": "decision", "kind": "exact", "expected": messages[-1]["content"]}]}
    mix = {"base": 0.4, "dialogue": 0.3, "reflection": 0.2, "initiative": 0.1}
    manifest = build_dataset(episodes, root / "dataset", verifications=proofs, mix=mix, character_budget=12000)
    cases = [{"id": "held-" + str(i), "group": "held-group-" + str(i), "task_id": "held-task-" + str(i),
              "source_ids": ["held-source-" + str(i)], "slice": "base", "messages": [{"role": "user", "content": "独立合成用例" + str(i)}],
              "expect": {"exact": ["正确"]}} for i in range(6)]
    before = [{"id": c["id"], "actor_version": "synthetic-old", "text": "错误", "complete": True, "tokens": 3, "latency_ms": 1} for c in cases]
    after = [{**p, "actor_version": "synthetic-new", "text": "正确"} for p in before]
    policy = {"required_slices": ["base"], "protected_slices": ["base"], "target_slices": ["base"],
              "min_cases": 6, "min_groups": 6, "min_per_slice": 6, "min_groups_per_slice": 6, "bootstrap_samples": 100}
    report = evaluate(cases, before, after, policy=policy, demo=True)
    write_json(root / "evaluation.json", report)
    goals = GoalStore(root / "goals", {"user_id": "demo", "character_id": "demo"})
    goal = goals.create_goal({"id": "explore", "title": "合成的自选比较项目", "origin": "model", "sources": ["demo-3"],
                              "rationale": "从已有观察中继续探索", "budget": {"max_actions": 2, "max_units": 3},
                              "milestones": [{"id": "result", "description": "形成可核对的比较", "contract": {"kind": "artifact", "path": "comparison.txt", "contains": ["合成证据"]}}],
                              "next_action": {"id": "compare", "description": "写出比较", "tool": "write_artifact", "arguments": {"path": "comparison.txt"},
                                              "estimated_units": 1, "milestone_ids": ["result"]}}, "demo-create")
    reservation = goals.reserve(goal["id"], goal["version"], command_id="demo-reserve")
    artifact = goals.artifacts / "comparison.txt"
    artifact.parent.mkdir(parents=True, exist_ok=True)
    artifact.write_text("已核对的合成证据及比较。", encoding="utf-8")
    settled = goals.settle(reservation["id"], {"status": "succeeded", "origin": "environment", "reservation_id": reservation["id"],
                                              "action_id": "compare", "actual_units": 1,
                                              "evidence": {"kind": "artifact", "path": "comparison.txt", "sha256": file_hash(artifact)}}, "demo-settle")
    summary = {"demonstration": True, "captured": store.audit(), "dataset_records": len(manifest["records"]),
               "evaluation_gain": report["target"]["mean_gain"], "decision": gate(report),
               "goal_status": settled["goal"]["status"], "training_executed": False, "network_calls": 0}
    write_json(root / "summary.json", summary)
    return summary
