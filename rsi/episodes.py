"""Versioned experience capture and causal message validation."""

import copy
import datetime
import math
import re
from pathlib import Path

from .common import canonical, digest, exclusive, read_json, write_json


KINDS = {"dialogue", "reflection", "initiative"}
STATUSES = {"received", "finished", "failed", "cancelled"}
MESSAGE_KEYS = {"id", "role", "origin", "content", "status", "available_at", "reasoning_content", "tool_calls", "tool_call_id", "name"}


def episode_hash(episode):
    return digest({k: v for k, v in episode.items() if k not in {"content_sha256", "train_indices", "verification_sha256"}})


def seal_episode(episode):
    result = copy.deepcopy(episode)
    result["content_sha256"] = episode_hash(result)
    validate_episode(result)
    return result


def _nonempty(value, label):
    if not isinstance(value, str) or not value.strip() or len(value) > 4096:
        raise ValueError(label + " must be a nonempty string")


def _number(value, label):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
        raise ValueError(label + " must be a finite nonnegative number")


def validate_tools(tools):
    if not isinstance(tools, list):
        raise ValueError("Tools must be an array")
    names = set()
    for tool in tools:
        if not isinstance(tool, dict) or tool.get("type") != "function" or not isinstance(tool.get("function"), dict):
            raise ValueError("Only named function tools are supported")
        function = tool["function"]
        name = function.get("name")
        if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", name) or name in names:
            raise ValueError("Invalid or duplicate tool name")
        if not isinstance(function.get("parameters"), dict) or function["parameters"].get("type") != "object":
            raise ValueError("Tool parameters must describe an object")
        names.add(name)
    return names


def _arguments(value):
    import json

    if isinstance(value, str):
        value = json.loads(value, parse_constant=lambda _: (_ for _ in ()).throw(ValueError("Nonfinite tool argument")))
    if not isinstance(value, dict):
        raise ValueError("Tool arguments must be an object")
    canonical(value)
    return value


