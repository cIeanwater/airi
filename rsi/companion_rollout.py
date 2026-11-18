"""Re-run companion trajectories on a local actor before refining SFT targets."""

import argparse
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import json
from pathlib import Path
import re
import time
import unicodedata
from urllib.error import HTTPError
from urllib.parse import urlparse
from urllib.request import ProxyHandler, Request, build_opener

from .common import digest, file_hash, read_json, write_json, write_jsonl
from .companion_corpus import ACT, _host_state, check_companion_spec
from .host_context import project_host_messages, project_host_tools
from .rollout import NoRedirect, assemble_shards
from .trajectory import build_trajectory, receipt_evidence


MODEL = "gemma4-e2b-int4"
BATCHES = {
    "v3": "companion-v3/e2b-sft-v3-final",
    "v4": "companion-v4/e2b-sft-v4-final",
    "v5": "companion-v5/e2b-self-thinking-long-final",
}
STYLE = "【日常说话】\n先用简体中文想一句，再像熟人聊天一样答。通常一两句，直接接住对方刚说的事。查资料时把来源链接留在工具回执里。只说资料支持的内容，缺线索就说拿不准。"
EXAM = re.compile(r"回到[「『《\"].{1,100}[」』》\"].{0,32}(?:回一句|方式|回答)|按.{0,12}(?:后来说的方式|练习方式)|(?:按|从)(?:摘录|原文|资料).{0,12}(?:找那一条|逐字|抄给我)|再帮我把原句贴|(?:定位线索|原题|原文依据|这道题|第[一二三四1-4]小题|参考答案|照抄|逐字|公开训练摘录|(?:找|补|带|标|附).{0,6}出处|按.{0,10}格式回答)|(?:^|\n)(?:摘录|问题|答案)[：:]|(?:怎么写|写的什么|旧值)[？?]?$", re.S)
DEPENDENT = re.compile(r"^(?:那|哦|嗯[，,]|好[，,]|对[，,]|行[，,]|上次|刚刚你|刚才你|上一轮|之前你|等等|还是按|顺便|再帮)")
FILLER = re.compile(r"^(?:我(?:想|在|正在).{0,35}(?:整理|做|翻|找)|.{0,20}(?:要做|在做).{0,22}(?:小卡|记录|笔记|书签)).{0,24}[。！!！]$")
LINK = re.compile(r"!?\[[^\]\n]{0,100}\]\(\s*https?://[^)\s]+\s*\)|(?:https?://|www\.)[^\s<>）】\]]+|\[(?:\d+(?:,\s*\d+)*|出处|来源|资料卡|档案出处)\]", re.I)
AD = re.compile(r"^(?:[【\[(（]\s*)?(?:广告|赞助内容|商业推广|sponsored|advertisement)(?:\s|[：:）)】\]]|$)", re.I)
FACT = re.compile(r"\d+(?:[.·:/-]\d+)*(?:年|月|日|分|时|点|人|元|度|%|倍|公里)?")
HAN = re.compile(r"[\u3400-\u9fff]")
PIN = "companion-v4-people-weather-spoiler-downstream"
PIN_REPLIES = ("歪歪挂着的小风向标，还挺有个性呀。", "好呀，你慢慢摸，我先不往后猜。")
TOOL_STYLE = "检索公开资料。query 写和当前问题相关的短关键词。最多返回三条有摘要的结果。链接留在回执里，按证据直接用口语回答。"
HOST_PROFILE = "runtime-host-v1"


def compact(text):
    return re.sub(r"[^\w\u3400-\u9fff]+", "", unicodedata.normalize("NFKC", text).lower())


def user_shape(queries):
    return tuple(compact(re.sub(r"[「『《][^」』》]{1,90}[」』》]|\d+(?:\.\d+)?", "◇", text)) for text in queries)


