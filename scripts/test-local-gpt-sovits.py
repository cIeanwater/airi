"""Check the speech HTTP boundary without loading model weights."""

import http.client
import importlib.util
import io
import json
import threading
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
        self.proceed = threading.Event()
        self.proceed.set()
        self.finished = threading.Event()
        self.calls = 0

    def describe(self):
        return {"device": "controlled-test-boundary"}

    def synthesize(self, _request, emit, check_cancelled):
        self.calls += 1
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
                service.parse_request(self.request(**changes))


if __name__ == "__main__":
    unittest.main()
