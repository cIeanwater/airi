"""Build multi-turn SFT rows and bind tool results to their source."""

import copy
import html
import re

from .cleaning import SECRET, URL_CREDENTIAL, _messages
from .common import canonical, digest
from .dataset import compile_messages
from .episodes import _arguments


RECEIPT_SOURCES = {"observed", "replay", "executed"}
NATIVE_MARKUP = re.compile(r"<(?:\|(?:turn|channel|tool_call|tool_response)|(?:turn|channel|tool_call|tool_response)\|)>")
JSON_TYPES = {
    "null": (type(None),), "boolean": (bool,), "integer": (int,), "number": (int, float),
    "string": (str,), "array": (list,), "object": (dict,),
}


def receipt_evidence(call, tool_message, *, source, source_id):
    """Bind the original tool call and result to a source record."""
    if source not in RECEIPT_SOURCES or not isinstance(source_id, str) or not source_id.strip():
        raise ValueError("A tool result needs a source and record ID")
    if (tool_message.get("role") != "tool" or tool_message.get("tool_call_id") != call["id"]
            or not isinstance(tool_message.get("content"), str)
            or tool_message.get("status", "finished") != "finished"):
        raise ValueError("Tool result does not match the call")
    function = call["function"]
    return {"call_id": call["id"], "name": function["name"],
            "arguments_sha256": digest(_arguments(function["arguments"])),
            "content_sha256": digest(tool_message["content"]), "source": source, "source_id": source_id}


def replay_receipt(session, call, *, source_id):
    """Run one argument-bound replay step and return its message and evidence."""
    bound_call = copy.deepcopy(call)
    bound_call["function"]["arguments"] = _arguments(bound_call["function"]["arguments"])
    result = session.execute(bound_call)
    tool_message = {"role": "tool", "tool_call_id": call["id"],
                    "content": result if isinstance(result, str) else canonical(result).decode("utf-8"),
                    "loss_weight": 0}
    evidence = receipt_evidence(bound_call, tool_message, source="replay", source_id=source_id)
    evidence["environment_sha256"] = session.environment_sha256
    return tool_message, evidence


def capture_request(record, *, source_id):
    """Read a completed request with its full visible causal prefix."""
    if not isinstance(source_id, str) or not source_id.strip():
        raise ValueError("A captured request needs a source ID")
    if not isinstance(record, dict):
        raise ValueError("A captured request must be an object")
    gateway = "model_input" in record
    request = record.get("model_input" if gateway else "request")
    if not isinstance(request, dict) or not isinstance(request.get("messages"), list):
        raise ValueError("Capture requires the effective request messages")
    response = record.get("response") or {}
    choices = response.get("choices") or [] if isinstance(response, dict) else []
    choice = choices[0] if choices else {}
    successful = record.get("status") == "ok" and record.get("http_status") == 200 if gateway else record.get("status") == 200
    if not successful or not isinstance(choice, dict) or choice.get("finish_reason") != "stop":
        raise ValueError("Capture requires a completed response with all tool results")
    output = choice.get("message")
    if not isinstance(output, dict) or output.get("role", "assistant") != "assistant":
        raise ValueError("Capture requires a completed final answer")
    tools = copy.deepcopy(request.get("tools") or [])
    messages = _messages([*request["messages"], {**output, "role": "assistant"}], tools)
    if not messages[-1]["content"].strip():
        raise ValueError("Capture requires a completed final answer")
    evidence = []
    calls = {}
    for message in messages:
        message["loss_weight"] = 0
        calls.update((call["id"], call) for call in message.get("tool_calls", []))
        if message["role"] == "tool":
            evidence.append(receipt_evidence(calls[message["tool_call_id"]], message, source="observed", source_id=source_id))
    return {"source_id": source_id, "request_sha256": digest(record), "messages": messages,
            "tools": tools, "template_kwargs": copy.deepcopy(request.get("chat_template_kwargs") or {}), "evidence": evidence}


