"""Check the actual local TTS HTTP transport with a controlled runtime boundary."""

import http.client
from contextlib import closing
import importlib.util
import io
import json
import threading
import time
import unittest
import wave
from pathlib import Path


spec = importlib.util.spec_from_file_location("local_moss_tts", Path(__file__).with_name("serve-local-moss-tts.py"))
service = importlib.util.module_from_spec(spec)
spec.loader.exec_module(service)


class ControlledRuntime:
    sample_rate = 48000
    channels = 2

    def __init__(self):
        self.continue_generation = threading.Event()
        self.continue_generation.set()
        self.finished = threading.Event()
        self.calls = 0

    def describe(self):
        return {"device": "cpu"}

    def synthesize(self, _request, emit, check_cancelled):
        self.calls += 1
        try:
            check_cancelled()
            emit(b"\x01\x00\xff\xff" * 128)
            self.continue_generation.wait(2)
            check_cancelled()
            emit(b"\x02\x00\xfe\xff" * 128)
            return {"audio_seconds": 256 / self.sample_rate, "elapsed_seconds": 0.01, "first_pcm_seconds": 0.001, "chunks": 2, "rtf": 1.875}
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
        self.runtime.continue_generation.set()
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()

    def request(self, **changes):
        return {"model": service.MODEL_ID, "input": "你好！", "voice": "xiaoyu", **changes}

    def connect(self):
        return http.client.HTTPConnection("127.0.0.1", self.port, timeout=3)

    def test_returns_complete_wav_with_native_sample_rate_and_channels(self):
        with closing(self.connect()) as connection:
            connection.request("POST", "/v1/audio/speech", body=json.dumps(self.request()))
            response = connection.getresponse()
            self.assertEqual(response.status, 200)
            self.assertEqual(response.getheader("Content-Type"), "audio/wav")
            with wave.open(io.BytesIO(response.read()), "rb") as wav:
                self.assertEqual(wav.getframerate(), 48000)
                self.assertEqual(wav.getnchannels(), 2)
                self.assertEqual(wav.getnframes(), 256)

    def test_emits_pcm_before_generation_finishes(self):
        self.runtime.continue_generation.clear()
        with closing(self.connect()) as connection:
            connection.request("POST", "/v1/audio/speech", body=json.dumps(self.request(stream=True, response_format="pcm")))
            response = connection.getresponse()
            self.assertEqual(response.status, 200)
            self.assertEqual(response.getheader("Transfer-Encoding"), "chunked")
            self.assertEqual(response.getheader("X-Audio-Sample-Rate"), "48000")
            self.assertEqual(response.getheader("X-Audio-Channels"), "2")
            self.assertEqual(response.read(4), b"\x01\x00\xff\xff")
            self.assertFalse(self.runtime.finished.is_set())
            self.runtime.continue_generation.set()
            self.assertEqual(len(response.read()), 1020)

    def test_rejects_overlap_instead_of_queuing_old_requests(self):
        self.runtime.continue_generation.clear()
        with closing(self.connect()) as first:
            first.request("POST", "/v1/audio/speech", body=json.dumps(self.request(stream=True)))
            response = first.getresponse()
            self.assertEqual(response.read(4), b"\x01\x00\xff\xff")
            with closing(self.connect()) as second:
                second.request("POST", "/v1/audio/speech", body=json.dumps(self.request()))
                busy = second.getresponse()
                self.assertEqual(busy.status, 429)
                busy.read()
            self.assertEqual(self.runtime.calls, 1)
            self.runtime.continue_generation.set()
            response.read()

    def test_client_disconnect_cancels_the_runtime_and_releases_the_lock(self):
        self.runtime.continue_generation.clear()
        connection = self.connect()
        connection.request("POST", "/v1/audio/speech", body=json.dumps(self.request(stream=True)))
        response = connection.getresponse()
        response.read(4)
        response.close()
        connection.close()
        self.runtime.continue_generation.set()
        deadline = time.monotonic() + 2
        while self.server.counts["cancelled"] == 0 and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertEqual(self.server.counts["cancelled"], 1)
        self.assertFalse(self.server.generation_lock.locked())

    def test_rejects_foreign_origins_and_exposes_pcm_headers_for_local_origins(self):
        with closing(self.connect()) as connection:
            connection.request("POST", "/v1/audio/speech", body=json.dumps(self.request()), headers={"Origin": "https://example.invalid"})
            response = connection.getresponse()
            self.assertEqual(response.status, 403)
            self.assertIsNone(response.getheader("Access-Control-Allow-Origin"))
            response.read()
        self.assertEqual(self.runtime.calls, 0)
        with closing(self.connect()) as connection:
            connection.request("OPTIONS", "/v1/audio/speech", headers={"Origin": "http://localhost:5173"})
            response = connection.getresponse()
            self.assertEqual(response.status, 204)
            self.assertEqual(response.getheader("Access-Control-Allow-Origin"), "http://localhost:5173")
            self.assertIn("X-Audio-Sample-Rate", response.getheader("Access-Control-Expose-Headers"))

    def test_rejects_unsupported_model_voice_stream_format_and_style_instructions(self):
        for changes in ({"model": "tts-1"}, {"voice": "Trump"}, {"stream": "true"}, {"stream": True, "response_format": "wav"}, {"instructions": "be happy"}, {"input": "字" * 241}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                service.parse_request(self.request(**changes))


if __name__ == "__main__":
    unittest.main()
