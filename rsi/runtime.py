"""A bounded host boundary for one autonomous goal action per wake."""

from pathlib import Path
from uuid import uuid4

from .common import file_hash


class ArtifactWriter:
    """Write only inside the current identity's goal artifact directory."""

    def __init__(self, directory):
        self.directory = Path(directory).resolve()

    def execute(self, reservation):
        arguments = reservation["action"]["arguments"]
        relative = Path(arguments["path"])
        if relative.is_absolute() or ".." in relative.parts or ":" in str(relative) or "\\" in str(relative):
            raise ValueError("Goal artifact path escapes its scope")
        target = self.directory / relative
        if not target.resolve().is_relative_to(self.directory) or any(p.is_symlink() for p in [target, *target.parents]):
            raise ValueError("Linked goal artifact paths are refused")
        text = arguments["text"]
        if not isinstance(text, str) or len(text.encode("utf-8")) > 5 * 1024 * 1024:
            raise ValueError("Goal artifact exceeds its text budget")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")
        return {"status": "succeeded", "origin": "tool", "reservation_id": reservation["id"],
                "action_id": reservation["action"]["id"], "actual_units": reservation["charged_units"],
                "evidence": {"kind": "artifact", "path": relative.as_posix(), "sha256": file_hash(target)}}


class GoalRunner:
    """Host adapters execute effects. The model can choose goals and arguments only."""

    def __init__(self, store, adapters=None):
        self.store = store
        self.adapters = adapters or {}

    def tick(self, *, execute=False, preferred_goal_id=None, rest=False):
        selected = self.store.select(preferred_goal_id=preferred_goal_id, rest=rest)
        if selected["decision"] != "act" or not execute:
            return {**selected, "execute": execute}
        goal = self.store.get(selected["goal_id"])
        adapter = self.adapters.get(goal["next_action"]["tool"])
        if adapter is None:
            return {"decision": "blocked", "goal_id": goal["id"], "reason": "No registered host adapter"}
        reservation = self.store.reserve(goal["id"], goal["version"], command_id="reserve-" + uuid4().hex)
        try:
            outcome = adapter.execute(reservation)
            settled = self.store.settle(reservation["id"], outcome, "settle-" + reservation["id"])
        except (ValueError, OSError, RuntimeError) as error:
            # An uncertain effect stays reserved until the host reconciles its evidence.
            return {"decision": "reconcile", "reservation_id": reservation["id"], "reason": type(error).__name__}
        return {"decision": "acted", "goal_id": goal["id"], "status": settled["goal"]["status"], "reservation_id": reservation["id"]}
