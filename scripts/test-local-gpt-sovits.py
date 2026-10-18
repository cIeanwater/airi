"""Check the speech HTTP boundary without loading model weights."""

import http.client
import importlib.util
import io
import json
import threading
import tempfile
import time
import unittest
import wave
from contextlib import closing
from pathlib import Path


spec = importlib.util.spec_from_file_location("local_gpt_sovits", Path(__file__).with_name("serve-local-gpt-sovits.py"))
service = importlib.util.module_from_spec(spec)
spec.loader.exec_module(service)


class ControlledRuntime:
    sample_rate = 32000
    channels = 1

    def __init__(self):
        self.profiles = {"default_voice": "student-bright", "voices": {
            "student-bright": {"id": "student-bright", "name": "Curious", "description": "Bright reference",
                               "speed_factor": 1.0, "reference_audio": "private-reference.wav", "reference_text": "Private reference text"},
            "serena-friendly": {"id": "serena-friendly", "name": "Friendly", "description": "Warm reference", "speed_factor": 1.06},
        }}
        self.proceed = threading.Event()
        self.proceed.set()
        self.finished = threading.Event()
        self.calls = 0
        self.last_voice = None

    def describe(self):
        return {"device": "controlled-test-boundary"}

    def synthesize(self, request, emit, check_cancelled):
        self.calls += 1
        self.last_voice = request["voice"]
        try:
            emit(b"\x01\x00" * 128)
            while not self.proceed.wait(0.005):
                check_cancelled()
            check_cancelled()
            emit(b"\x02\x00" * 128)
            return {"audio_seconds": 0.008, "first_pcm_seconds": 0.001, "chunks": 2}
        finally:
            self.finished.set()