def _check_arguments(call, schema):
    arguments = _arguments(call["function"]["arguments"])
    properties = schema.get("properties", {})
    missing = set(schema.get("required", [])) - arguments.keys()
    unknown = arguments.keys() - properties.keys() if schema.get("additionalProperties") is False else set()
    if missing or unknown:
        raise ValueError("Tool arguments do not match required or allowed keys: " + call["function"]["name"])
    for key, value in arguments.items():
        field = properties.get(key, {})
        declared = field.get("type")
        if isinstance(declared, str) and declared in JSON_TYPES and type(value) not in JSON_TYPES[declared]:
            raise ValueError("Tool argument has the wrong type: " + key)
        if declared == "string" and not field.get("minLength", 0) <= len(value) <= field.get("maxLength", float("inf")):
            raise ValueError("Tool argument has the wrong length: " + key)


def _weighted_messages(messages, tools):
    clean = _messages(messages, tools)
    schemas = {tool["function"]["name"]: tool["function"]["parameters"] for tool in tools}
    targets = []
    for i, (raw, message) in enumerate(zip(messages, clean)):
        weight = raw.get("loss_weight")
        if type(weight) is not int or weight not in (0, 1) or weight == 1 and message["role"] != "assistant":
            raise ValueError("Use binary loss weights and supervise only assistant turns")
        if message["role"] == "tool" and raw.get("status", "finished") != "finished":
            raise ValueError("Keep completed tool results in the trajectory")
        if weight:
            if raw.get("status", "finished") != "finished":
                raise ValueError("An unfinished assistant turn cannot be supervised")
            if not message["content"].strip() and not message.get("tool_calls"):
                raise ValueError("A supervised turn needs an answer or a tool call")
            rationale = message.get("reasoning_content", "")
            if not re.search(r"[\u3400-\u9fff]", rationale):
                raise ValueError("A supervised turn needs a Chinese decision rationale")
            for call in message.get("tool_calls", []):
                _check_arguments(call, schemas[call["function"]["name"]])
            targets.append(i)
        for key in ("content", "reasoning_content"):
            if NATIVE_MARKUP.search(message.get(key, "")):
                raise ValueError("Place native channels and tool calls in their structured fields")
        message["loss_weight"] = weight
    if not targets:
        raise ValueError("Select at least one assistant target")
    if clean[-1]["role"] != "assistant" or clean[-1].get("tool_calls") or not clean[-1]["content"].strip():
        raise ValueError("Finish the trajectory with an assistant answer")
    return clean, targets


def build_trajectory(messages, tools, *, evidence, template_kwargs=None):
    """Check a refined trajectory and return a compact SFT row."""
    clean, _ = _weighted_messages(messages, tools)
    if not isinstance(evidence, list) or any(not isinstance(receipt, dict) for receipt in evidence):
        raise ValueError("Tool evidence must be a list of source records")
    by_id = {receipt.get("call_id"): receipt for receipt in evidence}
    if len(by_id) != len(evidence):
        raise ValueError("Tool evidence includes a duplicate call")
    calls = {}
    for message in clean:
        calls.update((call["id"], call) for call in message.get("tool_calls", []))
        if message["role"] != "tool":
            continue
        call_id = message["tool_call_id"]
        proof = by_id.pop(call_id, None)
        if not proof:
            raise ValueError("No source evidence for tool call " + call_id)
        expected = receipt_evidence(calls[call_id], message, source=proof.get("source"), source_id=proof.get("source_id"))
        if any(proof.get(key) != value for key, value in expected.items()):
            raise ValueError("Tool evidence does not match the refined call or result: " + call_id)
    if by_id:
        raise ValueError("Tool evidence includes an unused call")
    kwargs = {"enable_thinking": True} if template_kwargs is None else template_kwargs
    if (not isinstance(kwargs, dict) or set(kwargs) - {"enable_thinking", "preserve_thinking"}
            or any(type(value) is not bool for value in kwargs.values())):
        raise ValueError("Only native thinking settings belong in a trajectory")
    row = {"messages": clean, "tools": copy.deepcopy(tools), "template_kwargs": copy.deepcopy(kwargs)}
    rendered = html.unescape(canonical(row).decode("utf-8"))
    if SECRET.search(rendered) or URL_CREDENTIAL.search(rendered):
        raise ValueError("The trajectory contains a credential")
    return row


def compile_trajectory(row, tokenizer, max_length):
    """Compile each selected assistant with the native causal context."""
    tools = row.get("tools", [])
    messages, targets = _weighted_messages(row["messages"], tools)
    return [compile_messages(messages, tokenizer, max_length, tools=tools, train_indices=[target],
                             template_kwargs=row.get("template_kwargs", {})) for target in targets]
