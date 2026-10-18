"""Check chat turn boundaries without model weights or personal messages."""

import importlib.util
import unittest
from pathlib import Path
from types import SimpleNamespace


SPEC = importlib.util.spec_from_file_location("bf16_server", Path(__file__).with_name("serve-qwen35-2b-bf16.py"))
SERVER = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(SERVER)


class GenerationBoundaryTests(unittest.TestCase):
    def stream(self, parts):
        boundary = SERVER.GenerationBoundary()
        result = "".join(boundary.consume(part) for part in parts)
        return result + boundary.finish(), boundary

    def test_chat_eos_and_checkpoint_eos_both_end_generation(self):
        tokenizer = SimpleNamespace(eos_token_id=248046)
        config = SimpleNamespace(eos_token_id=248044)
        self.assertEqual(SERVER.generation_end_token_ids(tokenizer, config), [248046, 248044])

    def test_end_token_ids_keep_existing_lists_without_duplicates(self):
        tokenizer = SimpleNamespace(eos_token_id=248046)
        config = SimpleNamespace(eos_token_id=[248044, 248046])
        self.assertEqual(SERVER.generation_end_token_ids(tokenizer, config), [248046, 248044])

    def test_missing_chat_eos_fails_before_generation(self):
        with self.assertRaisesRegex(ValueError, "chat EOS"):
            SERVER.generation_end_token_ids(SimpleNamespace(eos_token_id=None), SimpleNamespace(eos_token_id=248044))

    def test_missing_checkpoint_eos_keeps_chat_eos(self):
        self.assertEqual(SERVER.generation_end_token_ids(SimpleNamespace(eos_token_id=248046), SimpleNamespace(eos_token_id=None)), [248046])

    def test_complete_reply_ends_before_synthetic_following_turns(self):
        first = '<|ACT {"emotion":"happy"}|>你好。'
        extra = '\n<|im_start|>user\n[2026-10-18 00:58] 合成问题<|im_end|>\n<|im_start|>assistant\n合成回复'
        complete, boundary = self.stream([first + "<|im_end|>" + extra])
        self.assertEqual(complete, first)
        self.assertTrue(boundary.stopped)

    def test_streamed_reply_stops_at_every_split_of_protocol_boundaries(self):
        first = '<|ACT {"emotion":"neutral"}|>好的。'
        for marker in ("<|im_end|>", "<|endoftext|>", "<|im_start|>"):
            for split in range(len(marker) + 1):
                with self.subTest(marker=marker, split=split):
                    result, boundary = self.stream([first + marker[:split], marker[split:] + "user\n合成下一轮", "assistant\n合成回复"])
                    self.assertEqual(result, first)
                    self.assertTrue(boundary.stopped)

    def test_character_chunks_match_complete_reply(self):
        first = '<|ACT {"emotion":"question"}|>你在说 user 还是 assistant？'
        text = first + "<|im_end|>\n<|im_start|>user\n合成下一轮"
        streamed, _boundary = self.stream(text)
        self.assertEqual(streamed, first)
        complete, _boundary = self.stream([text])
        self.assertEqual(complete, first)

    def test_normal_role_words_and_timestamp_quotes_stay_visible(self):
        text = 'user 和 assistant 是消息角色。你写的 [2026-10-18 00:58] 是时间。'
        streamed, boundary = self.stream(text)
        self.assertEqual(streamed, text)
        complete, _boundary = self.stream([text])
        self.assertEqual(complete, text)
        self.assertFalse(boundary.stopped)

    def test_act_markers_and_other_literal_brackets_stay_visible(self):
        text = '<|ACT {"emotion":"happy"}|>这里的 < 只是小于号，<|custom|> 也是普通输出。'
        streamed, _boundary = self.stream(text)
        self.assertEqual(streamed, text)
        complete, _boundary = self.stream([text])
        self.assertEqual(complete, text)

    def test_finish_keeps_an_incomplete_literal_prefix(self):
        text = "普通文本 <|im_"
        streamed, _boundary = self.stream([text])
        self.assertEqual(streamed, text)

    def test_configured_protocol_boundary_handles_split_chunks(self):
        boundary = SERVER.GenerationBoundary(("<end_of_turn>",))
        result = boundary.consume("合成回复<end_of_")
        result += boundary.consume("turn>合成下一轮")
        result += boundary.finish()
        self.assertEqual(result, "合成回复")
        self.assertTrue(boundary.stopped)

    def test_invalid_protocol_boundaries_are_rejected(self):
        for markers in ((), ("",)):
            with self.assertRaisesRegex(ValueError, "nonempty"):
                SERVER.GenerationBoundary(markers)


if __name__ == "__main__":
    unittest.main()
