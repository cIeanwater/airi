"""Goal lifecycle and restart checks with synthetic files and host receipts."""

import copy
import hashlib
import tempfile
import unittest
from pathlib import Path

from rsi.common import read_json, write_json
from rsi.goals import GoalStore


class GoalTests(unittest.TestCase):
    def setUp(self):
        temporary_root = Path(__file__).resolve().parents[1] / "private/test-temp"
        temporary_root.mkdir(parents=True, exist_ok=True)
        self.temporary = tempfile.TemporaryDirectory(dir=temporary_root)
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.scope = {"user_id": "synthetic-owner", "character_id": "synthetic-character"}
        self.now = 1000000
        self.store = GoalStore(self.root, self.scope, clock=lambda: self.now)

    def spec(self, goal_id="music"):
        return {
            "id": goal_id, "title": "Compare two musical structures", "origin": "model",
            "sources": ["episode-synthetic"], "rationale": "A repeated contrast raises a question",
            "budget": {"max_actions": 3, "max_units": 5},
            "milestones": [{"id": "comparison", "description": "Produce a grounded comparison",
                            "contract": {"kind": "artifact", "path": goal_id + ".txt", "contains": ["measured contrast"]}}],
            "next_action": {"id": "compare", "description": "Write the comparison", "tool": "write_artifact",
                            "arguments": {"path": goal_id + ".txt"}, "estimated_units": 2, "milestone_ids": ["comparison"]},
        }

    def create(self, goal_id="music"):
        return self.store.create_goal(self.spec(goal_id), "create-" + goal_id)

    def artifact_outcome(self, reservation, text="A measured contrast with actual evidence"):
        path = self.store.artifacts / (reservation["goal_id"] + ".txt")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        return {"status": "succeeded", "origin": "tool", "reservation_id": reservation["id"],
                "action_id": reservation["action"]["id"], "actual_units": 1,
                "evidence": {"kind": "artifact", "path": path.name, "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}}

    def test_model_goal_progress_needs_no_user_message(self):
        goal = self.create()
        selected = self.store.select()
        self.assertEqual(selected["decision"], "act")
        self.assertEqual(selected["goal_id"], "music")
        reservation = self.store.reserve(goal["id"], goal["version"], command_id="reserve")
        result = self.store.settle(reservation["id"], self.artifact_outcome(reservation), "settle")
        self.assertEqual(result["goal"]["status"], "completed")
        self.assertEqual(result["goal"]["used_units"], 1)
        self.assertEqual(self.store.select()["decision"], "rest")

    def test_scope_isolation_survives_restart(self):
        self.create()
        reopened = GoalStore(self.root, self.scope, clock=lambda: self.now)
        self.assertEqual(reopened.get("music")["title"], "Compare two musical structures")
        other = GoalStore(self.root, {**self.scope, "user_id": "other"})
        self.assertEqual(other.list_goals(), [])
        with self.assertRaisesRegex(ValueError, "scope"):
            other.get("music")

    def test_create_and_reserve_commands_are_idempotent(self):
        goal = self.create()
        self.assertEqual(self.create(), goal)
        reservation = self.store.reserve("music", 1, command_id="reserve")
        self.assertEqual(self.store.reserve("music", 1, command_id="reserve"), reservation)
        self.assertEqual(self.store.get("music")["used_actions"], 1)
        with self.assertRaisesRegex(ValueError, "different content"):
            self.store.reserve("music", 1, lease_seconds=30, command_id="reserve")

    def test_expired_reservation_does_not_repeat_an_unknown_effect(self):
        self.create()
        reservation = self.store.reserve("music", 1, lease_seconds=5, command_id="reserve")
        self.now += 100
        reopened = GoalStore(self.root, self.scope, clock=lambda: self.now)
        self.assertEqual(reopened.select()["decision"], "rest")
        with self.assertRaisesRegex(ValueError, "reconciliation"):
            reopened.reserve("music", 2, command_id="retry")
        result = reopened.settle(reservation["id"], self.artifact_outcome(reservation), "late-result")
        self.assertEqual(result["goal"]["status"], "completed")

    def test_self_claimed_success_cannot_complete_a_goal(self):
        self.create()
        reservation = self.store.reserve("music", 1, command_id="reserve")
        outcome = self.artifact_outcome(reservation)
        outcome["origin"] = "model"
        outcome["verified"] = True
        with self.assertRaisesRegex(ValueError, "external origin"):
            self.store.settle(reservation["id"], outcome, "claim")
        self.assertEqual(self.store.get("music")["status"], "active")
        with self.assertRaisesRegex(ValueError, "Completion requires evidence"):
            self.store.transition("music", "completed", "I feel done", 2, "complete")

    def test_content_and_action_bindings_cannot_be_reused(self):
        self.create()
        reservation = self.store.reserve("music", 1, command_id="reserve")
        outcome = self.artifact_outcome(reservation)
        outcome["action_id"] = "other-action"
        with self.assertRaisesRegex(ValueError, "bound"):
            self.store.settle(reservation["id"], outcome, "wrong-action")
        outcome["action_id"] = "compare"
        (self.store.artifacts / "music.txt").write_text("changed", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "content changed"):
            self.store.settle(reservation["id"], outcome, "wrong-content")

    def test_preexisting_artifact_is_not_new_progress(self):
        self.create()
        self.store.artifacts.mkdir(parents=True)
        path = self.store.artifacts / "music.txt"
        path.write_text("A measured contrast with actual evidence", encoding="utf-8")
        reservation = self.store.reserve("music", 1, command_id="reserve")
        outcome = self.artifact_outcome(reservation)
        with self.assertRaisesRegex(ValueError, "did not produce"):
            self.store.settle(reservation["id"], outcome, "stale-progress")

    def test_artifact_positive_assertions_are_checked(self):
        self.create()
        reservation = self.store.reserve("music", 1, command_id="reserve")
        outcome = self.artifact_outcome(reservation, "I completed the project")
        with self.assertRaisesRegex(ValueError, "assertions"):
            self.store.settle(reservation["id"], outcome, "incorrect-artifact")

    def test_pause_resume_abandon_and_stale_versions(self):
        self.create()
        paused = self.store.transition("music", "paused", "Need a quiet interval", 1, "pause")
        self.assertEqual(self.store.select()["decision"], "rest")
        with self.assertRaisesRegex(ValueError, "version changed"):
            self.store.transition("music", "active", "Resume", 1, "stale-resume")
        resumed = self.store.transition("music", "active", "New evidence arrived", paused["version"], "resume")
        abandoned = self.store.transition("music", "abandoned", "Question is no longer useful", resumed["version"], "abandon")
        self.assertEqual(abandoned["last_reason"], "Question is no longer useful")
        with self.assertRaisesRegex(ValueError, "not allowed"):
            self.store.transition("music", "active", "Restart", abandoned["version"], "restart-terminal")

    def test_interest_and_actor_choice_are_persistent(self):
        interest = {"id": "harmony", "title": "Harmony", "origin": "model", "sources": ["experience"],
                    "rationale": "Repeated listening raises a question", "questions": ["Why does this contrast recur?"]}
        self.store.create_interest(interest, "create-interest")
        spec = self.spec()
        spec["interest_ids"] = ["harmony"]
        self.store.create_goal(spec, "create-music")
        self.create("rhythm")
        selected = self.store.select(preferred_goal_id="rhythm")
        self.assertEqual(selected["goal_id"], "rhythm")
        self.assertEqual(self.store.select(rest=True)["decision"], "rest")
        reopened = GoalStore(self.root, self.scope)
        self.assertEqual(reopened.list_interests()[0]["id"], "harmony")
        revised = reopened.revise_interest("harmony", {"questions": ["Which contrast survives a new context?"]}, 1, "enrich-interest")
        self.assertEqual(revised["version"], 2)
        self.assertEqual(len(reopened.events()), 4)

    def test_dependency_cycle_and_invalid_budget_are_rejected(self):
        self.create("first")
        spec = self.spec("second")
        spec["dependencies"] = ["first"]
        self.store.create_goal(spec, "create-second")
        with self.assertRaisesRegex(ValueError, "cycle"):
            self.store.revise("first", {"dependencies": ["second"]}, 1, "create-cycle")
        spec = self.spec("bad")
        spec["budget"]["max_units"] = float("nan")
        with self.assertRaises(ValueError):
            self.store.create_goal(spec, "invalid-budget")
        self.assertEqual(self.store.select(preferred_goal_id="second")["decision"], "rest")

    def test_daily_budget_covers_several_goals(self):
        limited = GoalStore(self.root, self.scope, clock=lambda: self.now, daily_actions=1)
        limited.create_goal(self.spec("first"), "create-first")
        limited.create_goal(self.spec("second"), "create-second")
        limited.reserve("first", 1, command_id="reserve-first")
        self.assertEqual(limited.select(preferred_goal_id="second")["decision"], "rest")
        self.now += 86400
        self.assertEqual(limited.select(preferred_goal_id="second")["decision"], "act")

    def test_failed_action_needs_host_verification_and_new_action_id(self):
        self.create()
        reservation = self.store.reserve("music", 1, command_id="reserve")
        outcome = {"status": "failed", "origin": "tool", "reservation_id": reservation["id"],
                   "action_id": "compare", "actual_units": 1, "reason": "Source unavailable", "evidence_ids": ["untrusted-id"]}
        with self.assertRaisesRegex(ValueError, "registered host"):
            self.store.settle(reservation["id"], outcome, "claim-failed")
        receipts = {reservation["id"]: {"action_id": "compare", "status": "failed"}}

        def host_check(contract, current, received):
            receipt = receipts.get(current["id"], {})
            return {"accepted": receipt == {"action_id": received["action_id"], "status": received["status"]}, "evidence_ids": ["host-receipt"]}

        reopened = GoalStore(self.root, self.scope, clock=lambda: self.now, verifiers={"outcome": host_check})
        result = reopened.settle(reservation["id"], outcome, "measured-failure")
        self.assertEqual(result["goal"]["status"], "active")
        self.assertEqual(result["goal"]["next_action"], None)
        with self.assertRaisesRegex(ValueError, "new action ID"):
            reopened.revise("music", {"next_action": self.spec()["next_action"]}, 3, "repeat-action")

    def test_registered_host_checks_goal_specific_result(self):
        spec = self.spec()
        spec["milestones"][0]["contract"] = {"kind": "host", "verifier": "tool_receipt", "expect": {"comparison_count": 2}}
        self.store.create_goal(spec, "create-host-goal")
        reservation = self.store.reserve("music", 1, command_id="reserve")
        outcome = {"status": "succeeded", "origin": "tool", "reservation_id": reservation["id"],
                   "action_id": "compare", "actual_units": 1, "receipt": {"comparison_count": 2}}
        with self.assertRaisesRegex(ValueError, "not registered"):
            self.store.settle(reservation["id"], outcome, "missing-verifier")

        def verify(contract, current, result):
            return {"accepted": result["receipt"] == contract["expect"] and current["action"]["id"] == result["action_id"],
                    "evidence_ids": ["synthetic-runtime-receipt"]}

        reopened = GoalStore(self.root, self.scope, verifiers={"tool_receipt": verify})
        completed = reopened.settle(reservation["id"], outcome, "verified-completion")
        self.assertEqual(completed["goal"]["status"], "completed")

    def test_event_chain_detects_modified_history(self):
        self.create()
        path = next((self.store.directory / "events").glob("*.json"))
        event = read_json(path)
        event["updates"]["goals"]["music"]["title"] = "changed outside the writer"
        write_json(path, event)
        with self.assertRaisesRegex(ValueError, "corrupt"):
            self.store.list_goals()


if __name__ == "__main__":
    unittest.main()
