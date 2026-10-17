"""Benchmark only public and generated fixtures against the prepared local ASR.

Call stack: fixture WAV -> HTTP or incremental PCM16 WebSocket -> resident CPU model.
"""

import argparse
import json
import statistics
import threading
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path

import numpy as np
import soundfile as sf
from websockets.exceptions import InvalidStatus
from websockets.sync.client import connect


MODEL = "zipformer-small-zh-en-int8"


def http_transcribe(path, base_url, model=MODEL, token=None):
    boundary = "AiriPublicBenchmark" + uuid.uuid4().hex
    body = f'--{boundary}\r\nContent-Disposition: form-data; name="model"\r\n\r\n{model}\r\n--{boundary}\r\nContent-Disposition: form-data; name="language"\r\n\r\nzh\r\n--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="public.wav"\r\nContent-Type: audio/wav\r\n\r\n'.encode() + path.read_bytes() + f"\r\n--{boundary}--\r\n".encode()
    headers = {"Content-Type": "multipart/form-data; boundary=" + boundary, "Origin": "http://localhost:5173"}
    if token:
        headers["Authorization"] = "Bearer " + token
    request = urllib.request.Request(base_url + "/v1/audio/transcriptions", data=body, headers=headers)
    started = time.perf_counter()
    with urllib.request.urlopen(request, timeout=10) as response:
        result = json.loads(response.read())
    return {"http_ms": round((time.perf_counter() - started) * 1000, 2), **result}


def stream_transcribe(path, endpoint, pace=False):
    audio, rate = sf.read(path, dtype="float32", always_2d=True)
    samples = audio.mean(axis=1)
    if rate != 16000:
        samples = np.interp(np.arange(0, len(samples), rate / 16000), np.arange(len(samples)), samples).astype(np.float32)
    pcm = (np.clip(samples, -1, 1) * 32767).astype("<i2").tobytes()
    updates = []
    finished_sending = threading.Event()
    first_partial_before_finish = False
    first_partial_ms = None
    started = time.perf_counter()
    final_sent = 0
    with connect(endpoint, origin="http://localhost:5173", max_size=1048576) as socket:
        socket.send(json.dumps({"type": "start", "model": MODEL, "sample_rate": 16000}))

        def send():
            nonlocal final_sent
            for position in range(0, len(pcm), 3200):
                socket.send(pcm[position:position + 3200])
                if pace:
                    time.sleep(0.1)
            final_sent = time.perf_counter()
            finished_sending.set()
            socket.send('{"type":"finish"}')

        sender = threading.Thread(target=send)
        sender.start()
        for message in socket:
            event = json.loads(message)
            if event["type"] == "partial":
                if first_partial_ms is None:
                    first_partial_ms = round((time.perf_counter() - started) * 1000, 2)
                if not finished_sending.is_set():
                    first_partial_before_finish = True
                updates.append(event["text"])
            if event["type"] == "final":
                sender.join()
                return {**event, "post_finish_ms": round((time.perf_counter() - final_sent) * 1000, 2), "first_partial_ms": first_partial_ms, "partial_count": len(updates), "partial_before_finish": first_partial_before_finish}
            assert event["type"] != "error", event
    raise RuntimeError("No final transcript")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fixtures", type=Path, required=True)
    parser.add_argument("--http", default="http://127.0.0.1:18086")
    parser.add_argument("--websocket", default="ws://127.0.0.1:18087/v1/stream")
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--whisper-base-url")
    parser.add_argument("--whisper-token-file", type=Path)
    args = parser.parse_args()
    known = {
        "synthetic-zh-1.wav": "你好我叫小林今天想聊一聊学校里的趣事",
        "synthetic-zh-2.wav": "请告诉我明天上午九点的天气怎么样",
        "synthetic-zh-3.wav": "我想先问你一个问题然后再决定怎么做",
    }
    rows = []
    token = args.whisper_token_file.read_text(encoding="utf-8").strip() if args.whisper_token_file else None
    for name, expected in known.items():
        results = [http_transcribe(args.fixtures / name, args.http) for _ in range(3)]
        text = results[-1]["text"]
        assert text.startswith(expected[:4]), (name, text)
        row = {"file": name, "expected": expected, "recognized": text, "median_http_ms": statistics.median(x["http_ms"] for x in results), "median_inference_ms": statistics.median(x["inference_ms"] for x in results)}
        if args.whisper_base_url:
            baseline = [http_transcribe(args.fixtures / name, args.whisper_base_url, "Systran/faster-whisper-small", token) for _ in range(3)]
            row["whisper_median_http_ms"] = statistics.median(x["http_ms"] for x in baseline)
        rows.append(row)
    english = Path(__file__).resolve().parents[1] / "packages/testing-audio/cases/single-utterance-pipeline/input.test.wav"
    english_result = http_transcribe(english, args.http)
    assert "SAY HELLO" in english_result["text"].upper()
    paced = stream_transcribe(args.fixtures / "synthetic-zh-1.wav", args.websocket, pace=True)
    assert paced["partial_before_finish"] and paced["partial_count"] > 0
    assert paced["text"].startswith("你好我叫小林")
    assert paced["post_finish_ms"] < 250
    foreign_http = urllib.request.Request(args.http + "/health", headers={"Origin": "https://example.invalid"})
    try:
        urllib.request.urlopen(foreign_http, timeout=5)
        raise AssertionError("Foreign HTTP origin was accepted")
    except urllib.error.HTTPError as error:
        assert error.code == 403
    try:
        with connect(args.websocket, origin="https://example.invalid"):
            raise AssertionError("Foreign WebSocket origin was accepted")
    except InvalidStatus as error:
        assert error.response.status_code == 403
    with connect(args.websocket, origin="http://localhost:5173") as socket:
        socket.send(json.dumps({"type": "start", "model": "invalid", "sample_rate": 16000}))
        assert json.loads(socket.recv())["type"] == "error"
    report = {"model": MODEL, "private_audio_read": False, "chinese": rows, "english": english_result, "paced_stream": paced, "origin_rejections": 2, "bad_model_rejected": True}
    args.report.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    print(json.dumps({"chinese_http_medians_ms": [r["median_http_ms"] for r in rows], "english_http_ms": english_result["http_ms"], "paced_post_finish_ms": paced["post_finish_ms"], "live_partial": paced["partial_before_finish"], "origin_rejections": 2}))


if __name__ == "__main__":
    main()
