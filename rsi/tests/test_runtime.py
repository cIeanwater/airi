"""Autonomous host dispatch and uncertainty handling."""

import tempfile
import unittest
from pathlib import Path

from rsi.goals import GoalStore
from rsi.runtime import ArtifactWriter, GoalRunner


class RuntimeTests(unittest.TestCase):
    def setUp(self):
        root = Path(__file__).resolve().parents[1] / "private/test-temp"
        root.mkdir(parents=True, exist_ok=True)
        self.temporary = tempfile.TemporaryDirectory(dir=root)
        self.addCleanup(self.temporary.cleanup)
        self.store = GoalStore(self.temporary.name, {"user_id": "synthetic", "character_id": "synthetic"})
        self.store.create_goal({"id": "learn", "title": "Explore evidence", "origin": "model", "sources": ["source"], "rationale": "An unresolved question",
                                "budget": {"max_actions": 2, "max_units": 3},
                                "milestones": [{"id": "result", "description": "Record verified comparison", "contract": {"kind": "artifact", "path": "comparison.txt", "contains": ["evidence"]}}],
                                "next_action": {"id": "write", "description": "Compare", "tool": "write_artifact", "arguments": {"path": "comparison.txt", "text": "synthetic evidence"},
                                                "estimated_units": 1, "milestone_ids": ["result"]}}, "create")

    def test_goal_advances_without_user_and_then_rests(self):
        runner = GoalRunner(self.store, {"write_artifact": ArtifactWriter(self.store.artifacts)})
        self.assertEqual(runner.tick(execute=True)["status"], "completed")
        self.assertEqual(runner.tick(execute=True)["decision"], "rest")

    def test_dry_run_does_not_reserve_or_write(self):
        runner = GoalRunner(self.store, {"write_artifact": ArtifactWriter(self.store.artifacts)})
        runner.tick()
        self.assertEqual(self.store.get("learn")["used_actions"], 0)
        self.assertFalse((self.store.artifacts / "comparison.txt").exists())

    def test_unknown_host_tool_is_blocked_before_reservation(self):
        self.assertEqual(GoalRunner(self.store).tick(execute=True)["decision"], "blocked")
        self.assertEqual(self.store.get("learn")["used_actions"], 0)

    def test_escaping_artifact_stays_pending_for_reconciliation(self):
        goal = self.store.get("learn")
        action = {**goal["next_action"], "arguments": {"path": "../escape.txt", "text": "evidence"}}
        self.store.revise("learn", {"next_action": action}, goal["version"], "revise")
        runner = GoalRunner(self.store, {"write_artifact": ArtifactWriter(self.store.artifacts)})
        self.assertEqual(runner.tick(execute=True)["decision"], "reconcile")
        self.assertEqual(runner.tick(execute=True)["decision"], "rest")


if __name__ == "__main__":
    unittest.main()
