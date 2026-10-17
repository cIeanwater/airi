"""Check Smart Turn preprocessing, public Chinese fixtures, and loopback HTTP policy.

Call stack: local fixtures -> feature parity -> real ONNX CPU -> optional HTTP checks.
No microphone capture or private conversation data is read.
"""

import argparse
import hashlib
import importlib.util
import json
import urllib.error
import urllib.request
from pathlib import Path

import numpy as np
import soundfile as sf


def load_module(name, path):
    specification = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(specification)
    specification.loader.exec_module(module)
    return module


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", type=Path, required=True)
    parser.add_argument("--fixtures", type=Path, required=True)
    parser.add_argument("--reference-features", type=Path, required=True)
    parser.add_argument("--base-url", default="http://127.0.0.1:18085")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    server = load_module("smart_turn_server", Path(__file__).with_name("serve-smart-turn.py"))
    reference = load_module("official_whisper_features", args.reference_features)
    runtime = server.SmartTurn(args.model, 2)
    try:
        server.SmartTurn(args.fixtures / "model-manifest.json", 2)
    except ValueError as error:
        assert "SHA-256" in str(error)
    else:
        raise AssertionError("An incorrect checkpoint was accepted")
    random = np.random.default_rng(17)
    feature_cases = [np.zeros(128000, dtype=np.float32), random.normal(0, 0.02, 128000).astype(np.float32)]
    metadata = json.loads((args.fixtures / "dataset-preview.json").read_text())["rows"]
    metadata += json.loads((args.fixtures / "chinese-rows.json").read_text())
    fixtures = []
    for item in metadata:
        row = item["row"]
        path = args.fixtures / f'zh-{item["row_idx"]}.flac'
        if row["language"] != "zho" or not path.exists():
            continue
        audio, rate = sf.read(path, dtype="float32")
        assert rate == 16000 and audio.ndim == 1
        audio = audio[-128000:]
        feature_cases.append(np.pad(audio, (128000 - audio.size, 0)))
        prediction = runtime.predict(audio, rate)
        fixtures.append({"row_idx": item["row_idx"], "id": row["id"], "expected_complete": row["endpoint_bool"], "synthetic": row["synthetic"], "dataset": row["dataset"], "seconds": round(audio.size / rate, 3), **prediction})

    differences = []
    for audio in feature_cases:
        actual = server.log_mel(audio)
        expected = reference.compute_whisper_log_mel_features(audio, do_normalize=True)
        difference = float(np.max(np.abs(actual - expected)))
        assert actual.shape == expected.shape == (80, 800)
        assert np.allclose(actual, expected, atol=1e-6, rtol=0), difference
        differences.append(difference)
    assert runtime.predict(np.zeros(16000, dtype=np.float32), 16000)["complete"] is False

    def request(path, data=None, origin="http://localhost:5173"):
        headers = {"Origin": origin, "Content-Type": "application/octet-stream"}
        req = urllib.request.Request(args.base_url + path, data=data, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=10) as response:
                return response.status, json.loads(response.read()), dict(response.headers)
        except urllib.error.HTTPError as error:
            return error.code, json.loads(error.read()), dict(error.headers)

    status, health, _ = request("/health")
    assert status == 200 and health["provider"] == "CPUExecutionProvider"
    status, silent, headers = request("/v1/turn?sample_rate=16000", np.zeros(16000, dtype="<f4").tobytes())
    assert status == 200 and silent["complete"] is False
    assert headers.get("Access-Control-Allow-Origin") == "http://localhost:5173"
    status, _, headers = request("/v1/turn", b"1234", "https://example.invalid")
    assert status == 403 and "Access-Control-Allow-Origin" not in headers
    assert request("/v1/turn?sample_rate=1000", b"1234")[0] == 400
    assert request("/v1/turn", np.array([float("nan")], dtype="<f4").tobytes())[0] == 400
    assert request("/v1/turn", b"123")[0] == 400
    assert request("/health", origin="http://localhost:invalid")[0] == 403
    correct = sum(item["complete"] == item["expected_complete"] for item in fixtures)
    report = {"model_sha256": runtime.sha256, "model_hash_rejection": True, "reference_features_sha256": hashlib.sha256(args.reference_features.read_bytes()).hexdigest(), "feature_cases": len(differences), "feature_max_absolute_error": max(differences), "http_checks": 8, "public_chinese_correct": correct, "public_chinese_total": len(fixtures), "private_audio_read": False, "fixtures": fixtures}
    args.output.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps({key: value for key, value in report.items() if key != "fixtures"}))


if __name__ == "__main__":
    main()
