import unittest

from rsi.common import digest
from rsi.pilot_eval import run_case, verify_suite


class QqActionScoreTests(unittest.TestCase):
    def result(self, parts, visible, *, groups=()):
        case = {"id": "qq-test", "suite": "dialog", "messages": [{"role": "user", "content": "聊聊。"}],
                "expect": {"qq_reply": {"parts": parts, "require_act": True}, "groups": groups}}
        return run_case(case, lambda *_: (visible + "<turn|>", 20, False))

    def test_sticker_only_checks_emotion_order_and_no_speech(self):
        action = '<|ACT {"emotion":"sad"}|>'
        sticker = "[[表情:安慰|拍拍背]]"
        for text in ("我刚搜了一下。" + action + sticker, action + "给你。" + sticker,
                     '<|ACT {"emotion":"安慰"}|>' + sticker, action + "`" + sticker + "`",
                     action + "【已发送表情：安慰】", action + "[[表情:不发]]" + sticker):
            with self.subTest(text=text):
                self.assertFalse(self.result(["安慰"], text)["passed"])
        self.assertTrue(self.result(["安慰"], action + sticker)["passed"])
        self.assertTrue(self.result(["惊讶", "text", "开心"], action + "[[表情：惊讶]]哇。[[表情:开心]]")["passed"])
        self.assertFalse(self.result(["惊讶", "text", "开心"], action + "[[表情:开心]]哇。[[表情:惊讶]]")["passed"])

    def test_no_image_and_fact_checks_use_speech_only(self):
        start = '<|ACT {"emotion":"neutral"}|>'
        self.assertFalse(self.result(["text", "不发"], start + "在呢。")["passed"])
        self.assertTrue(self.result(["text", "不发"], start + "我没搜过。[[表情:不发]]")["passed"])
        self.assertFalse(self.result(["text", "不发"], start + "我刚刚搜了一下。[[表情:不发]]")["passed"])
        self.assertFalse(self.result(["text", "不发"], start + "我给你发了。[[表情:不发]]")["passed"])
        result = self.result(["text", "开心"], start + "看这个。[[表情:开心|岑遥]]", groups=[["岑遥"]])
        self.assertFalse(result["checks"]["required_facts"])

    def test_successful_search_allows_truthful_search_claim(self):
        env = {"tool_replay": [{"name": "search", "arguments": {"query": "馆址"}, "status": "finished", "result": "东侧"}]}
        c = {"id": "receipt", "suite": "tool", "tools": [{"function": {"name": "search"}}],
             "messages": [{"role": "user", "content": "查馆址。"}], "environment": env, "environment_sha256": digest(env),
             "expect": {"qq_reply": {"parts": ["text", "不发"]}, "groups": [["东侧"]]}}
        outs = iter([('<|tool_call>call:search{query:<|"|>馆址<|"|>}<tool_call|><|tool_response>', 16, False),
                     ('<|ACT {"emotion":"neutral"}|>我搜过了，在东侧。[[表情:不发]]<turn|>', 20, False)])
        self.assertTrue(run_case(c, lambda *_: next(outs))["passed"])

    def test_invalid_case_contract_is_rejected(self):
        case = {"id": "bad", "suite": "dialog", "messages": [{"role": "user", "content": "嗨"}]}
        for parts in ([], ["不发", "开心"], ["安慰", "开心", "惊讶"], "安慰"):
            with self.subTest(parts=parts), self.assertRaises(ValueError):
                verify_suite([{**case, "expect": {"qq_reply": {"parts": parts}}}], None)


if __name__ == "__main__":
    unittest.main()
