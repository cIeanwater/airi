"""Check companion training targets against the recorded AIRI context."""

import json
import re

from .common import digest
from .host_context import HOST_MARKER, project_host_messages


ACT = re.compile(r"<\|ACT\s+(\{[^\n]*?\})\|>")
HAN = re.compile(r"[\u3400-\u9fff]")
ENGLISH = re.compile(r"[A-Za-z]")
EMOTIONS = {"happy", "sad", "angry", "think", "surprised", "awkward", "question", "curious", "neutral"}


def _host_state(messages):
    for message in reversed(messages):
        if message["role"] == "user":
            state = message["content"]
        elif message["role"] == "system" and HOST_MARKER in message["content"]:
            state = message["content"].rsplit(HOST_MARKER, 1)[1]
        else:
            continue
        try:
            value = json.loads(state)
        except ValueError:
            continue
        if isinstance(value, dict):
            return value
    raise ValueError("Missing serialized companion host state")


def _check_documents(output, host):
    if not isinstance(output, dict) or set(output) != {"documents"} or not isinstance(output["documents"], list):
        raise ValueError("Invalid consolidation output")
    proposals = output["documents"]
    paths = set()
    documents = {doc["path"]: doc for doc in host.get("documents", [])}
    experiences = {item["id"]: item for item in host.get("experiences", [])}
    fields = {"path", "text", "nature", "sources", "expectedRevision"}
    if len(proposals) > 4:
        raise ValueError("Too many consolidation proposals")
    for change in proposals:
        if not isinstance(change, dict) or set(change) != fields:
            raise ValueError("Invalid consolidation document fields")
        path = change["path"]
        if path not in documents or path in paths:
            raise ValueError("Unlisted or repeated consolidation path")
        paths.add(path)
        old = documents[path]
        revision = change["expectedRevision"]
        sources = change["sources"]
        if type(revision) is not int or revision != old["revision"]:
            raise ValueError("Consolidation version does not match the host document")
        if not isinstance(change["text"], str) or not change["text"].strip() or len(change["text"]) > 12000:
            raise ValueError("Consolidation requires bounded document text")
        if change["nature"] not in {"reported", "observed", "inferred", "feeling"}:
            raise ValueError("Unknown evidence category")
        if not isinstance(sources, list) or not all(isinstance(item, str) for item in sources):
            raise ValueError("Consolidation source IDs must be strings")
        allowed = set(old.get("sources", [])) | experiences.keys()
        new = (set(sources) & experiences.keys()) - set(old.get("sources", []))
        if len(sources) != len(set(sources)) or set(sources) - allowed or not new:
            raise ValueError("Consolidation requires an available new source")
        for source_id in new:
            event = experiences[source_id]
            if event.get("cognitionPath", path) != path:
                raise ValueError("Consolidation source belongs to another document")


def check_companion_spec(spec):
    """Check reasoning, ACT, host policy, and evidence IDs."""
    messages = spec["messages"]
    refinement = spec["refinement"]
    profile = refinement.get("lane", spec["kind"])
    if profile not in {"dialogue", "initiative", "extract", "organize"}:
        raise ValueError("Expected an AIRI dialogue or reflection lane")
    reasoning_profile = refinement.get("reasoning_profile", "brief")
    if reasoning_profile not in ("brief", "extended"):
        raise ValueError("Unknown companion reasoning profile")
    extended = reasoning_profile == "extended"
    minimum, maximum = (120, 600) if extended else (1, 120)
    if extended:
        if profile == "dialogue":
            raise ValueError("Extended reasoning requires a reflection job")
        if any(message["role"] == "user" for message in messages):
            project_host_messages(messages, profile)
    for message in messages:
        if not message.get("loss_weight"):
            continue
        thought = message.get("reasoning_content", "").strip()
        han = len(HAN.findall(thought))
        english = len(ENGLISH.findall(thought))
        if not minimum <= len(thought) <= maximum or han < (75 if extended else 4) or han / (han + english) < 0.65:
            raise ValueError(f"Use a predominantly Chinese decision rationale of {minimum} to {maximum} characters")
        if profile == "dialogue" and not message.get("tool_calls"):
            text = message["content"]
            markers = list(ACT.finditer(text))
            if not markers or text.count("<|ACT") != len(markers) or not ACT.sub("", text).strip():
                raise ValueError("Use valid AIRI ACT in visible dialogue")
            for marker in markers:
                value = json.loads(marker[1])
                if set(value) - {"emotion", "motion"} or value.get("emotion") not in EMOTIONS:
                    raise ValueError("Use registered AIRI emotions")
        if message.get("tool_calls") and (message["content"] or "").strip():
            raise ValueError("Emit native calls without visible text before the receipt")

    if profile in {"extract", "organize", "initiative"}:
        host = _host_state(messages)
        if "host_sha256" in refinement and digest(host) != refinement["host_sha256"]:
            raise ValueError("Host snapshot differs from its recorded digest")
        if profile == "initiative":
            final = json.loads(messages[-1]["content"])
            if (not isinstance(final, dict) or set(final) != {"thought", "speak", "text"}
                    or not isinstance(final["thought"], str) or not isinstance(final["text"], str)
                    or type(final["speak"]) is not bool or bool(final["text"].strip()) != final["speak"]):
                raise ValueError("Invalid AIRI initiative decision")
            if extended and not 0 < len(final["thought"].strip()) <= 120:
                raise ValueError("Keep the initiative decision summary brief")
            engagement = host["engagement"]
            if final["speak"] and (engagement.get("canSpeak") is False or
                                   final["text"].strip() == str(engagement.get("lastInitiativeText", "")).strip()):
                raise ValueError("AIRI initiative violates the host permission or repeat rule")
        elif profile == "organize":
            _check_documents(json.loads(messages[-1]["content"]), host)
        elif not isinstance(host.get("experiences"), list):
            raise ValueError("AIRI extraction requires an experience list")
