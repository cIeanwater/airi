import unittest

from rsi.common import digest
from rsi.pilot_eval import document_checks, reflection_decision, run_case, run_suite, summarize, verify_suite


def answer(text):
    return "按真实资料回答。<channel|>" + text + "<turn|>", 20, True


class PilotEvalTests(unittest.TestCase):
    def test_search_reference_supports_url_and_short_chinese_answer(self):
        environment = {"tool_replay": [{"name": "search", "arguments": {"query": "工作坊"}, "status": "finished",
                                        "result": {"url": "https://workspace.example.org/sessions/register", "time": "14:30"}}]}
        case = {"id": "search", "suite": "tool", "messages": [{"role": "user", "content": "几点？"}],
                "tools": [{"function": {"name": "search"}}], "environment": environment,
                "environment_sha256": digest(environment),
                "expect": {"groups": [["14:30"], ["https://workspace.example.org/sessions/register"]], "max_sentences": 1}}
        call = ("查已给主题。<channel|><|tool_call>call:search{query:<|\"|>工作坊<|\"|>}<tool_call|><|tool_response>", 20, True)
        outputs = iter([call, answer("14：30开放，见[公告](https://workspace.example.org/sessions/register)。")])
        result = run_case(case, lambda *_: next(outputs))
        self.assertTrue(result["passed"])
        self.assertTrue(result["brief_chinese_native_thought"])
        self.assertEqual(len(result["replay_receipts"]), 1)
        self.assertEqual(summarize([result])["checks"]["exact_calls_and_replay_receipts"], {"passed": 1, "total": 1})

        bad_call = (call[0].replace("工作坊", "管理员"), call[1], call[2])
        result = run_case(case, lambda *_: bad_call)
        self.assertFalse(result["checks"]["exact_calls_and_replay_receipts"])

    def test_application_envelope_validates_separately_from_bare_json(self):
        body = '{"thought":"先等。","speak":false,"text":""}'
        case = {"id": "quiet", "suite": "initiative", "messages": [{"role": "user", "content": "运行状态"}],
                "expect": {"speak": False}}
        output = f'```json\n{body}\n``` <|ACT {{"emotion":"neutral"}}|>'
        result = run_case(case, lambda *_: answer(output))
        self.assertTrue(result["passed"])
        self.assertFalse(result["strict_json"])
        self.assertTrue(result["checks"]["silent_text"])
        self.assertIsNone(reflection_decision(body.replace('false', '"false"')))
        self.assertIsNone(reflection_decision(body.replace('false', 'false,"speak":true')))

    def test_dialogue_scores_spoken_text_with_inline_airi_actions(self):
        case = {"id": "actions", "suite": "dialog", "messages": [{"role": "user", "content": "在吗？"}],
                "expect": {"groups": [["在呢"]], "max_chars": 5, "none": ["happy", "emotion"]}}
        output = '<|ACT {"emotion":"neutral"}|>在呢<|ACT {"emotion":"happy"}|>。'
        result = run_case(case, lambda *_: answer(output))
        self.assertTrue(result["passed"])
        malformed = output.replace('"happy"', 'happy')
        self.assertFalse(run_case(case, lambda *_: answer(malformed))["complete"])

    def test_batch_keeps_single_reply_and_pending_tool_loop_paired(self):
        environment = {"tool_replay": [{"name": "memory", "arguments": {"query": "卡片"}, "status": "finished", "result": "在抽屉。"}]}
        tool_case = {"id": "tool", "suite": "tool", "messages": [{"role": "user", "content": "查卡片位置。"}],
                     "tools": [{"function": {"name": "memory"}}], "environment": environment,
                     "environment_sha256": digest(environment), "expect": {"groups": [["抽屉"]]}}
        dialog = {"id": "dialog", "suite": "dialog", "messages": [{"role": "user", "content": "问早。"}],
                  "expect": {"groups": [["早"]]}}
        buffers = []

        def batch(requests):
            buffers.append([len(messages) for messages, _ in requests])
            results = []
            for messages, tools in requests:
                if messages[-1]["role"] == "tool":
                    self.assertEqual(messages[-1]["content"], "在抽屉。")
                    results.append(answer("卡片在抽屉。"))
                elif tools:
                    results.append(("先检索位置。<channel|><|tool_call>call:memory{query:<|\"|>卡片<|\"|>}<tool_call|><|tool_response>", 20, True))
                else:
                    results.append(answer("早上好。"))
            return results

        results = list(run_suite([tool_case, dialog], batch, 2))
        self.assertEqual([row["id"] for row in results], ["tool", "dialog"])
        self.assertTrue(all(result["passed"] for result in results))
        self.assertEqual(buffers, [[1, 1], [3]])

    def test_system_only_input_is_available_for_initiative(self):
        case = {"id": "auto", "suite": "initiative", "messages": [{"role": "system", "content": "应用禁止说话。"}],
                "expect": {"speak": False}}
        self.assertEqual(verify_suite([case], None)["cases"], 1)
        with self.assertRaisesRegex(ValueError, "Invalid case kind or missing final input"):
            verify_suite([{**case, "suite": "dialog"}], None)

    def test_consolidation_rejects_wrong_source_and_revision(self):
        current = {"path": "人物/甲.md", "text": "对方常用蓝夹。", "nature": "reported", "sources": ["user-evidence"],
                   "expectedRevision": 3}
        expected = {"documents": [{**current, "groups": [["对方"], ["蓝夹"]]}]}
        self.assertTrue(all(document_checks({"documents": [current]}, expected).values()))
        for invalid in ({**current, "sources": ["other-scope"]}, {**current, "expectedRevision": 4}):
            self.assertFalse(document_checks({"documents": [invalid]}, expected)["consolidation_source_and_revision"])
        self.assertFalse(document_checks({"documents": [current, current]}, expected)["consolidation_paths"])
        self.assertFalse(document_checks({"documents": [current]}, {"documents": []})["consolidation_paths"])


if __name__ == "__main__":
    unittest.main()
