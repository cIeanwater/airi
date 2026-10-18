"""Read saved model requests for one message without reconstructing its context."""

import json
import math
import re
import stat
import time
from pathlib import Path


RECORD_FILENAME = re.compile(r"\d{8}T\d{12}Z-(chatcmpl-[0-9a-f]{32})\.json")
CONTROL_MARKER = re.compile(r"<\|(ACT|DELAY) ([^\n]*?)\|>")
QUERY_FIELDS = {"message_id", "session_id", "content", "created_at"}
MAX_RECORDS = 1000
MAX_RECORD_BYTES = 32 * 1024 * 1024
HEADER_FIELDS = {"headers", "httpheaders", "requestheaders", "responseheaders", "authorization", "proxyauthorization"}


def reject_nonfinite_json(value: str) -> None:
    raise ValueError(f"JSON numbers must be finite, received {value}")


def _finite_number(value) -> bool:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    try:
        return math.isfinite(value)
    except OverflowError:
        return False


def validate_message_context_query(query: dict) -> None:
    """Reject filenames, invalid timestamps, and queries without a message identity."""
    if not isinstance(query, dict) or not query or set(query) - QUERY_FIELDS:
        raise ValueError("Use only message_id, session_id, content, and created_at in a message context query")
    for key in ("message_id", "session_id", "content"):
        if key not in query:
            continue
        value = query[key]
        maximum = 1024 * 1024 if key == "content" else 512
        if not isinstance(value, str) or not value or len(value) > maximum:
            raise ValueError(f"{key} must be a nonempty string of at most {maximum} characters")
    if "created_at" in query:
        timestamp = query["created_at"]
        if not _finite_number(timestamp) or timestamp < 0:
            raise ValueError("created_at must be a finite, nonnegative timestamp in milliseconds")
    if "message_id" not in query and "content" not in query:
        raise ValueError("A message context query requires message_id or content")


def _unavailable(reason: str) -> dict:
    return {"status": "unavailable", "reason": reason}


def _is_link(path: Path) -> bool:
    attributes = path.lstat()
    return stat.S_ISLNK(attributes.st_mode) or bool(getattr(attributes, "st_file_attributes", 0) & 0x400)


def _read_record(path: Path, directory: Path, cutoff: float) -> dict | None:
    """Ignore incomplete, expired, damaged, oversized, or redirected record files."""
    match = RECORD_FILENAME.fullmatch(path.name)
    if match is None:
        return None
    try:
        if _is_link(path) or path.resolve().parent != directory:
            return None
        attributes = path.stat()
        if not stat.S_ISREG(attributes.st_mode) or attributes.st_mtime < cutoff or attributes.st_size > MAX_RECORD_BYTES:
            return None
        record = json.loads(path.read_text(encoding="utf-8"), parse_constant=reject_nonfinite_json)
    except (OSError, UnicodeError, ValueError, RecursionError):
        return None
    if not isinstance(record, dict) or record.get("schema_version") != 1 or record.get("request_id") != match.group(1):
        return None
    timestamp = record.get("started_at_unix")
    if not _finite_number(timestamp):
        return None
    # HTTP headers are not part of MessageRecorder's request JSON schema.
    return {key: value for key, value in record.items() if key.lower().replace("_", "").replace("-", "") not in HEADER_FIELDS}


def _without_control_markers(text: str) -> str:
    """Remove only valid AIRI action and delay markers. Keep ordinary text exact."""
    def replace(match: re.Match) -> str:
        name, payload = match.groups()
        if name == "DELAY":
            if re.fullmatch(r"\d+(?:\.\d+)?", payload.strip()):
                delay = float(payload)
                if math.isfinite(delay) and delay > 0:
                    return ""
            return match.group(0)
        try:
            action = json.loads(payload, parse_constant=reject_nonfinite_json)
        except (ValueError, RecursionError):
            return match.group(0)
        return "" if isinstance(action, dict) else match.group(0)

    return CONTROL_MARKER.sub(replace, text)


def _output_content(record: dict) -> str | None:
    response = record.get("response")
    if isinstance(response, dict):
        choices = response.get("choices")
        if isinstance(choices, list):
            for choice in choices:
                if not isinstance(choice, dict) or choice.get("index") != 0:
                    continue
                message = choice.get("message")
                if isinstance(message, dict) and message.get("role") == "assistant" and isinstance(message.get("content"), str):
                    return message["content"]
    output = record.get("model_output_text")
    return output if isinstance(output, str) else None


def read_local_message_context(directory: Path | None, query: dict, *, max_age_days: int = 7, max_records: int = MAX_RECORDS) -> dict:
    """Return exact saved records, or explain why no unique record is available.

    The caller supplies its configured record directory. The query cannot select a path.
    Identity lookup includes tool rounds. Content lookup supports records without correlation.
    Files that no longer meet retention limits are unavailable. No history is reconstructed.
    """
    validate_message_context_query(query)
    if isinstance(max_age_days, bool) or not isinstance(max_age_days, int) or max_age_days < 1:
        raise ValueError("Message retention days must be a positive integer")
    if isinstance(max_records, bool) or not isinstance(max_records, int) or max_records < 1:
        raise ValueError("Message retention files must be a positive integer")
    if directory is None:
        return _unavailable("recording-disabled")
    if not isinstance(directory, Path):
        raise ValueError("The configured record directory must be a Path")
    try:
        if _is_link(directory) or not directory.is_dir():
            return _unavailable("not-recorded")
        approved_directory = directory.resolve()
        paths = sorted((path for path in approved_directory.glob("*-chatcmpl-*.json") if RECORD_FILENAME.fullmatch(path.name)),
                       key=lambda path: path.name, reverse=True)[:min(max_records, MAX_RECORDS)]
    except OSError:
        return _unavailable("not-recorded")
    cutoff = time.time() - max_age_days * 86400
    records = []
    for path in paths:
        record = _read_record(path, approved_directory, cutoff)
        if record is not None:
            records.append(record)

    if "message_id" in query:
        matches = []
        for record in records:
            correlation = record.get("correlation")
            if not isinstance(correlation, dict) or correlation.get("message_id") != query["message_id"]:
                continue
            if "session_id" in query and correlation.get("session_id") != query["session_id"]:
                continue
            if not all(isinstance(correlation.get(key), str) and correlation[key] for key in ("message_id", "session_id", "turn_id")):
                continue
            matches.append(record)
        if matches:
            identities = {(record["correlation"]["session_id"], record["correlation"]["turn_id"]) for record in matches}
            if len(identities) != 1:
                return _unavailable("ambiguous")
            matches.sort(key=lambda record: (record["started_at_unix"], record["request_id"]))
            return {"status": "found", "match": "id", "records": matches}

    if "content" not in query or "created_at" not in query:
        return _unavailable("not-recorded")
    expected_content = _without_control_markers(query["content"])
    matches = []
    for record in records:
        if record.get("correlation") is not None:
            continue
        if abs(record["started_at_unix"] * 1000 - query["created_at"]) > 120_000:
            continue
        content = _output_content(record)
        if content is not None and _without_control_markers(content) == expected_content:
            matches.append(record)
    if len(matches) > 1:
        return _unavailable("ambiguous")
    if matches:
        return {"status": "found", "match": "content", "records": matches}
    return _unavailable("not-recorded")
