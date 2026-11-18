"""Persistent goals with bounded action reservations and external completion evidence."""

import copy
import hashlib
import math
import time
import uuid
from pathlib import Path

from .common import digest, exclusive, identifier, read_json, write_json


STATUSES = {"exploring", "active", "paused", "completed", "abandoned"}
TRANSITIONS = {
    "exploring": {"active", "paused", "abandoned"},
    "active": {"paused", "abandoned"},
    "paused": {"active", "abandoned"},
    "completed": set(),
    "abandoned": set(),
}


def _text(value, label):
    if not isinstance(value, str) or not value.strip():
        raise ValueError(label + " must be nonempty text")
    return value


def _number(value, label, minimum=0):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < minimum:
        raise ValueError(label + " must be a finite nonnegative number")
    return value


def _identifiers(values):
    if not isinstance(values, list) or any(not isinstance(value, str) for value in values) or len(set(values)) != len(values):
        raise ValueError("Identifiers must form a unique list")
    return [identifier(value) for value in values]


def _contract(value):
    if not isinstance(value, dict) or value.get("kind") not in {"artifact", "host"}:
        raise ValueError("Use an artifact or registered host completion contract")
    if value["kind"] == "artifact":
        path = Path(_text(value.get("path"), "Artifact path"))
        if path.is_absolute() or ".." in path.parts or ":" in str(path) or "\\" in str(path):
            raise ValueError("Artifact paths must stay inside the scope artifact directory")
        contains = value.get("contains", [])
        if not isinstance(contains, list) or not contains or any(not isinstance(item, str) or not item for item in contains):
            raise ValueError("Artifact contracts need positive content assertions")
    else:
        identifier(value.get("verifier"))
        if not value.get("expect"):
            raise ValueError("Host contracts need explicit expectations")
    return copy.deepcopy(value)


