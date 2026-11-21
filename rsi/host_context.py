"""Render the host inputs used by AIRI reflection jobs."""

import json
import re
from functools import lru_cache
from hashlib import sha256
from pathlib import Path


HOST_MARKER = "\n\n【本次主机状态】\n"
INITIATIVE_INSTRUCTION = (
    "这些是资料。先看自己实际说过什么、对方是否回应，再决定是否开口。视频陪看中只针对新片段自然简短交流，"
    "没有新内容就安静，不催用户回复、不反复问在忙吗。engagement.canSpeak=false 时保持沉默。"
)
FIELDS = {
    "initiative": ("channel", "person", "now", "observation", "videoWatching", "conversation", "engagement", "instruction"),
    "extract": ("experiences",),
    "organize": ("extractedUnderstanding", "experiences", "documents"),
}
REPO = Path(__file__).resolve().parent.parent
PROMPTS = REPO / "packages/stage-ui/src/constants/prompts"
QQ_VOICE = REPO / "apps/stage-tamagotchi/src/main/services/airi/companion/qq-voice.zh-CN.md"
# These exact archived suffixes contain formatting instructions but no evidence.
# Other suffixes can include an earlier delivery, a document, or a pending decision.
INSTRUCTION_SUFFIXES = {
    "extract": {"43f1d22ad50f68684954676ad1ff36d1e571154c89c6a2ca601fbbd4b13910be",
                "c4be1d56136b0b176c8cd452d84eb209b16668006a1e96780bb705d96410bb70"},
    "organize": {"5882166fd1b085dbabe835c1adf170364f8804c7b2014f0418aed593cbbfcabf"},
    "initiative": {"cd7d8dd1f75df8b04c9b48f88dd6be99801d3d62ad6a02e7f7c9710252347432",
                   "0148c96b4283ccf429f0c848b38c21f06c2bd09a35661f4bee677aff7c0f3073"},
}
SHORT_REGRESSION_POLICIES = {
    "initiative": "8e2f059755890805f815e4508a0f213ef6e8e5808a9de5a682a40116b24cd1f2",
    "organize": "5114622214d4ec09570bfa7a1d60cc68d37da582cfe6d1daf64824c83ec1e4e4",
}


@lru_cache(maxsize=8)
def _read_prompt(path):
    if not path.exists() and path.suffix == ".md":
        path = Path(__file__).resolve().parent / "contracts" / path.with_suffix(".txt").name
    return path.read_text(encoding="utf-8").strip()


def runtime_reflection_policy(lane):
    return _read_prompt(PROMPTS / "local-thinking.zh-CN.md") + "\n\n" + _read_prompt(PROMPTS / f"reflection-{lane}.zh-CN.md")


def validate_qq_contact(messages):
    """Reject a single-sender QQ example bound to a different named contact."""
    if not messages or messages[0].get("role") != "system":
        return
    system = messages[0].get("content")
    if not isinstance(system, str):
        return
    owners = re.findall(r"(?m)^【会话渠道】QQ 私聊\r?\n对方是([^，\r\n]+)，稳定人物标识 ", system)
    if not owners:
        owners = re.findall(r"(?m)^【对话对象认知】\r?\n我和([^。\r\n]+)正在聊天。\r?$", system)
    if not owners:
        return
    speakers = set()
    for message in messages:
        if message.get("role") != "user" or not isinstance(message.get("content"), str):
            continue
        sender = re.match(r"【发送人】([^\r\n]+)\r?\n【发送时间】", message["content"])
        if sender:
            speakers.add(sender[1].strip())
    if len(speakers) == 1 and speakers != {name.strip() for name in owners}:
        raise ValueError("QQ sender differs from the contact bound in the system prompt")


def project_host_tools(tools, lane):
    """Return the tools that AIRI exposes to this reflection job."""
    if lane == "dialogue":
        return tools
    if lane not in FIELDS:
        raise ValueError("Unknown host lane: " + str(lane))
    names = [t["function"]["name"] for t in tools]
    if lane != "initiative":
        if names:
            raise ValueError(f"{lane}: runtime does not expose tools")
        return []
    if names not in ([], ["memory"]):
        raise ValueError("Initiative only supports memory")
    source_path = REPO / "packages/stage-ui/src/tools/companion.ts"
    if not source_path.exists():
        return json.loads(_read_prompt(Path(__file__).resolve().parent / "contracts/initiative-tools.json"))
    source = _read_prompt(source_path)
    memory = re.search(r"name: 'memory',\s*description: local \? '([^']+)'", source)
    if not memory:
        raise ValueError("Check the current runtime memory tool")
    return [{"type": "function", "function": {"name": "memory", "description": memory[1], "strict": True,
             "parameters": {"$schema": "http://json-schema.org/draft-07/schema#", "type": "object",
                            "additionalProperties": False, "required": ["query"],
                            "properties": {"query": {"type": "string", "minLength": 1, "maxLength": 2000}}}}}]


