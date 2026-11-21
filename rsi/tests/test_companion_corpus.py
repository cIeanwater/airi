"""Check companion tags and host-bound decisions."""

import json
import unittest

from rsi.companion_corpus import HOST_MARKER, check_companion_spec


def specimen(lane, output, host=None):
    system = "运行状态"
    if host is not None:
        system += HOST_MARKER + json.dumps(host, ensure_ascii=False)
    return {"kind": "dialogue", "refinement": {"lane": lane}, "messages": [
        {"role": "system", "content": system, "loss_weight": 0},
        {"role": "assistant", "content": output, "reasoning_content": "根据现场信息判断。", "loss_weight": 1},
    ]}


class CompanionCorpusTests(unittest.TestCase):
    def test_visible_dialogue_uses_valid_emotion_and_short_chinese_thought(self):
        row = specimen("dialogue", '<|ACT {"emotion":"happy"}|> 哇，好呀。')
        check_companion_spec(row)
        row["messages"][-1]["content"] = '<|ACT {"emotion":"excited"}|> 好呀。'
        with self.assertRaisesRegex(ValueError, "registered AIRI emotions"):
            check_companion_spec(row)
        row["messages"][-1]["reasoning_content"] = "I'll say something friendly. 哈哈哈哈。"
        with self.assertRaisesRegex(ValueError, "Chinese decision rationale"):
            check_companion_spec(row)

    def test_system_only_host_state_checks_blocked_and_repeated_speech(self):
        decision = json.dumps({"thought": "可以聊画面。", "speak": True, "text": "你好。"}, ensure_ascii=False)
        for policy in ({"canSpeak": False}, {"canSpeak": True, "lastInitiativeText": "你好。"}):
            with self.subTest(policy=policy), self.assertRaisesRegex(ValueError, "host permission or repeat rule"):
                check_companion_spec(specimen("initiative", decision, {"engagement": policy}))

    def test_consolidation_binds_revision_and_source_scope(self):
        path = "people/a.md"
        host = {"documents": [{"path": path, "revision": 3, "sources": ["old"]}],
                "experiences": [{"id": "new", "cognitionPath": path}, {"id": "foreign", "cognitionPath": "人格.md"}]}
        change = {"path": path, "text": "对方说喜欢黑咖啡。", "nature": "reported",
                  "sources": ["new", "old"], "expectedRevision": 3}

        def check(props):
            check_companion_spec(specimen("organize", json.dumps({"documents": [{**change, **props}]}), host))

        check({})
        for props, error in [({"expectedRevision": 2}, "version does not match"),
                             ({"sources": ["old"]}, "available new source"),
                             ({"sources": ["old", "new"]}, "first consolidation source"),
                             ({"sources": ["foreign"]}, "another document")]:
            with self.subTest(props=props), self.assertRaisesRegex(ValueError, error):
                check(props)

    def test_extended_reasoning_is_limited_to_system_only_self_thinking(self):
        decision = json.dumps({"thought": "等待对方。", "speak": False, "text": ""}, ensure_ascii=False)
        row = specimen("initiative", decision, {"engagement": {"canSpeak": False}})
        thought = "对方正在重新装纸轴。他此前说，等纸轴卡进槽再讨论。两次画面切换只能证明视角变了，我也没有收到他完成的消息。"
        thought += "如果这时主动问有没有弄好，他还要停下手里的动作回答。频繁问也会让他以为我能看见夹子的位置。我先等他报告完成，再围绕他提到的卡槽问一句。"
        thought += "如果后来只看到纸轴移动，我会继续等待对方的原话。"
        row["messages"][-1]["reasoning_content"] = thought
        with self.assertRaisesRegex(ValueError, "1 to 120 characters"):
            check_companion_spec(row)
        row["refinement"]["reasoning_profile"] = "extended"
        check_companion_spec(row)
        row["messages"].insert(1, {"role": "user", "content": "你好。", "loss_weight": 0})
        with self.assertRaisesRegex(ValueError, "one masked host"):
            check_companion_spec(row)
        row["messages"].pop(1)
        row["messages"][-1]["reasoning_content"] = "等待对方。"
        with self.assertRaisesRegex(ValueError, "120 to 600 characters"):
            check_companion_spec(row)


if __name__ == "__main__":
    unittest.main()
