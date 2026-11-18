"""Build an immutable runtime-format release and a smaller, stratified correction pass."""
import argparse
import json
import random
import tempfile
from collections import Counter, defaultdict
from pathlib import Path

from .common import digest, file_hash, read_json, read_jsonl, write_json, write_jsonl
from .finetune import CHAT_SFT_KIND, inspect_dataset
from .host_context import project_host_messages, project_host_tools


def bucket(channel, lane, row):
    final = row["messages"][-1]["content"]
    if lane == "dialogue":
        calls = [c["function"]["name"] for m in row["messages"] for c in m.get("tool_calls", [])]
        return "tool-" + "+".join(sorted(set(calls))) if calls else "no-tool"
    if lane == "initiative":
        return "speak" if json.loads(final)["speak"] else "wait"
    if lane == "organize":
        return "write" if json.loads(final)["documents"] else "no-op"
    return "no-op" if final.strip() == "NO_UPDATE" else "extract"


def release(path, items, scope, parent_hash, addition_hash):
    path.mkdir()
    counts, files, records = Counter(), {}, []
    for split in ("train", "validation", "test"):
        rows = []
        for rec, row, channel, lane in items:
            if rec["split"] != split:
                continue
            records.append({**rec, "content_sha256": digest(row)})
            rows.append(row)
            counts[split + "/" + channel + "/" + lane] += 1
        write_jsonl(path / (split + ".jsonl"), rows)
        files[split] = file_hash(path / (split + ".jsonl"))
    write_json(path / "manifest.json", dict(schema_version=1, kind=CHAT_SFT_KIND, scope=scope,
               readiness={"ready": True, "reasons": []}, records=records, files=files,
               source_manifest_sha256=parent_hash, additions_sha256=addition_hash))
    report = inspect_dataset(path)[1]
    if not report["ready"]:
        raise ValueError(report["errors"])
    return {"rows": report["rows"], "coverage": dict(counts), "manifest": report["manifest_sha256"]}


def verify_reviews(additions, candidates, ledger, actor_dir):
    """Bind each supervised decision to its actual actor trace and reviewed target."""
    proposed = {row["id"]:row for row in read_jsonl(candidates)}
    reviewed = read_jsonl(additions)
    entries = read_jsonl(ledger)
    by_target = {(e["id"],e["index"]): e for e in entries}
    expected = {(r["id"], i) for r in reviewed for i,m in enumerate(r["messages"]) if m.get("loss_weight")}
    if len(by_target) != len(entries) or set(by_target) != expected or not expected:
        raise ValueError("Incomplete or duplicate per-decision GPT review")
    for row in reviewed:
        for i, msg in enumerate(row["messages"]):
            if not msg.get("loss_weight"):
                continue
            key = row["id"], i
            entry = by_target[key]
            # Only local immutable actor files can authorize a supervised target.
            file = actor_dir / (digest(list(key)) + ".json")
            origin = proposed[key[0]]
            if (Path(entry["actor_file"]).name != file.name or file_hash(file) != entry["actor_sha256"]
                    or digest(row) != entry["reviewed_row_sha256"] or digest(msg) != entry["reviewed_target_sha256"]
                    or digest(origin) != entry["candidate_sha256"] or not entry["note"].strip()):
                raise ValueError("Broken review binding for " + str(key))
            binding = read_json(file)["binding"]
            if (binding["id"],binding["index"],binding["candidate"]) != (*key,digest(origin)):
                raise ValueError("Actor trace does not belong to the reviewed candidate")
    return {"rows":len(reviewed),"decisions":len(expected),"ledger_sha256":file_hash(ledger),
            "proposals_sha256":file_hash(candidates),"reviewed_sha256":file_hash(additions)}

def prepare(source, queue, additions, output, candidates, ledger, actor_dir, holdouts=None, seed=42):
    if output.exists():
        raise FileExistsError(output)
    verified = verify_reviews(additions, candidates, ledger, actor_dir)
    manifest = read_json(source / "manifest.json")
    audit = inspect_dataset(source)[1]
    if not audit["ready"]:
        raise ValueError(audit["errors"])
    with queue.open(encoding="utf-8-sig") as inp:
        meta = {q["id"]: (q["channel"], q["refinement"]["lane"]) for q in map(json.loads, inp)}
    all_rows, selected, projected, rejected = [], [], Counter(), []
    pools = defaultdict(list)
    rng = random.Random(seed)
    for split in ("train", "validation", "test"):
        records = [r for r in manifest["records"] if r["split"] == split]
        for rec, row in zip(records, read_jsonl(source / (split+".jsonl")), strict=True):
            channel, lane = meta[rec["id"]]
            try:
                messages = project_host_messages(row["messages"], lane)
                tools = project_host_tools(row["tools"],lane)
            except ValueError as err:
                rejected.append({"id": rec["id"], "reason": str(err)})
                continue
            projected[lane] += messages != row["messages"]
            row = {**row, "messages":messages, "tools":tools}
            item = (rec,row,channel,lane)
            all_rows.append(item)
            if split != "train" or channel == "qq":
                selected.append(item)
            else:
                pools[lane, bucket(channel,lane,row)].append(item)
    # Keep all scarce QQ trajectories and sample desktop controls by action, not raw frequency.
    for (lane, decision), candidates in sorted(pools.items()):
        cap = {"initiative":14, "organize":7, "extract":5, "dialogue":6}[lane]
        selected.extend(rng.sample(candidates, min(cap,len(candidates))))
    seen = {digest(row) for _, row, _, _ in all_rows}
    for addition in read_jsonl(additions):
        rec = {k:addition[k] for k in ("id","group","source_ids","kind")}
        row = {k:addition[k] for k in ("messages","tools","template_kwargs")}
        if digest(row) in seen:
            raise ValueError("Duplicate training addition: " + rec["id"])
        seen.add(digest(row))
        item = ({**rec, "split":"train"},row,"qq","dialogue")
        selected.append(item)
        all_rows.append(item)
    if holdouts:
        for sample in read_jsonl(holdouts):
            if sample["split"] not in {"validation","test"}:
                raise ValueError("Independent references must not enter training")
            rec = {k:sample[k] for k in ("id","group","split","source_ids","kind")}
            row = {k:sample[k] for k in ("messages","tools","template_kwargs")}
            row["messages"] = project_host_messages(row["messages"],sample["lane"])
            row["tools"] = project_host_tools(row["tools"],sample["lane"])
            item = rec,row,sample["channel"],sample["lane"]
            selected.append(item)
            all_rows.append(item)
    rng.shuffle(selected)
    output.parent.mkdir(parents=True,exist_ok=True)
    with tempfile.TemporaryDirectory(dir=output.parent) as tmp:
        stage=Path(tmp)/"release"
        stage.mkdir()
        lineage=manifest["scope"], digest(manifest), file_hash(additions)
        report = {"review":verified,"seed":seed, "projected":dict(projected), "quarantined":rejected,
                  "full":release(stage/"full",all_rows,*lineage), "correction":release(stage/"correction",selected,*lineage)}
        write_json(stage/"coverage.json",report)
        stage.rename(output)
    return report


if __name__ == "__main__":
    p=argparse.ArgumentParser(description=__doc__)
    for name in ["source","queue","additions","output","candidates","ledger","actor-dir"]:
        p.add_argument("--"+name,type=Path,required=True)
    p.add_argument("--holdouts",type=Path)
    args=p.parse_args()
    report=prepare(**vars(args))
    print(json.dumps({k:report[k] for k in ("full","correction","projected")},ensure_ascii=False))
