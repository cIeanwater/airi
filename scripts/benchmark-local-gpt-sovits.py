"""Measure local generated speech, audio continuity, recognition, and cancellation."""

import argparse
import http.client
import json
import time
import urllib.request
import wave
from pathlib import Path


def health(port):
    with urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=5) as response:
        return json.load(response)


def generate(port, text, output, voice):
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=60)
    started = time.perf_counter()
    connection.request("POST", "/v1/audio/speech", json.dumps({"model": "gpt-sovits-v2proplus", "voice": voice,
                                                            "input": text, "stream": True, "response_format": "pcm"}))
    response = connection.getresponse()
    if response.status != 200:
        raise RuntimeError(f"Speech returned HTTP {response.status}: {response.read().decode()}")
    rate = int(response.getheader("X-Audio-Sample-Rate"))
    channels = int(response.getheader("X-Audio-Channels"))
    chunks, arrivals = [], []
    while True:
        chunk = response.read1(1024 * 1024)
        if not chunk:
            break
        arrivals.append({"seconds": time.perf_counter() - started, "audio_seconds": len(chunk) / (rate * channels * 2)})
        chunks.append(chunk)
    elapsed = time.perf_counter() - started
    connection.close()
    with wave.open(str(output), "wb") as audio:
        audio.setframerate(rate)
        audio.setnchannels(channels)
        audio.setsampwidth(2)
        audio.writeframes(b"".join(chunks))
    duration = sum(map(len, chunks)) / (rate * channels * 2)
    # Playback begins 100 ms after first PCM. Each arrival extends the scheduled audio duration.
    scheduled, underrun = arrivals[0]["seconds"] + 0.1, 0.0
    for arrival in arrivals:
        gap = max(0, arrival["seconds"] - scheduled)
        underrun += gap
        scheduled = max(scheduled, arrival["seconds"]) + arrival["audio_seconds"]
    return {"file": str(output.resolve()), "voice": voice, "text": text, "sample_rate": rate, "channels": channels,
            "first_pcm_seconds": round(arrivals[0]["seconds"], 4), "generation_seconds": round(elapsed, 4),
            "audio_seconds": round(duration, 4), "rtf": round(elapsed / duration, 4),
            "transport_underrun_seconds_at_100ms_buffer": round(underrun, 4),
            "arrivals": [{key: round(value, 4) for key, value in arrival.items()} for arrival in arrivals]}


def cancel(port, after_pcm, voice):
    before = health(port)["requests"]["cancelled"]
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=30)
    connection.request("POST", "/v1/audio/speech", json.dumps({"model": "gpt-sovits-v2proplus", "voice": voice,
                                                            "input": "今天放学以后，我们一起去看看新开的书店，然后去操场走走，再回家写作业，好不好呀？",
                                                            "stream": True, "response_format": "pcm"}))
    response = None
    if after_pcm:
        response = connection.getresponse()
        response.read(2)
    else:
        time.sleep(0.02)
    started = time.perf_counter()
    if response:
        response.close()
    connection.close()
    deadline = started + 5
    while time.perf_counter() < deadline:
        status = health(port)
        if status["requests"]["cancelled"] > before and not status["busy"]:
            return {"stage": "after_pcm" if after_pcm else "before_pcm", "lock_released_seconds": round(time.perf_counter() - started, 4)}
        time.sleep(0.01)
    raise RuntimeError("The cancelled speech request did not release its generation lock")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=18089)
    parser.add_argument("--output-dir", type=Path, default=Path(__file__).resolve().parents[2] / ".tools/expressive-voice-2026-10-18")
    parser.add_argument("--asr-port", type=int, default=18086)
    parser.add_argument("--voice", help="Use a configured voice ID. Omit to test the service default")
    args = parser.parse_args()
    args.output_dir.mkdir(parents=True, exist_ok=True)
    report = {"synthetic_audio": True, "initial_health": health(args.port), "samples": []}
    voice = args.voice or report["initial_health"]["voice"]
    for name, text in (("greeting", "你回来啦！今天过得怎么样？要不要先坐下来，和我聊一会儿？"),
                       ("school", "今天放学以后，我们一起去看看新开的书店，好不好呀？"),
                       ("gentle", "嗯，我在听。今天很辛苦吧？先别着急，我们慢慢说。")):
        output = args.output_dir / f"gpt-student-{name}.wav"
        result = generate(args.port, text, output, voice)
        if args.asr_port:
            import requests
            with output.open("rb") as audio:
                response = requests.post(f"http://127.0.0.1:{args.asr_port}/v1/audio/transcriptions",
                                         data={"model": "zipformer-small-zh-en-int8"},
                                         files={"file": (output.name, audio, "audio/wav")}, timeout=60)
                response.raise_for_status()
                result["asr"] = response.json()
        report["samples"].append(result)
        print(json.dumps(result, ensure_ascii=True), flush=True)
    report["cancellation"] = [cancel(args.port, False, voice), cancel(args.port, True, voice)]
    report["recovery"] = generate(args.port, "我听到啦，你接着说，我在呢。", args.output_dir / "gpt-student-after-cancel.wav", voice)
    report["final_health"] = health(args.port)
    (args.output_dir / "gpt-benchmark.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"cancellation": report["cancellation"], "final_health": report["final_health"]}), flush=True)


if __name__ == "__main__":
    main()