def validate_episode(episode):
    required = {"schema_version", "id", "kind", "scope", "session_id", "source_ids", "occurred_at", "context", "model", "tools", "template_kwargs", "messages", "outcome", "content_sha256"}
    if not isinstance(episode, dict) or not required <= episode.keys() or episode.get("schema_version") != 1:
        raise ValueError("Invalid episode schema")
    if episode["kind"] not in KINDS:
        raise ValueError("Unknown episode kind")
    for name in ("id", "session_id"):
        _nonempty(episode[name], name)
    scope = episode["scope"]
    if not isinstance(scope, dict) or set(scope) != {"user_id", "character_id"}:
        raise ValueError("Scope requires user_id and character_id")
    for value in scope.values():
        _nonempty(value, "Scope identity")
    for name in ("person_id", "goal_id"):
        if name in episode:
            _nonempty(episode[name], name)
    sources = episode["source_ids"]
    if not isinstance(sources, list) or not sources or len(set(sources)) != len(sources):
        raise ValueError("Source lineage must be nonempty and unique")
    for source in sources:
        _nonempty(source, "Source ID")
    _number(episode["occurred_at"], "Occurrence time")
    for name in ("context", "model", "template_kwargs", "outcome"):
        if not isinstance(episode[name], dict):
            raise ValueError(name + " must be an object")
    if episode["outcome"].get("status") not in {"unverified", "verified", "failed", "cancelled"}:
        raise ValueError("Invalid episode outcome")
    context = episode["context"]
    if "snapshot_at" in context:
        _number(context["snapshot_at"], "Snapshot time")
        if context["snapshot_at"] > episode["occurred_at"]:
            raise ValueError("Future context cannot appear in an earlier episode")
    names = validate_tools(episode["tools"])
    messages = episode["messages"]
    if not isinstance(messages, list) or not messages:
        raise ValueError("Episode needs messages")
    ids, calls, pending, has_assistant = set(), set(), {}, False
    last_available = 0
    for message in messages:
        if not isinstance(message, dict) or set(message) - MESSAGE_KEYS:
            raise ValueError("Unknown message field")
        if not {"id", "role", "origin", "content", "status"} <= message.keys():
            raise ValueError("Message metadata is incomplete")
        _nonempty(message["id"], "Message ID")
        if message["id"] in ids:
            raise ValueError("Duplicate message ID")
        ids.add(message["id"])
        role = message["role"]
        origins = {"system": {"application"}, "user": {"human", "application"}, "assistant": {"model"}, "tool": {"tool"}}
        if role not in origins or message["origin"] not in origins[role]:
            raise ValueError("Message role and origin disagree")
        if message["status"] not in STATUSES or not isinstance(message["content"], str):
            raise ValueError("Invalid message content or status")
        if "available_at" in message:
            _number(message["available_at"], "Availability time")
            if message["available_at"] < last_available:
                raise ValueError("Message availability moved backwards")
            last_available = message["available_at"]
        if "reasoning_content" in message and (role != "assistant" or not isinstance(message["reasoning_content"], str)):
            raise ValueError("Only assistant messages contain explicit reasoning")
        if role != "assistant" and message.get("tool_calls"):
            raise ValueError("Only assistant messages call tools")
        if role == "system" and has_assistant:
            raise ValueError("System state belongs before the action trajectory")
        if role == "tool":
            call_id = message.get("tool_call_id")
            if call_id not in pending:
                raise ValueError("Tool result has no matching pending call")
            if message.get("name", pending[call_id]) != pending[call_id]:
                raise ValueError("Tool result name does not match its call")
            del pending[call_id]
            continue
        if pending:
            raise ValueError("Tool results must precede the next model or user message")
        if role == "assistant":
            has_assistant = True
            for call in message.get("tool_calls", []):
                if not isinstance(call, dict) or call.get("type") != "function" or not isinstance(call.get("function"), dict):
                    raise ValueError("Invalid tool call")
                _nonempty(call.get("id"), "Tool call ID")
                if call["id"] in calls or call["function"].get("name") not in names:
                    raise ValueError("Duplicate or undefined tool call")
                _arguments(call["function"].get("arguments"))
                calls.add(call["id"])
                pending[call["id"]] = call["function"]["name"]
            if message["status"] == "finished" and not (message["content"].strip() or message.get("reasoning_content") or message.get("tool_calls")):
                raise ValueError("A finished assistant message cannot be empty")
    if not has_assistant:
        raise ValueError("Episode needs an assistant decision")
    if pending and episode["outcome"]["status"] == "verified":
        raise ValueError("Pending tool calls cannot have a verified outcome")
    if episode["content_sha256"] != episode_hash(episode):
        raise ValueError("Episode content hash mismatch")
    return episode


def _milliseconds(value):
    if isinstance(value, str):
        return int(datetime.datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp() * 1000)
    if isinstance(value, (int, float)):
        return int(value)
    return 0


