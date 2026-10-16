"""Check the BF16 service with synthetic requests and content-free reports."""

import argparse
import hashlib
import json
import time
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen


MODEL_ID = "qwen3.5-2b-bf16"


def fetch(base_url: str, path: str, payload=None, headers=None, method=None):
    body = None if payload is None else json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request = Request(base_url + path, data=body, headers={"Content-Type": "application/json", **(headers or {})}, method=method)
    return urlopen(request, timeout=120)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://127.0.0.1:18083")
    parser.add_argument("--output", type=Path, default=Path("local-models/debug/qwen35-2b-bf16-smoke.json"))
    args = parser.parse_args()
    if not args.base_url.startswith("http://127.0.0.1:"):
        raise ValueError("The smoke test only sends synthetic requests to loopback")
    with fetch(args.base_url, "/health") as response:
        health = json.load(response)
    assert health["model_id"] == MODEL_ID
    assert health["publisher_repo"] == "Qwen/Qwen3.5-2B"
    assert health["publisher_hashes_verified"] is True
    assert health["checkpoint_dtype_counts"] == {"BF16": 596, "F32": 36}
    assert health["runtime_parameter_dtype_counts"]["torch.bfloat16"] > 0
    assert set(health["runtime_parameter_device_counts"]) == {"cuda:0"}
    assert health["quantization"] is None
    with fetch(args.base_url, "/v1/models") as response:
        assert [model["id"] for model in json.load(response)["data"]] == [MODEL_ID]
    browser_origin = "http://localhost:5173"
    with fetch(args.base_url, "/v1/chat/completions", headers={"Origin": browser_origin, "Access-Control-Request-Method": "POST"}, method="OPTIONS") as response:
        assert response.status == 204
        assert response.headers["Access-Control-Allow-Origin"] == browser_origin
        assert response.headers["Vary"] == "Origin"
    for method in ["OPTIONS", "POST"]:
        try:
            fetch(args.base_url, "/v1/chat/completions", payload={} if method == "POST" else None,
                  headers={"Origin": "https://example.invalid"}, method=method)
        except HTTPError as error:
            assert error.code == 403
            assert error.headers.get("Access-Control-Allow-Origin") is None
        else:
            raise AssertionError("The service accepted a foreign browser origin")
    results = []
    messages = [
        {"role": "system", "content": "你是本地推理接口测试助手。只用中文直接回答，不调用工具。"},
        {"role": "user", "content": "请用一句简短的中文问候。"},
    ]
    for streaming in [False, True]:
        payload = {"model": MODEL_ID, "messages": messages, "max_tokens": 48, "temperature": 0, "stream": streaming}
        started = time.perf_counter()
        first_text_seconds = None
        chunks = 0
        output = ""
        usage = None
        with fetch(args.base_url, "/v1/chat/completions", payload, headers={"Origin": browser_origin} if streaming else None) as response:
            if streaming:
                assert response.headers["Access-Control-Allow-Origin"] == browser_origin
            if streaming:
                done = False
                for raw_line in response:
                    line = raw_line.decode("utf-8").strip()
                    if not line.startswith("data: "):
                        continue
                    if line == "data: [DONE]":
                        done = True
                        break
                    event = json.loads(line[6:])
                    assert "error" not in event, event.get("error")
                    for choice in event["choices"]:
                        text = choice["delta"].get("content", "")
                        if text:
                            if first_text_seconds is None:
                                first_text_seconds = round(time.perf_counter() - started, 3)
                            output += text
                            chunks += 1
                    usage = event.get("usage", usage)
                assert done
            else:
                result = json.load(response)
                assert result["model"] == MODEL_ID
                output = result["choices"][0]["message"]["content"]
                usage = result["usage"]
        assert isinstance(output, str) and output.strip()
        assert usage["completion_tokens"] > 0
        if streaming:
            assert chunks > 0
        results.append({"stream": streaming, "seconds": round(time.perf_counter() - started, 3),
                        "first_text_seconds": first_text_seconds, "content_chunks": chunks,
                        "output_characters": len(output), "output_sha256": hashlib.sha256(output.encode("utf-8")).hexdigest(),
                        "usage": usage, "status": "passed"})
    negative_cases = [
        {"model": "unverified-alias", "messages": messages},
        {"model": MODEL_ID, "messages": [{"role": "user", "content": [{"type": "input_audio", "input_audio": {"data": "AA==", "format": "wav"}}]}]},
        {"model": MODEL_ID, "messages": messages, "max_tokens": 1000000},
    ]
    for payload in negative_cases:
        try:
            fetch(args.base_url, "/v1/chat/completions", payload)
        except HTTPError as error:
            assert error.code == 400
        else:
            raise AssertionError("The service accepted an invalid model, audio input, or token budget")
    report = {"status": "passed", "base_url": args.base_url, "model_id": MODEL_ID,
              "checkpoint_dtype_counts": health["checkpoint_dtype_counts"],
              "runtime_parameter_dtype_counts": health["runtime_parameter_dtype_counts"],
              "runtime_parameter_device_counts": health["runtime_parameter_device_counts"],
              "publisher_hashes_verified": health["publisher_hashes_verified"],
              "weight_sha256": health["sha256"]["model.safetensors-00001-of-00001.safetensors"],
              "native_audio_rejected": True, "negative_cases_passed": len(negative_cases),
              "loopback_preflight_allowed": True, "foreign_origin_post_rejected": True,
              "foreign_origin_preflight_rejected": True, "requests": results}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
