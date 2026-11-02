"""Ask a local model for a bounded update proposal using aggregate feedback only."""

import argparse
import json
import urllib.parse
import urllib.request
from pathlib import Path

from evolution import proposal_check
from pipeline import read, write


def suggest(config, inventory, metrics, endpoint, model, execute=False):
    url = urllib.parse.urlparse(endpoint)
    if url.scheme != "http" or url.hostname not in {"127.0.0.1", "localhost", "::1"} or url.username or url.password:
        raise ValueError("Proposal endpoint must use local loopback HTTP")
    prompt = {
        "instruction": "根据数据量与既有开发评估，提出一次微调候选。只返回JSON，含training、mix、character_budget、rationale。training基于给定配置，只允许修改rank、learning_rate、epochs、max_length。mix含base、dialogue、persona、reasoning，总和1，base至少0.3。不要修改模型路径、设备或推理格式。数据量不足时减小预算。此提议必须由独立评估验证，不能自行宣告成功。",
        "training": config, "inventory": inventory, "development_metrics": metrics,
    }
    if not execute:
        return {"execute": False, "endpoint": endpoint, "request": prompt}
    payload = {"model": model, "messages": [{"role": "user", "content": json.dumps(prompt, ensure_ascii=False)}],
               "temperature": 0.2, "max_tokens": 2048, "stream": False}
    request = urllib.request.Request(endpoint.rstrip("/") + "/chat/completions", data=json.dumps(payload).encode(),
                                     headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(request, timeout=120) as response:
        result = json.loads(response.read(1024 * 1024))
    choice = result["choices"][0]
    if choice.get("finish_reason") != "stop":
        raise ValueError("Proposal generation did not finish")
    value = json.loads(choice["message"]["content"])
    editable = {"rank", "learning_rate", "epochs", "max_length"}
    for key in set(config) | set(value["training"]):
        if key not in editable and config.get(key) != value["training"].get(key):
            raise ValueError("Model changed a fixed experiment setting")
    proposal_check(value)
    return value


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("config", "inventory", "output"):
        parser.add_argument("--" + name, required=True)
    parser.add_argument("--metrics", help="Aggregate development scores. Keep final test cases sealed")
    parser.add_argument("--endpoint", default="http://127.0.0.1:18083/v1")
    parser.add_argument("--model", default="gemma4-e2b-int4")
    parser.add_argument("--execute", action="store_true")
    args = parser.parse_args()
    if Path(args.output).exists():
        parser.error("Use a new proposal output file")
    value = suggest(read(args.config), read(args.inventory), read(args.metrics) if args.metrics else {}, args.endpoint, args.model, args.execute)
    if args.execute:
        write(args.output, value)
    print(json.dumps(value, ensure_ascii=False))


if __name__ == "__main__":
    main()
