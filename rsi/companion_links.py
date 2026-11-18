"""Check that training follow-ups use the previous AIRI output."""

import json

from .common import digest
from .companion_corpus import _host_state


def _check_document_link(previous, current, document):
    proposals = json.loads(previous["messages"][-1]["content"])["documents"]
    for change in proposals:
        expected = {key: change[key] for key in ("path", "text", "sources")}
        expected["revision"] = change["expectedRevision"] + 1
        if document == expected:
            break
    else:
        raise ValueError("Follow-up document differs from the preceding revision")
    context = current["messages"][0]["content"]
    if document["text"] not in context or current["refinement"].get("downstream_context_sha256") != digest(context):
        raise ValueError("Follow-up prompt lacks the applied document")


def _check_extraction_link(previous, current, extraction):
    if (previous["refinement"].get("lane") != "extract" or current["refinement"].get("lane") != "organize"
            or not isinstance(extraction, str) or not extraction.strip() or extraction.strip() == "NO_UPDATE"
            or previous["messages"][-1]["content"] != extraction
            or _host_state(current["messages"]).get("extractedUnderstanding") != extraction):
        raise ValueError("Organizer input differs from the preceding extraction")


def _check_delivery(current, decision, delivery):
    state = delivery["state"]
    if state not in {"delivered", "not_sent", "suppressed"}:
        raise ValueError("Unknown delivery status")
    if state != "delivered":
        if set(delivery) != {"state"} or (state == "suppressed" and decision["speak"]):
            raise ValueError("A suppressed proposal cannot count as delivery")
        text = decision["text"]
        if text and any(message["role"] == "assistant" and message["content"] == text
                        for message in current["messages"]):
            raise ValueError("Undelivered proposal appears in conversation")
        if text and current["refinement"].get("lane") == "initiative":
            history = _host_state(current["messages"]).get("conversation", [])
            if any(event.get("actor") == "assistant" and event.get("text") == text for event in history):
                raise ValueError("Undelivered proposal appears in host history")
        return
    if not decision["speak"] or delivery["text"] != decision["text"]:
        raise ValueError("Delivery cannot contain text from a silent or different decision")
    if current["refinement"].get("lane") == "initiative":
        events = _host_state(current["messages"]).get("conversation", [])
        if not any(event["id"] == delivery["event_id"] and event["actor"] == "assistant"
                   and event["text"] == delivery["text"] for event in events):
            raise ValueError("Delivered initiative is missing from host history")
    elif not any(message["role"] == "assistant" and not message["loss_weight"]
                 and message["content"] == delivery["text"] for message in current["messages"]):
        raise ValueError("Delivered initiative must appear as unsupervised history")


def check_companion_links(specs):
    """Check explicit cross-invocation links after source shards are combined."""
    by_id = {spec["id"]: spec for spec in specs}
    if len(by_id) != len(specs):
        raise ValueError("Duplicate companion trajectory ID")
    checked = {"links": 0, "applied_documents": 0, "recorded_decisions": 0}
    ancestors = set()
    for spec in specs:
        ref = spec["refinement"]
        parent = ref.get("downstream_of")
        if parent is None:
            if "extraction_source" in ref:
                raise ValueError("An extraction link requires a parent")
            ancestors.add(spec["id"])
            continue
        try:
            if parent not in ancestors or by_id[parent]["group"] != spec["group"]:
                raise ValueError("Parent must appear first in the same timeline group")
            previous = by_id[parent]
            if "extraction_source" in ref:
                _check_extraction_link(previous, spec, ref["extraction_source"])
                checked["used_extractions"] = checked.get("used_extractions", 0) + 1
            if "consumed_document" in ref:
                _check_document_link(previous, spec, ref["consumed_document"])
                checked["applied_documents"] += 1
            if "observed_decision" in ref:
                decision = json.loads(previous["messages"][-1]["content"])
                if decision != ref["observed_decision"]:
                    raise ValueError("Follow-up decision differs from the preceding output")
                _check_delivery(spec, decision, ref["previous_delivery"])
                checked["recorded_decisions"] += 1
        except (ValueError, KeyError, TypeError) as error:
            raise ValueError(spec["id"] + ": " + str(error)) from error
        ancestors.add(spec["id"])
        checked["links"] += 1
    return checked