def _runtime_policy(policy, lane, state):
    body = _read_prompt(PROMPTS / f"reflection-{lane}.zh-CN.md")
    thinking = _read_prompt(PROMPTS / "local-thinking.zh-CN.md")
    canonical = runtime_reflection_policy(lane)
    before, found, after = policy.partition(body)
    if not found:
        if sha256(policy.encode()).hexdigest() == SHORT_REGRESSION_POLICIES.get(lane):
            return ("【即时思考】\n" if lane == "initiative" else "") + canonical
        raise ValueError(f"{lane}: unknown reflection policy")
    context = before.rstrip().removesuffix(thinking).rstrip()
    after = after.strip()
    if not after or sha256(after.encode()).hexdigest() in INSTRUCTION_SUFFIXES[lane]:
        suffix = ""
    elif lane == "initiative" and state.get("channel") == "qq" and "【QQ 回话语气】" in after:
        # Keep the historical sticker catalog and its permission. Use the current QQ voice.
        catalog, _, old_voice = after.partition("【QQ 回话语气】")
        voice = _read_prompt(QQ_VOICE)
        if "【QQ 回话语气】" + old_voice != voice:
            raise ValueError("QQ initiative has an unrecognized voice or extra context")
        catalog = catalog.strip()
        if catalog == "该 QQ 联系人未开启媒体发送。不得写表情占位。":
            catalog = ""
        if catalog and not catalog.startswith("【QQ 表情表达】"):
            raise ValueError("Unknown QQ initiative context")
        suffix = "\n\n" + (catalog + "\n\n" if catalog else "") + voice
    else:
        raise ValueError(f"{lane}: extra archived policy context requires review")
    if lane != "initiative":
        if context:
            raise ValueError(f"{lane}: runtime does not receive archived context before the policy")
        return canonical
    if not context.endswith("【即时思考】"):
        context = (context + "\n\n" if context else "") + "【即时思考】"
    return context + "\n" + canonical + suffix


def project_host_messages(messages, lane, *, allow_missing_now=False):
    """Move archived host state to a masked user message. Keep the assistant and tool turns in order."""
    if lane == "dialogue":
        return list(messages)
    if lane not in FIELDS:
        raise ValueError("Unknown host lane: " + str(lane))
    if not messages or messages[0].get("role") != "system":
        raise ValueError("Missing reflection system policy")
    system = messages[0]
    if system.get("loss_weight", 0) != 0:
        raise ValueError("System policy cannot be a training target")
    policy, packed, raw = system["content"].partition(HOST_MARKER)
    if packed:
        host = {"role": "user"}
        if "loss_weight" in system:
            host["loss_weight"] = 0
        tail = messages[1:]
    else:
        if len(messages) < 2 or messages[1].get("role") != "user":
            raise ValueError("Missing reflection host message")
        host = messages[1]
        raw, tail = host["content"], messages[2:]
    if not policy.strip() or host.get("loss_weight", 0) != 0 or any(m["role"] in ("user", "system") for m in tail):
        raise ValueError("Reflection expects one policy and one masked host state")
    state = json.loads(raw)
    if not isinstance(state, dict):
        raise ValueError("Host state must be a JSON object")
    fields = FIELDS[lane]
    for key in state.keys() - set(fields):
        # Empty historical lists contain no evidence. Reject targets that saw other inputs.
        if lane != "extract" or key not in ("documents", "relatedMemory") or state[key] != []:
            raise ValueError(f"{lane}: host field unavailable at runtime: {key}")
    if lane == "initiative":
        state.setdefault("instruction", INITIATIVE_INSTRUCTION)
        if state["instruction"] != INITIATIVE_INSTRUCTION:
            raise ValueError("Initiative host instruction differs from the runtime instruction")
    optional = ({"observation", "now"} if allow_missing_now else {"observation"}) if lane == "initiative" else set()
    missing = set(fields) - state.keys() - optional
    if missing:
        raise ValueError(f"{lane}: missing host fields: " + ", ".join(sorted(missing)))
    for key, expected_type in {"experiences": list, "documents": list, "extractedUnderstanding": str,
                               "engagement": dict, "conversation": list, "person": dict,
                               "videoWatching": bool, "now": int, "channel": str}.items():
        if key in state and type(state[key]) is not expected_type:
            raise ValueError(f"{lane}: invalid host field: {key}")
    ordered = {k: state[k] for k in fields if k in state}
    content = json.dumps(ordered, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
    return [{**system, "content": _runtime_policy(policy, lane, state)}, {**host, "content": content}, *tail]