def clean_reply(text):
    text = LINK.sub("", ACT.sub("", text)).strip()
    text = re.sub(r"\s+([，。！？])", r"\1", text)
    text = re.sub(r"(?:这条是刚查来的|拿来列旧资料就好|范围就留在这个摘录里|这是旧快照)[。！]?\s*$", "", text)
    for pattern, replacement in (
        (r"^我对到了，(?:这|评分这)项在档里是", "旧档写的是"),
        (r"^这格有字呀，", "哦，"),
        (r"^旧资料在「([^」]+)」这一格写的是", r"旧资料里\1是"),
        (r"^(?:根据|按照|按)这份(?:档案|资料)，", "老资料里，"),
        (r"^(?:根据|通过)此次(?:搜索|检索|查询)[，：:]", ""),
        (r"^回执里这句是[：:]", "原话是"),
    ):
        text = re.sub(pattern, replacement, text)
    return text.strip()


def evidence_search(content, query):
    old = json.loads(content)
    seen_urls, seen_text, results = set(), set(), []
    for item in old.get("results", []):
        url, title, snippet = (item.get(key, "").strip() for key in ("url", "title", "snippet"))
        identity = compact(snippet)
        parsed = urlparse(url)
        if (parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password
                or parsed.hostname.endswith(("doubleclick.net", "googleadservices.com", "googlesyndication.com"))
                or AD.search(title) or AD.search(snippet) or not title or len(identity) < 32
                or parsed._replace(fragment="").geturl() in seen_urls or identity in seen_text):
            continue
        seen_urls.add(parsed._replace(fragment="").geturl())
        seen_text.add(identity)
        results.append({"title": title[:250], "url": url, "snippet": snippet[:1200]})
        if len(results) == 3:
            break
    if old.get("results") and not results:
        raise ValueError("search_evidence_too_thin")
    return json.dumps({**old, "query": query, "results": results, "reliability": "这是与原语料绑定的模拟回执。摘要是证据，链接供工具内核对。"}, ensure_ascii=False, separators=(",", ":"))


def prepare(original, proof, *, single=False):
    row = deepcopy(original)
    messages = row["messages"]
    dialogue = proof["refinement"].get("lane", "dialogue") == "dialogue"
    if dialogue:
        users = [i for i, msg in enumerate(messages) if msg["role"] == "user"]
        if any(EXAM.search(messages[i]["content"]) for i in users):
            raise ValueError("scripted_quiz")
        if single:
            if not users or DEPENDENT.search(messages[users[0]]["content"]) or FILLER.search(messages[users[0]]["content"]):
                raise ValueError("dependent_or_filler_first_turn")
            messages = messages[:users[1]] if len(users) > 1 else messages
            if not messages[-1].get("loss_weight"):
                raise ValueError("history_only_first_turn")
            row["messages"] = messages
        messages[0]["content"] += "\n\n" + STYLE
    else:
        host = project_host_messages(messages, proof["refinement"]["lane"])[1]
        if EXAM.search(host["content"]):
            raise ValueError("scripted_quiz_host_event")
    calls = {call["id"]: call for msg in messages for call in msg.get("tool_calls", [])}
    for tool in row["tools"]:
        if tool["function"]["name"] == "search":
            tool["function"]["description"] = TOOL_STYLE
    for msg in messages:
        if msg["role"] != "tool":
            continue
        call = calls[msg["tool_call_id"]]["function"]
        if call["name"] in {"screen_read", "video_read"}:
            raise ValueError("no_pixels_available_for_rollout")
        if call["name"] == "search":
            msg["content"] = evidence_search(msg["content"], call["arguments"]["query"])
    return row


def read_candidates(root):
    for batch, path in BATCHES.items():
        folder = root / path
        manifest = read_json(folder / "manifest.json")
        training = folder / "train.jsonl"
        proofs = folder / "provenance.jsonl"
        if file_hash(training) != manifest["files"]["train"] or file_hash(proofs) != manifest["provenance_sha256"]:
            raise ValueError("Final source corpus has changed: " + path)
        records = (r for r in manifest["records"] if r["split"] == "train")
        with training.open(encoding="utf-8") as train, proofs.open(encoding="utf-8") as audit:
            for record, raw, provenance in zip(records, train, audit, strict=True):
                row, proof = json.loads(raw), json.loads(provenance)
                if record["id"] != proof["id"] or digest(row) != record["content_sha256"]:
                    raise ValueError("Training and provenance are not aligned")
                if batch != "v3" or record["kind"] == "dialogue":
                    yield batch, record, row, proof, manifest["scope"]