class SpeechProtocolTests(unittest.TestCase):
    def setUp(self):
        self.runtime = ControlledRuntime()
        self.server = service.SpeechServer(("127.0.0.1", 0), self.runtime)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.port = self.server.server_address[1]

    def tearDown(self):
        self.runtime.proceed.set()
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()

    def request(self, **changes):
        return {"model": service.MODEL_ID, "voice": service.VOICE_ID, "input": "放学啦，我们一起回去吧！", **changes}

    def connect(self):
        return http.client.HTTPConnection("127.0.0.1", self.port, timeout=3)

    def test_preserves_mono_32khz_in_complete_wav(self):
        with closing(self.connect()) as connection:
            connection.request("POST", "/v1/audio/speech", json.dumps(self.request()))
            response = connection.getresponse()
            self.assertEqual(response.status, 200)
            with wave.open(io.BytesIO(response.read()), "rb") as audio:
                self.assertEqual(audio.getframerate(), 32000)
                self.assertEqual(audio.getnchannels(), 1)
                self.assertEqual(audio.getnframes(), 256)

    def test_streams_before_generation_finishes_and_rejects_overlapping_work(self):
        self.runtime.proceed.clear()
        with closing(self.connect()) as first:
            first.request("POST", "/v1/audio/speech", json.dumps(self.request(stream=True)))
            response = first.getresponse()
            self.assertEqual(response.read(2), b"\x01\x00")
            self.assertEqual(response.getheader("X-Audio-Sample-Rate"), "32000")
            self.assertEqual(response.getheader("X-Audio-Channels"), "1")
            self.assertFalse(self.runtime.finished.is_set())
            with closing(self.connect()) as second:
                second.request("POST", "/v1/audio/speech", json.dumps(self.request()))
                busy = second.getresponse()
                self.assertEqual(busy.status, 429)
                busy.read()
            self.runtime.proceed.set()
            self.assertEqual(len(response.read()), 510)

    def test_disconnect_cancels_without_another_audio_chunk_and_releases_the_lock(self):
        self.runtime.proceed.clear()
        connection = self.connect()
        connection.request("POST", "/v1/audio/speech", json.dumps(self.request(stream=True)))
        response = connection.getresponse()
        response.read(2)
        response.close()
        connection.close()
        deadline = time.monotonic() + 2
        while self.server.counts["cancelled"] == 0 and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertEqual(self.server.counts["cancelled"], 1)
        self.assertTrue(self.runtime.finished.is_set())
        self.assertFalse(self.server.generation_lock.locked())
        self.assertFalse(self.runtime.proceed.is_set())

    def test_rejects_foreign_origins_and_advertises_pcm_headers(self):
        with closing(self.connect()) as connection:
            connection.request("POST", "/v1/audio/speech", json.dumps(self.request()), headers={"Origin": "https://example.invalid"})
            response = connection.getresponse()
            self.assertEqual(response.status, 403)
            response.read()
        self.assertEqual(self.runtime.calls, 0)
        with closing(self.connect()) as connection:
            connection.request("OPTIONS", "/v1/audio/speech", headers={"Origin": "http://localhost:5173"})
            response = connection.getresponse()
            self.assertEqual(response.status, 204)
            self.assertIn("X-Audio-Sample-Rate", response.getheader("Access-Control-Expose-Headers"))

    def test_rejects_unsupported_profiles_and_control_tokens(self):
        for changes in ({"model": "moss-tts-nano-100m"}, {"voice": "unknown"}, {"stream": "true"},
                        {"stream": True, "response_format": "wav"}, {"instructions": "be happy"},
                        {"input": "字" * 241}, {"input": "<|ACT|>happy"}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                service.parse_request(self.request(**changes), self.runtime.profiles)

    def test_lists_only_public_voice_metadata_and_routes_the_selected_voice(self):
        with closing(self.connect()) as connection:
            connection.request("GET", "/v1/audio/voices")
            response = connection.getresponse()
            self.assertEqual(response.status, 200)
            body = json.loads(response.read())
        self.assertEqual(body["default_voice"], "student-bright")
        self.assertEqual([voice["id"] for voice in body["voices"]], ["student-bright", "serena-friendly"])
        self.assertNotIn("reference_audio", body["voices"][0])
        self.assertNotIn("reference_text", body["voices"][0])
        with closing(self.connect()) as connection:
            connection.request("POST", "/v1/audio/speech", json.dumps(self.request(voice="serena-friendly")))
            response = connection.getresponse()
            self.assertEqual(response.status, 200)
            response.read()
        self.assertEqual(self.runtime.last_voice, "serena-friendly")

    def test_request_uses_configured_default_and_rejects_http_reference_changes(self):
        request = self.request()
        del request["voice"]
        profiles = {**self.runtime.profiles, "default_voice": "serena-friendly"}
        self.assertEqual(service.parse_request(request, profiles)["voice"], "serena-friendly")
        for voice in ({"reference_audio": "C:/private.wav"}, ["serena-friendly"]):
            with self.assertRaises(ValueError):
                service.parse_request(self.request(voice=voice), profiles)


class VoiceProfileTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        self.reference = self.root / "reference.wav"
        with wave.open(str(self.reference), "wb") as audio:
            audio.setframerate(16000)
            audio.setnchannels(1)
            audio.setsampwidth(2)
            audio.writeframes(b"\0\0" * 16000 * 4)
        self.entry = {"id": "bright", "name": "Bright", "description": "Synthetic reference",
                      "reference_audio": "reference.wav", "reference_text": "你好，今天过得怎么样？", "speed_factor": 1.08}
        self.config = {"version": 1, "default_voice": "bright", "voices": [self.entry]}
        self.path = self.root / "voices.json"

    def tearDown(self):
        self.directory.cleanup()

    def load(self):
        self.path.write_text(json.dumps(self.config), encoding="utf-8")
        return service.load_voice_profiles(self.path)

    def test_resolves_reference_against_config_and_preserves_exact_text(self):
        profile = self.load()["voices"]["bright"]
        self.assertEqual(profile["reference_audio"], self.reference.resolve())
        self.assertEqual(profile["reference_text"], self.entry["reference_text"])
        self.assertEqual(profile["reference_seconds"], 4)
        self.assertEqual(profile["speed_factor"], 1.08)
        self.assertEqual(len(profile["reference_sha256"]), 64)

    def test_rejects_duplicate_ids_missing_defaults_and_unknown_fields(self):
        for change in ({"voices": [self.entry, self.entry]}, {"default_voice": "absent"}, {"model_path": "unchecked.pth"}):
            original = self.config
            self.config = {**original, **change}
            with self.subTest(change=change), self.assertRaises(ValueError):
                self.load()
            self.config = original

    def test_rejects_invalid_text_ids_and_speed(self):
        for field, value in (("id", "../private"), ("speed_factor", True), ("speed_factor", 1.3),
                             ("speed_factor", float("nan")), ("reference_text", ""), ("reference_text", "<|ACT|>"),
                             ("name", "voice\nname")):
            self.config["voices"] = [{**self.entry, field: value}]
            with self.subTest(field=field, value=value), self.assertRaises(ValueError):
                self.load()

    def test_rejects_short_and_non_pcm16_references(self):
        for seconds, width in ((2, 2), (11, 2), (4, 1)):
            with wave.open(str(self.reference), "wb") as audio:
                audio.setframerate(16000)
                audio.setnchannels(1)
                audio.setsampwidth(width)
                audio.writeframes(b"\0" * width * 16000 * seconds)
            with self.subTest(seconds=seconds, width=width), self.assertRaises(ValueError):
                self.load()


if __name__ == "__main__":
    unittest.main()