class GoalStore:
    """Owns one identity's append-only history. Time uses Unix seconds. Daily budgets use UTC+8."""

    def __init__(self, root, scope, *, clock=None, verifiers=None, daily_actions=10, daily_units=20):
        if not isinstance(scope, dict) or set(scope) != {"user_id", "character_id"}:
            raise ValueError("Goal scope needs user_id and character_id")
        for value in scope.values():
            _text(value, "Scope identity")
        self.scope = copy.deepcopy(scope)
        self.directory = Path(root) / digest(scope)
        self.artifacts = self.directory / "artifacts"
        self.clock = clock or time.time
        self.verifiers = dict(verifiers or {})
        if isinstance(daily_actions, bool) or not isinstance(daily_actions, int):
            raise ValueError("Daily action limit must be an integer")
        self.daily_actions = int(_number(daily_actions, "Daily action limit", 1))
        self.daily_units = _number(daily_units, "Daily unit limit", 1)

    def _load(self):
        state = {"goals": {}, "interests": {}, "reservations": {}}
        events, commands, previous = [], {}, None
        for path in sorted((self.directory / "events").glob("*.json")):
            event = read_json(path)
            content = {k: v for k, v in event.items() if k != "hash"}
            if event.get("scope") != self.scope or event.get("sequence") != len(events) + 1 or event.get("previous") != previous or event.get("hash") != digest(content):
                raise ValueError("Goal history contains a conflicting or corrupt event")
            if event["command_id"] in commands:
                raise ValueError("Goal history repeats a command ID")
            for collection, updates in event["updates"].items():
                if collection not in state:
                    raise ValueError("Goal history contains an unknown collection")
                for key, value in updates.items():
                    state[collection][key] = value
            events.append(event)
            commands[event["command_id"]] = event
            previous = event["hash"]
        return state, events, commands

    def events(self):
        return copy.deepcopy(self._load()[1])

    def _mutate(self, kind, request, command_id, operation):
        identifier(command_id)
        request_hash = digest([kind, request])
        with exclusive(self.directory / "writer.lock"):
            state, events, commands = self._load()
            if command_id in commands:
                previous = commands[command_id]
                if previous["request_hash"] != request_hash:
                    raise ValueError("Command ID already has different content")
                return copy.deepcopy(previous["result"])
            now = _number(self.clock(), "Clock")
            updates, result = operation(state, now)
            event = {"schema_version": 1, "scope": self.scope, "sequence": len(events) + 1,
                     "command_id": command_id, "request_hash": request_hash, "kind": kind, "at": now,
                     "previous": events[-1]["hash"] if events else None, "updates": updates, "result": result}
            event["hash"] = digest(event)
            path = self.directory / "events" / (f"{event['sequence']:012d}-" + digest(command_id) + ".json")
            if path.exists():
                raise ValueError("Goal event already exists")
            write_json(path, event)
            return copy.deepcopy(result)

    def list_goals(self):
        return copy.deepcopy(list(self._load()[0]["goals"].values()))

    def list_interests(self):
        return copy.deepcopy(list(self._load()[0]["interests"].values()))

    def get(self, goal_id):
        return copy.deepcopy(self._goal(self._load()[0], goal_id))

    @staticmethod
    def _goal(state, goal_id, expected_version=None):
        identifier(goal_id)
        goal = state["goals"].get(goal_id)
        if not goal:
            raise ValueError("Goal does not exist in this scope")
        if expected_version is not None and goal["version"] != expected_version:
            raise ValueError("Goal version changed")
        return copy.deepcopy(goal)

    @staticmethod
    def _authored(spec):
        if spec.get("origin") not in {"model", "human"}:
            raise ValueError("Goal origin must name model or human")
        _text(spec.get("rationale"), "Rationale")
        if not isinstance(spec.get("sources"), list) or not spec["sources"] or any(not isinstance(s, str) or not s for s in spec["sources"]):
            raise ValueError("Goal and interest records need source references")
        identifier(spec.get("id"))
        _text(spec.get("title"), "Title")

    def create_interest(self, spec, command_id):
        spec = copy.deepcopy(spec)
        self._authored(spec)
        if set(spec) - {"id", "title", "origin", "sources", "rationale", "questions"}:
            raise ValueError("Unknown interest fields")
        if not isinstance(spec.get("questions", []), list) or any(not isinstance(q, str) for q in spec.get("questions", [])):
            raise ValueError("Interest questions must be text")

        def operation(state, now):
            if spec["id"] in state["interests"]:
                raise ValueError("Interest already exists")
            interest = {**spec, "version": 1, "created_at": now, "updated_at": now}
            return {"interests": {spec["id"]: interest}}, interest

        return self._mutate("create_interest", spec, command_id, operation)

    def revise_interest(self, interest_id, changes, expected_version, command_id):
        identifier(interest_id)
        changes = copy.deepcopy(changes)
        if not changes or set(changes) - {"title", "rationale", "sources", "questions"}:
            raise ValueError("Only interest context can be revised")

        def operation(state, now):
            interest = copy.deepcopy(state["interests"].get(interest_id))
            if not interest or interest["version"] != expected_version:
                raise ValueError("Interest is missing or its version changed")
            interest.update(changes)
            self._authored(interest)
            if not isinstance(interest.get("questions", []), list) or any(not isinstance(q, str) for q in interest.get("questions", [])):
                raise ValueError("Interest questions must be text")
            interest.update(version=interest["version"] + 1, updated_at=now)
            return {"interests": {interest_id: interest}}, interest

        return self._mutate("revise_interest", [interest_id, changes, expected_version], command_id, operation)

    def _validate_goal(self, goal, state):
        self._authored(goal)
        for key in ("interest_ids", "dependencies"):
            _identifiers(goal[key])
        if any(item not in state["interests"] for item in goal["interest_ids"]):
            raise ValueError("Goal references an unknown interest")
        if any(item not in state["goals"] or item == goal["id"] for item in goal["dependencies"]):
            raise ValueError("Goal references an invalid dependency")
        if not isinstance(goal["blockers"], list) or any(not isinstance(b, str) or not b for b in goal["blockers"]):
            raise ValueError("Blockers must be text")
        budget = goal["budget"]
        if not isinstance(budget, dict) or set(budget) != {"max_actions", "max_units"}:
            raise ValueError("Budget needs max_actions and max_units")
        if isinstance(budget["max_actions"], bool) or not isinstance(budget["max_actions"], int):
            raise ValueError("Action limit must be an integer")
        _number(budget["max_actions"], "Action limit", 1)
        _number(budget["max_units"], "Unit limit", 1)
        milestones = goal["milestones"]
        if not isinstance(milestones, list) or not milestones:
            raise ValueError("Goals need at least one milestone")
        _identifiers([m["id"] for m in milestones])
        for milestone in milestones:
            if set(milestone) != {"id", "description", "contract", "completed", "evidence"}:
                raise ValueError("Invalid milestone fields")
            _text(milestone["description"], "Milestone description")
            _contract(milestone["contract"])
        _number(goal["priority"], "Priority")
        action = goal.get("next_action")
        if action is not None:
            if not isinstance(action, dict) or set(action) - {"id", "description", "tool", "arguments", "estimated_units", "milestone_ids", "not_before"}:
                raise ValueError("Invalid next action")
            identifier(action.get("id"))
            identifier(action.get("tool"))
            _text(action.get("description"), "Action description")
            if not isinstance(action.get("arguments"), dict):
                raise ValueError("Action arguments must be an object")
            _number(action.get("estimated_units"), "Action estimate", 0.01)
            targets = _identifiers(action.get("milestone_ids"))
            available = {m["id"] for m in milestones if not m["completed"]}
            if not targets or not set(targets) <= available:
                raise ValueError("Action must target incomplete milestones")
            _number(action.get("not_before", 0), "Action start time")
        graph = {key: g["dependencies"] for key, g in state["goals"].items()}
        graph[goal["id"]] = goal["dependencies"]

        def visit(key, ancestors):
            if key in ancestors:
                raise ValueError("Goal dependencies contain a cycle")
            for dependency in graph.get(key, []):
                visit(dependency, ancestors | {key})

        visit(goal["id"], set())

    def create_goal(self, spec, command_id):
        spec = copy.deepcopy(spec)
        allowed = {"id", "title", "origin", "sources", "rationale", "interest_ids", "milestones", "budget", "next_action", "dependencies", "blockers", "priority"}
        if set(spec) - allowed:
            raise ValueError("Unknown goal fields")

        def operation(state, now):
            if spec.get("id") in state["goals"]:
                raise ValueError("Goal already exists")
            goal = {"interest_ids": [], "dependencies": [], "blockers": [], "priority": 0, "next_action": None, **spec}
            goal["milestones"] = [{**m, "completed": False, "evidence": []} for m in goal.get("milestones", [])]
            goal.update(status="exploring", version=1, created_at=now, updated_at=now,
                        used_actions=0, used_units=0, last_reason="Created")
            self._validate_goal(goal, state)
            return {"goals": {goal["id"]: goal}}, goal

        return self._mutate("create_goal", spec, command_id, operation)

    def revise(self, goal_id, changes, expected_version, command_id):
        changes = copy.deepcopy(changes)
        if not changes or set(changes) - {"title", "rationale", "sources", "interest_ids", "next_action", "dependencies", "blockers", "priority"}:
            raise ValueError("Only goal context and the next action can be revised")

        def operation(state, now):
            goal = self._goal(state, goal_id, expected_version)
            if goal["status"] in {"completed", "abandoned"} or self._pending(state, goal_id):
                raise ValueError("Terminal goals and reserved actions cannot be revised")
            action = changes.get("next_action")
            if action and any(r["goal_id"] == goal_id and r["action"]["id"] == action["id"] for r in state["reservations"].values()):
                raise ValueError("Use a new action ID after an attempted action")
            goal.update(changes)
            goal.update(version=goal["version"] + 1, updated_at=now)
            self._validate_goal(goal, state)
            return {"goals": {goal_id: goal}}, goal

        return self._mutate("revise", [goal_id, changes, expected_version], command_id, operation)

    def transition(self, goal_id, status, reason, expected_version, command_id):
        if status not in STATUSES:
            raise ValueError("Unknown goal status")
        _text(reason, "Transition reason")

        def operation(state, now):
            goal = self._goal(state, goal_id, expected_version)
            if status not in TRANSITIONS[goal["status"]]:
                raise ValueError("Goal transition is not allowed. Completion requires evidence")
            if self._pending(state, goal_id):
                raise ValueError("Reconcile the reserved action before changing its goal")
            goal.update(status=status, last_reason=reason, version=goal["version"] + 1, updated_at=now)
            return {"goals": {goal_id: goal}}, goal

        return self._mutate("transition", [goal_id, status, reason, expected_version], command_id, operation)

    @staticmethod
    def _pending(state, goal_id):
        return any(r["goal_id"] == goal_id and r["status"] == "reserved" for r in state["reservations"].values())

    def _eligible(self, state, goal, now):
        if goal["status"] not in {"active", "exploring"}:
            return "Goal is not active"
        if self._pending(state, goal["id"]):
            return "Reserved action needs a result or reconciliation"
        if goal["blockers"] or any(state["goals"][key]["status"] != "completed" for key in goal["dependencies"]):
            return "Goal has blockers or unfinished dependencies"
        action = goal["next_action"]
        if not action or action.get("not_before", 0) > now:
            return "No action is due"
        if goal["used_actions"] >= goal["budget"]["max_actions"] or goal["used_units"] + action["estimated_units"] > goal["budget"]["max_units"]:
            return "Goal budget is exhausted"
        today = int((now + 8 * 3600) // 86400)
        reservations = [r for r in state["reservations"].values() if int((r["created_at"] + 8 * 3600) // 86400) == today]
        if len(reservations) >= self.daily_actions or sum(r["charged_units"] for r in reservations) + action["estimated_units"] > self.daily_units:
            return "Daily action budget is exhausted"
        return None

    def select(self, now=None, preferred_goal_id=None, *, rest=False):
        now = _number(self.clock() if now is None else now, "Clock")
        if rest:
            return {"decision": "rest", "reason": "Actor selected rest"}
        state = self._load()[0]
        choices = sorted(state["goals"].values(), key=lambda g: (-g["priority"], g["created_at"], g["id"]))
        if preferred_goal_id is not None:
            choices = [self._goal(state, preferred_goal_id)]
        for goal in choices:
            reason = self._eligible(state, goal, now)
            if reason is None:
                return {"decision": "act", "goal_id": goal["id"], "goal_version": goal["version"], "action": copy.deepcopy(goal["next_action"])}
        return {"decision": "rest", "reason": "No eligible action within the current budgets"}

    def reserve(self, goal_id, expected_version, *, lease_seconds=60, command_id):
        _number(lease_seconds, "Lease duration", 1)
        if lease_seconds > 3600:
            raise ValueError("Action leases are limited to one hour")

        def operation(state, now):
            goal = self._goal(state, goal_id, expected_version)
            reason = self._eligible(state, goal, now)
            if reason:
                raise ValueError(reason)
            action = copy.deepcopy(goal["next_action"])
            artifacts_before = {}
            for milestone in goal["milestones"]:
                contract = milestone["contract"]
                if milestone["id"] in action["milestone_ids"] and contract["kind"] == "artifact":
                    path = self.artifacts / contract["path"]
                    artifacts_before[contract["path"]] = hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else None
            reservation = {"id": uuid.uuid4().hex, "goal_id": goal_id, "goal_version": goal["version"], "scope": self.scope,
                           "action": action, "created_at": now, "expires_at": now + lease_seconds, "status": "reserved",
                           "charged_units": action["estimated_units"], "artifacts_before": artifacts_before, "outcome": None}
            goal.update(status="active", version=goal["version"] + 1, updated_at=now,
                        used_actions=goal["used_actions"] + 1, used_units=goal["used_units"] + action["estimated_units"])
            return {"goals": {goal_id: goal}, "reservations": {reservation["id"]: reservation}}, reservation

        return self._mutate("reserve", [goal_id, expected_version, lease_seconds], command_id, operation)

    def _verify(self, contract, reservation, outcome):
        evidence = outcome.get("evidence", {})
        if contract["kind"] == "host":
            verifier = self.verifiers.get(contract["verifier"])
            if verifier is None:
                raise ValueError("The host completion verifier is not registered")
            checked = verifier(copy.deepcopy(contract), copy.deepcopy(reservation), copy.deepcopy(outcome))
            if not isinstance(checked, dict) or checked.get("accepted") is not True or not checked.get("evidence_ids"):
                raise ValueError("The host did not verify this milestone")
            return {"kind": "host", "verifier": contract["verifier"], "binding": digest([reservation, outcome]), "evidence_ids": checked["evidence_ids"]}
        if evidence.get("path") != contract["path"]:
            raise ValueError("Artifact evidence does not match the milestone path")
        root = self.artifacts.resolve()
        original = self.artifacts / contract["path"]
        target = original.resolve(strict=True)
        if not target.is_relative_to(root) or any(p.is_symlink() for p in [original, *original.parents]) or not target.is_file():
            raise ValueError("Artifact evidence escapes its scope")
        if target.stat().st_size > 5 * 1024 * 1024:
            raise ValueError("Artifact assertions support at most five MiB")
        data = target.read_bytes()
        sha = hashlib.sha256(data).hexdigest()
        if evidence.get("kind") != "artifact" or sha != evidence.get("sha256"):
            raise ValueError("Artifact evidence content changed")
        if not contract.get("allow_existing", False) and sha == reservation["artifacts_before"].get(contract["path"]):
            raise ValueError("This action did not produce or change its artifact")
        text = data.decode("utf-8")
        if not all(value in text for value in contract["contains"]):
            raise ValueError("Artifact does not satisfy the milestone assertions")
        return {"kind": "artifact", "path": contract["path"], "sha256": sha, "binding": digest([reservation["id"], reservation["action"], sha])}

    def settle(self, reservation_id, outcome, command_id):
        identifier(reservation_id)
        outcome = copy.deepcopy(outcome)
        if not isinstance(outcome, dict) or outcome.get("status") not in {"succeeded", "failed", "not_executed"}:
            raise ValueError("Action outcome needs succeeded, failed, or not_executed status")
        if outcome.get("origin") not in {"tool", "environment", "human"}:
            raise ValueError("Action outcomes require an external origin")

        def operation(state, now):
            reservation = copy.deepcopy(state["reservations"].get(reservation_id))
            if not reservation or reservation["status"] != "reserved":
                raise ValueError("Reservation is missing or already settled")
            if outcome.get("reservation_id") != reservation_id or outcome.get("action_id") != reservation["action"]["id"]:
                raise ValueError("Outcome is not bound to the reserved action")
            units = _number(outcome.get("actual_units"), "Actual action units")
            if units > reservation["charged_units"]:
                raise ValueError("Action cost exceeds its reservation")
            goal = self._goal(state, reservation["goal_id"])
            evidence = {}
            if outcome["status"] == "succeeded":
                for milestone in goal["milestones"]:
                    if milestone["id"] in reservation["action"]["milestone_ids"]:
                        milestone_outcome = copy.deepcopy(outcome)
                        milestone_outcome["evidence"] = outcome.get("evidence_by_milestone", {}).get(milestone["id"], outcome.get("evidence", {}))
                        checked = self._verify(milestone["contract"], reservation, milestone_outcome)
                        milestone.update(completed=True, evidence=[checked])
                        evidence[milestone["id"]] = checked
            else:
                verifier = self.verifiers.get("outcome")
                checked = verifier({}, copy.deepcopy(reservation), copy.deepcopy(outcome)) if verifier else None
                if not outcome.get("reason") or not isinstance(checked, dict) or checked.get("accepted") is not True or not checked.get("evidence_ids"):
                    raise ValueError("Failed or unexecuted actions need a registered host outcome verifier")
                evidence["outcome"] = checked
            goal.update(next_action=None, updated_at=now, version=goal["version"] + 1,
                        used_units=goal["used_units"] - reservation["charged_units"] + units)
            if all(m["completed"] for m in goal["milestones"]):
                goal.update(status="completed", last_reason="Completion contracts passed")
            reservation.update(status="settled", settled_at=now, charged_units=units, outcome=outcome, verified_evidence=evidence)
            return {"goals": {goal["id"]: goal}, "reservations": {reservation_id: reservation}}, {"goal": goal, "reservation": reservation}

        return self._mutate("settle", [reservation_id, outcome], command_id, operation)