def select(root, output, per_category, template_limit, extra):
    pool, rejected = defaultdict(list), Counter()
    def candidates():
        yield from read_candidates(root)
        if extra:
            with extra.open(encoding="utf-8") as source:
                for line in source:
                    spec = json.loads(line)
                    row = build_trajectory(spec["messages"], spec["tools"], evidence=spec["evidence"],
                                           template_kwargs=spec["template_kwargs"])
                    yield "extra", {**spec, "content_sha256": digest(row)}, row, spec, spec["scope"]
    for batch, record, row, proof, scope in candidates():
        lane = proof["refinement"].get("lane", record["kind"])
        proof = {**proof, "refinement": {**proof["refinement"], "lane": lane}}
        user_count = sum(msg["role"] == "user" for msg in row["messages"]) if lane == "dialogue" else 0
        buckets = ("single", "multi") if user_count > 1 else ("single",) if lane == "dialogue" else (lane,)
        for bucket in buckets:
            try:
                prepared = prepare(row, proof, single=bucket == "single")
            except (KeyError, ValueError) as error:
                rejected[f"{bucket}:{error}"] += 1
                continue
            queries = [msg["content"] for msg in prepared["messages"] if lane == "dialogue" and msg["role"] == "user"]
            first = queries[0] if queries else ""
            pool[bucket].append({"batch": batch, "id": record["id"], "group": record["group"], "kind": record["kind"],
                                 "scope": scope, "source_ids": record["source_ids"], "row": prepared, "bucket": bucket,
                                 "source_sha256": record["content_sha256"], "refinement": proof["refinement"],
                                 "original_evidence": proof["evidence"], "dialogue_start": compact(first),
                                 "dialogue_shape": user_shape(queries)})
    chosen, ids, inputs, selected = {}, set(), set(), []
    shapes = Counter()
    quotas = {"single": per_category, "multi": per_category, "initiative": per_category - 2 * (per_category // 3),
              "extract": per_category // 3, "organize": per_category // 3}
    for bucket in ("multi", "single", "initiative", "extract", "organize"):
        def priority(item):
            return (item["id"] != PIN, item["batch"] != "v5" if bucket not in {"single", "multi"} else 0,
                    item["batch"] != "v3" if bucket == "single" else 0,
                    digest(item["id"]))
        kept = []
        for candidate in sorted(pool[bucket], key=priority):
            key, first, shape = candidate["id"], candidate["dialogue_start"], candidate["dialogue_shape"]
            if key in ids or first and first in inputs or (bucket == "single" and key == PIN):
                continue
            if first:
                if shapes[shape] >= template_limit:
                    rejected[f"{bucket}:repeated_query_template"] += 1
                    continue
                shapes[shape] += 1
                inputs.add(first)
            ids.add(key)
            kept.append(candidate)
            if len(kept) == quotas[bucket]:
                break
        chosen[bucket] = len(kept)
        selected.append(kept)
    queue = [row for i in range(max(chosen.values())) for bucket in selected for row in bucket[i:i + 1]]
    write_jsonl(output, queue)
    write_json(output.with_suffix(".report.json"), {"selected": chosen, "available_before_cross_bucket_dedup": {k: len(v) for k, v in pool.items()},
                                                        "rejected": dict(rejected), "source_batches": dict(Counter(r["batch"] for r in queue)),
                                                        "query_template_limit": template_limit,
                                                        "tool_rows": dict(Counter(r["bucket"] for r in queue if any(m["role"] == "tool" for m in r["row"]["messages"]))),
                                                        "file_sha256": file_hash(output)})
    return chosen


def clean_messages(messages):
    result = []
    for original in messages:
        message = {k: v for k, v in original.items() if k in {"role", "content", "tool_calls", "tool_call_id", "reasoning_content"}}
        if "tool_calls" in message:
            message["tool_calls"] = [{**call, "function": {**call["function"], "arguments": json.dumps(call["function"]["arguments"], ensure_ascii=False)
                                                            if isinstance(call["function"]["arguments"], dict) else call["function"]["arguments"]}}
                                     for call in message["tool_calls"]]
        result.append(message)
    return result


def ask(endpoint, history, tools, budget):
    payload = {"messages": clean_messages(history), "model": MODEL, "tools": tools,
               "chat_template_kwargs": {"enable_thinking": True}, "temperature": 0, "max_tokens": budget, "cache_prompt": True}
    req = Request(endpoint + "/v1/chat/completions", json.dumps(payload).encode(), headers={"Content-Type": "application/json"})
    opener = build_opener(ProxyHandler({}), NoRedirect())
    try:
        with opener.open(req, timeout=150) as response:
            result = json.load(response)
    except HTTPError as error:
        if error.code not in {400, 404, 413, 422}:
            raise
        with error:
            body = error.read(4096)
        try:
            details = json.loads(body)
            detail = details.get("error", details)
            if isinstance(detail, dict):
                detail = detail.get("message") or detail.get("type") or error.reason
        except (AttributeError, ValueError):
            detail = error.reason
        raise ValueError(f"Local E2B HTTP {error.code}: {str(detail)[:400]}") from error
    if result["model"] != MODEL or result["choices"][0]["message"].get("role") != "assistant":
        raise ValueError("Unexpected model response")
    choice = result["choices"][0]
    return {"finish_reason": choice["finish_reason"], "message": choice["message"], "usage": result.get("usage", {})}


def keywords(text):
    words = re.findall(r"[a-z0-9]+|[\u3400-\u9fff]+", text.lower())
    return {part for word in words for part in ([word] if not HAN.search(word) or len(word) < 3 else (word[i:i+2] for i in range(len(word)-1)))}


def matching_calls(actual, expected):
    if len(actual) != len(expected):
        return False
    for got, want in zip(actual, expected, strict=True):
        if got.get("function", {}).get("name") != want["function"]["name"]:
            return False
        try:
            query = json.loads(got["function"]["arguments"])["query"]
        except (TypeError, KeyError, ValueError):
            return False
        if not isinstance(query, str) or len(query) > (400 if want["function"]["name"] == "search" else 2000):
            return False
        first, second = keywords(query), keywords(want["function"]["arguments"]["query"])
        if first != second and (len(first & second) < 2 or len(first & second) / max(1, len(first | second)) < 0.48):
            return False
    return True


def refined_dialogue(reference, generated, evidence_prefix):
    old = clean_reply(reference["content"])
    text = clean_reply(generated.get("content") or "")
    context = "\n".join(msg["content"] or "" for msg in evidence_prefix)
    old_words = keywords(old)
    words = keywords(text)
    new_numbers = set(FACT.findall(text)) - set(FACT.findall(old + "\n" + context))
    novel_quote = any(compact(q) not in compact(old + context) for q in re.findall(r"[「“『]([^」”』]{2,})[」”』]", text))
    factual = any(msg["role"] == "tool" for msg in evidence_prefix) or bool(FACT.search(old))
    close = len(words & old_words) / max(1, len(words | old_words)) >= (0.75 if factual else 0.54)
    negations = re.compile(r"(?:没有|没查到|不能|不确定|不想|不会|不是|没说|还没|不必)")
    same_negation = set(negations.findall(text)) == set(negations.findall(old))
    unsupported_action = re.search(r"(?:我|咱们)(?:刚|已经|之前|刚才|都|也).{0,8}(?:查到|见过|玩过|去过|找到|记得|下载|买到|写入|发给)", text)
    stilted = re.search(r"^当然可以|作为.{0,5}助手|(?:根据|基于)(?:以上|提供的|搜索)|(?:具体如下|希望对你有帮助|我可以帮你|需要我为你)", text)
    if (not text or len(text) > 110 or LINK.search(text) or not close or not same_negation or new_numbers
            or novel_quote or stilted or unsupported_action and not unsupported_action.group(0) in old):
        text = old
        decision = "restored_grounded_reference" if old != clean_reply(generated.get("content") or "") else "accepted_actor_wording"
    else:
        decision = "accepted_actor_wording"
    marker = ACT.search(reference["content"])
    if not text or LINK.search(text) or not marker:
        raise ValueError("missing_grounded_conversational_reply")
    return {**reference, "content": f"{marker[0]} {text}"}, decision


def refine(candidate, endpoint):
    row = deepcopy(candidate["row"])
    lane = candidate["refinement"]["lane"]
    row["tools"] = project_host_tools(row["tools"], lane)
    original_proofs = {p["call_id"]: p for p in candidate["original_evidence"]}
    original = project_host_messages(row["messages"], lane)
    host_proof = {}
    if lane != "dialogue":
        prior_host = digest(_host_state(row["messages"]))
        if candidate["refinement"].get("host_sha256", prior_host) != prior_host:
            raise ValueError("Source host snapshot differs from its recorded digest")
        host_proof = {"source_host_sha256": prior_host, "host_sha256": digest(_host_state(original)),
                      "host_profile": HOST_PROFILE}
    history, trace, proofs = [], [], []
    pending, turn = None, 0
    for i, msg in enumerate(original):
        if msg["role"] != "assistant" or not msg.get("loss_weight"):
            history.append(msg)
            continue
        if pending is None:
            budget = {"dialogue": 68, "initiative": 100, "extract": 112, "organize": 220}[lane]
            observed = ask(endpoint, history, row["tools"], budget)
        else:
            observed, pending = pending, None
        message = observed["message"]
        tools = msg.get("tool_calls", [])
        actual = message.get("tool_calls") or []
        event = {"target_index": i, "actor": observed}
        if tools:
            receipts = original[i+1:i+1+len(tools)]
            matched = matching_calls(actual, tools)
            event["decision"] = "replayed_actor_call" if matched else "corrected_tool_call"
            for call, receipt in zip(tools, receipts, strict=True):
                proof = original_proofs[call["id"]]
                proofs.append({**receipt_evidence(call, receipt, source=proof["source"], source_id=proof["source_id"]),
                               "original_receipt_sha256": proof["content_sha256"]})
            if matched:
                replay = [{**receipt, "tool_call_id": call["id"]} for call, receipt in zip(actual, receipts, strict=True)]
                pending = ask(endpoint, [*history, message, *replay], row["tools"], budget)
        elif lane == "dialogue":
            corrected, answer = refined_dialogue(msg, message, history)
            if observed["finish_reason"] != "stop" or actual:
                corrected, _ = refined_dialogue(msg, {}, history)
                answer = "corrected_incomplete_or_extra_tool"
            if candidate["id"] == PIN:
                marker = ACT.search(corrected["content"])
                corrected["content"] = f"{marker[0]} {PIN_REPLIES[turn]}"
                answer = "context_facts_only_review"
            msg = corrected
            event["decision"] = answer
            turn += 1
        else:
            target = msg["content"]
            generated = message.get("content") or ""
            if lane in {"initiative", "organize"}:
                try:
                    matches = json.loads(generated) == json.loads(target)
                except (TypeError, ValueError):
                    matches = False
            else:
                matches = compact(generated) == compact(target)
            event["decision"] = "matched_reflection_target" if matches and not actual else "corrected_reflection_target"
        history.append(msg)
        trace.append(event)
    row["messages"] = history
    provenance = {"lane": lane, **host_proof, **{k: candidate["refinement"][k] for k in ("reasoning_profile",) if k in candidate["refinement"]},
                  "prior_id": candidate["id"], "prior_sha256": candidate["source_sha256"], "batch": candidate["batch"],
                  "rollout_sha256": digest(trace), "refinements": dict(Counter(t["decision"] for t in trace))}
    row = build_trajectory(history, row["tools"], template_kwargs=row["template_kwargs"], evidence=proofs)
    spec = {k: candidate[k] for k in ("id", "group", "scope", "kind", "source_ids", "bucket")}
    spec.update(row, evidence=proofs, refinement=provenance)
    check_companion_spec(spec)
    return {"id": candidate["id"], "status": "accepted", "spec": spec, "trace": trace}


def run(input_path, ledger, endpoints, workers, limit):
    for endpoint in endpoints:
        address = urlparse(endpoint)
        if (address.scheme != "http" or address.hostname != "127.0.0.1" or not address.port
                or address.username or address.password or address.path or address.query or address.fragment):
            raise ValueError("Use an isolated local HTTP endpoint")
    saved = {}
    if ledger.exists():
        with ledger.open("rb+") as storage:
            last_complete = 0
            for line in storage:
                if not line.endswith(b"\n"):
                    ledger.with_suffix(".incomplete-last-record").write_bytes(line)
                    storage.truncate(last_complete)
                    break
                last_complete += len(line)
        with ledger.open(encoding="utf-8") as previous:
            for line in previous:
                if not line.endswith("\n"):
                    break
                record = json.loads(line)
                if record["status"] == "accepted":
                    spec = record["spec"]
                    ref = spec["refinement"]
                    if ref["lane"] == "dialogue" or ref.get("host_profile") == HOST_PROFILE:
                        saved[record["id"]] = (spec["bucket"], ref["prior_sha256"])
    def one(task):
        index, candidate = task
        for turn in range(3):
            try:
                return refine(candidate, endpoints[index % len(endpoints)])
            except OSError as error:
                failure = f"{type(error).__name__}: {error}"
                time.sleep(1 + turn)
            except (KeyError, ValueError) as error:
                failure = f"{type(error).__name__}: {error}"
                break
        return {"id": candidate["id"], "status": "rejected", "bucket": candidate["bucket"], "reason": failure}
    with input_path.open(encoding="utf-8") as rows:
        pending = [row for row in map(json.loads, rows) if saved.get(row["id"]) != (row["bucket"], row["source_sha256"])]
    if limit:
        pending = pending[:limit]
    counts = Counter()
    with ledger.open("a", encoding="utf-8", newline="\n") as out, ThreadPoolExecutor(max_workers=workers) as pool:
        for result in pool.map(one, enumerate(pending)):
            counts[result["status"]] += 1
            out.write(json.dumps(result, ensure_ascii=False, separators=(",", ":")) + "\n")
            out.flush()
            if sum(counts.values()) <= 5 or sum(counts.values()) % 25 == 0:
                print(dict(counts), flush=True)
    return counts


def export(ledger, folder, shard_size, input_path=None):
    folder.mkdir(parents=True, exist_ok=False)
    specs, counts, failures = {}, Counter(), Counter()
    expected = None
    if input_path:
        with input_path.open(encoding="utf-8") as queue:
            expected = {row["id"]: (row["source_sha256"], row["bucket"]) for row in map(json.loads, queue)}
    with ledger.open(encoding="utf-8") as f:
        for line in f:
            if not line.endswith("\n"):
                break
            result = json.loads(line)
            if result["status"] == "accepted":
                spec = result["spec"]
                refinement = spec["refinement"]
                if refinement["lane"] != "dialogue" and refinement.get("host_profile") != HOST_PROFILE:
                    failures["legacy_host_rollout_requires_replay"] += 1
                    continue
                if digest(result["trace"]) != refinement["rollout_sha256"]:
                    raise ValueError("Rollout trace integrity failed: " + result["id"])
                if expected is None or expected.get(result["id"]) == (refinement["prior_sha256"], spec["bucket"]):
                    specs[result["id"]] = spec
            else:
                failures[result["reason"]] += 1
    accepted = list(specs.values())
    for i in range(0, len(accepted), shard_size):
        write_jsonl(folder / f"spec-{i // shard_size + 1:04}.jsonl", accepted[i:i + shard_size])
    for item in accepted:
        counts[item["bucket"]] += 1
    report = {"accepted": dict(counts), "recorded_rejections": dict(failures), "rows": len(accepted),
              "pending": None if expected is None else len(expected.keys() - specs.keys())}
    write_json(folder / "export-report.json", report)
    return report


def publish(input_path, ledger, folder, tokenizer, workers):
    work = folder.with_name(folder.name + "-build")
    work.mkdir(parents=True, exist_ok=True)
    specs = work / "specs"
    report = read_json(specs / "export-report.json") if (specs / "export-report.json").is_file() else export(ledger, specs, 240, input_path)
    if report["pending"] or not report["rows"]:
        raise ValueError("Some selected trajectories need a completed valid rollout")
    compiled = work / "compiled"
    compiled.mkdir(exist_ok=True)
    checked = assemble_shards(specs, tokenizer, 8192, compiled, workers, companion=True)
    folders = sorted(p for p in compiled.iterdir() if (p / "manifest.json").is_file())
    manifests = [read_json(p / "manifest.json") for p in folders]
    records = [r for manifest in manifests for r in manifest["records"]]
    if len(records) != report["rows"]:
        raise ValueError("Native shard count does not match the accepted rollout count")
    folder.mkdir(parents=True, exist_ok=False)
    for name in ("train.jsonl", "provenance.jsonl"):
        with (folder / name).open("xb") as out:
            for shard in folders:
                out.write((shard / name).read_bytes())
    files = {"train": file_hash(folder / "train.jsonl")}
    for split in ("validation", "test"):
        path = folder / (split + ".jsonl")
        path.touch(exist_ok=False)
        files[split] = file_hash(path)
    write_json(folder / "manifest.json", {"kind": "chat-sft", "schema_version": 1, "scope": manifests[0]["scope"], "records": records,
                                         "files": files, "provenance_sha256": file_hash(folder / "provenance.jsonl"),
                                         "readiness": {"ready": False, "reasons": ["pilot_without_holdout"]}})
    from .finetune import inspect_dataset

    _, check = inspect_dataset(folder)
    if not check["pilot_ready"]:
        raise ValueError("Published dataset failed trainer preflight")
    final = {**report, "native": checked["totals"], "trainer_preflight": check, "queue_sha256": file_hash(input_path),
             "ledger_sha256": file_hash(ledger), "dataset_manifest_sha256": file_hash(folder / "manifest.json")}
    write_json(folder / "acceptance-report.json", final)
    return final


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    selection = commands.add_parser("select")
    selection.add_argument("--root", required=True, type=Path)
    selection.add_argument("--output", required=True, type=Path)
    selection.add_argument("--per-category", type=int, default=3500)
    selection.add_argument("--template-limit", type=int, default=12)
    selection.add_argument("--extra", type=Path)
    runner = commands.add_parser("run")
    runner.add_argument("--input", required=True, type=Path)
    runner.add_argument("--ledger", required=True, type=Path)
    runner.add_argument("--endpoint", required=True, action="append")
    runner.add_argument("--workers", type=int, default=16)
    runner.add_argument("--limit", type=int)
    exporter = commands.add_parser("export")
    exporter.add_argument("--ledger", required=True, type=Path)
    exporter.add_argument("--output", required=True, type=Path)
    exporter.add_argument("--shard-size", type=int, default=200)
    publisher = commands.add_parser("publish")
    publisher.add_argument("--input", required=True, type=Path)
    publisher.add_argument("--ledger", required=True, type=Path)
    publisher.add_argument("--output", required=True, type=Path)
    publisher.add_argument("--tokenizer", required=True, type=Path)
    publisher.add_argument("--workers", type=int, default=2)
    args = parser.parse_args()
    if args.command == "select":
        result = select(args.root, args.output, args.per_category, args.template_limit, args.extra)
    elif args.command == "run":
        result = run(args.input, args.ledger, args.endpoint, args.workers, args.limit)
    elif args.command == "export":
        result = export(args.ledger, args.output, args.shard_size)
    else:
        result = publish(args.input, args.ledger, args.output, args.tokenizer, args.workers)
    print(json.dumps(result, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