def import_request(record, *, scope=None, kind="dialogue", source_id=None):
    """Import one exact request prefix. Unknown origin and outcome remain explicit issues."""
    if not isinstance(record, dict):
        raise ValueError("Request record must be an object")
    gateway = "model_input" in record
    request = record.get("model_input") if gateway else record.get("request")
    if not isinstance(request, dict) or not isinstance(request.get("messages"), list):
        raise ValueError("Exact model input is unavailable")
    annotations = record.get("rsi", {})
    if not isinstance(annotations, dict):
        raise ValueError("Invalid RSI annotations")
    selected_scope = scope or annotations.get("scope") or {"user_id": "unknown", "character_id": "unknown"}
    if scope and annotations.get("scope") and scope != annotations["scope"]:
        raise ValueError("Request scope conflicts with the selected scope")
    correlation = record.get("correlation") or {}
    session = annotations.get("session_id") or correlation.get("session_id") or "unlinked"
    source = source_id or "request:" + digest(record)
    response = record.get("response") or {}
    choices = response.get("choices", []) if isinstance(response, dict) else []
    choice = choices[0] if choices else {}
    output = choice.get("message")
    origin_hints = annotations.get("message_origins")
    if origin_hints is not None and (not isinstance(origin_hints, list) or len(origin_hints) != len(request["messages"])):
        raise ValueError("Origin annotations do not match the request messages")
    issues = []
    if selected_scope["user_id"] == "unknown" or selected_scope["character_id"] == "unknown":
        issues.append("scope_unknown")
    if session == "unlinked":
        issues.append("session_unlinked")
    messages = []
    source_messages = list(request["messages"])
    if isinstance(output, dict):
        source_messages.append(output)
    for i, original in enumerate(source_messages):
        role = original.get("role")
        content = original.get("content")
        if content is None and role == "assistant":
            content = ""
        if not isinstance(content, str):
            raise ValueError("Multimodal message requires a modality-specific importer")
        origin = {"system": "application", "user": "application", "assistant": "model", "tool": "tool"}.get(role)
        if i < len(request["messages"]) and origin_hints:
            origin = origin_hints[i]
        elif role == "user":
            issues.append("user_origin_unconfirmed")
        message = {"id": digest([source, i]), "role": role, "origin": origin, "content": content, "status": "finished"}
        for name in ("tool_calls", "tool_call_id", "name", "reasoning_content"):
            if original.get(name) is not None:
                message[name] = copy.deepcopy(original[name])
        messages.append(message)
    success = record.get("status") == "ok" and record.get("http_status") == 200 if gateway else record.get("status") == 200
    finish = choice.get("finish_reason")
    if not isinstance(output, dict):
        issues.append("response_missing")
    elif not success or finish not in {"stop", "tool_calls"}:
        messages[-1]["status"] = "cancelled" if record.get("status") in {"canceled", "cancelled"} else "failed"
        issues.append("response_incomplete")
    if request.get("response_format"):
        issues.append("structured_output_constraints")
    if request.get("tool_choice") not in (None, "auto"):
        issues.append("forced_tool_choice")
    context = copy.deepcopy(annotations.get("context", {}))
    if not isinstance(context, dict):
        raise ValueError("Context annotations must be an object")
    context["correlation"] = copy.deepcopy(correlation)
    context["issues"] = sorted(set(issues) | set(context.get("issues", [])))
    episode = {"schema_version": 1, "id": "episode-" + digest([selected_scope, source]), "kind": kind,
               "scope": copy.deepcopy(selected_scope), "session_id": session, "source_ids": [source],
               "occurred_at": _milliseconds(record.get("startedAt", record.get("started_at"))), "context": context,
               "model": {"requested": request.get("model"), "resolved": response.get("model") if isinstance(response, dict) else None,
                         "provenance": copy.deepcopy(record.get("provenance"))},
               "tools": copy.deepcopy(request.get("tools") or []), "template_kwargs": copy.deepcopy(request.get("chat_template_kwargs") or {}),
               "messages": messages, "outcome": {"status": "unverified", "delivery": "unknown", "finish_reason": finish}}
    for key in ("person_id", "goal_id"):
        if annotations.get(key):
            episode[key] = annotations[key]
    return seal_episode(episode)


class EpisodeStore:
    """Immutable capture files. Callers submit explicit scopes and correlation IDs."""

    def __init__(self, root):
        self.root = Path(root)

    def _path(self, episode_id):
        _nonempty(episode_id, "Episode ID")
        return self.root / "episodes" / (digest(episode_id) + ".json")

    def append(self, episode):
        validate_episode(episode)
        path = self._path(episode["id"])
        with exclusive(self.root / ".capture.lock"):
            if path.exists():
                if read_json(path) != episode:
                    raise ValueError("Episode identity already has different content")
                return False
            write_json(path, episode)
        return True

    def get(self, episode_id):
        path = self._path(episode_id)
        return validate_episode(read_json(path)) if path.exists() else None

    def list(self):
        return [validate_episode(read_json(path)) for path in sorted((self.root / "episodes").glob("*.json"))]

    def audit(self):
        import collections

        episodes = self.list()
        return {"episodes": len(episodes), "by_kind": dict(collections.Counter(e["kind"] for e in episodes)),
                "by_outcome": dict(collections.Counter(e["outcome"]["status"] for e in episodes)),
                "assistant_only": sum(not any(m["role"] == "user" for m in e["messages"]) for e in episodes),
                "tool_calls": sum(len(m.get("tool_calls", [])) for e in episodes for m in e["messages"]),
                "snapshot_sha256": digest(sorted(e["content_sha256"] for e in episodes))}
