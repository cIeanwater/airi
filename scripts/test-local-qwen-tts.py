"""Check the local TTS HTTP contract without loading the model or using a GPU."""

import importlib.util
import unittest
from pathlib import Path


spec = importlib.util.spec_from_file_location("local_qwen_tts", Path(__file__).with_name("serve-local-qwen-tts.py"))
service = importlib.util.module_from_spec(spec)
spec.loader.exec_module(service)


class SpeechContractTests(unittest.TestCase):
    def request(self, **changes):
        return {"model": service.MODEL_ID, "input": "今天过得怎么样？", "voice": "Serena", **changes}

    def test_accepts_local_browser_origins_and_rejects_remote_or_malformed_origins(self):
        for origin in (None, "http://localhost:5173", "http://127.0.0.1:4173", "https://[::1]:9443"):
            self.assertTrue(service.allows_origin(origin))
        for origin in ("null", "https://example.com", "http://localhost.example.com", "http://localhost/path", "http://user@localhost:5173", "http://localhost:bad"):
            self.assertFalse(service.allows_origin(origin))

    def test_preserves_text_and_combines_persona_with_explicit_emotion(self):
        result = service.parse_request(self.request(emotion="curious", instructions="稍微放慢一点。"), {"serena"})
        self.assertEqual(result["text"], "今天过得怎么样？")
        self.assertEqual(result["speaker"], "serena")
        self.assertIn("成年女大学生", result["instruct"])
        self.assertIn("好奇", result["instruct"])
        self.assertIn("稍微放慢一点", result["instruct"])

    def test_accepts_xsai_extra_body_instruction_shape(self):
        result = service.parse_request(self.request(extra_body={"instructions": "开心地说。", "emotion": "happy"}), {"serena"})
        self.assertEqual(result["emotion"], "happy")
        self.assertIn("开心地说", result["instruct"])

    def test_rejects_wrong_models_unknown_voices_and_unsupported_formats(self):
        for changes in ({"model": "tts-1"}, {"voice": "unknown"}, {"response_format": "mp3"}, {"speed": 2}, {"emotion": "unknown"}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                service.parse_request(self.request(**changes), {"serena"})

    def test_rejects_empty_or_oversized_text_and_control_tokens(self):
        for text in ("", "   ", "字" * 401, '<|ACT {"emotion":"happy"}|>你好'):
            with self.subTest(text=text[:20]), self.assertRaises(ValueError):
                service.parse_request(self.request(input=text), {"serena"})

    def test_neutral_request_does_not_inherit_a_previous_emotion(self):
        service.parse_request(self.request(emotion="angry"), {"serena"})
        result = service.parse_request(self.request(), {"serena"})
        self.assertEqual(result["emotion"], "neutral")
        self.assertNotIn("不满", result["instruct"])


if __name__ == "__main__":
    unittest.main()
