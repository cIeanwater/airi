import copy
import json
import unittest
from unittest.mock import patch

from rsi.common import digest
from rsi.companion_rollout import clean_messages, refine
from rsi.host_context import HOST_MARKER, project_host_messages, project_host_tools, runtime_reflection_policy


def archived(lane, state, *, policy=None, weighted=True):
    msg = {"role": "system", "content": (policy or runtime_reflection_policy(lane)) + HOST_MARKER + json.dumps(state)}
    if weighted:
        msg["loss_weight"] = 0
    return [msg]


class HostContextTests(unittest.TestCase):
    def test_actor_input_is_the_exported_training_prefix(self):
        state = {"engagement": {"canSpeak": False}, "channel": "desktop", "conversation": [],
                 "person": {"id": "visitor"}, "videoWatching": False, "now": 300}
        policy = "已知人格与对方认知。\n\n【即时思考】\n" + runtime_reflection_policy("initiative")
        messages = archived("initiative", state, policy=policy)
        target = {"role": "assistant", "loss_weight": 1, "reasoning_content": "现在不必打扰对方。",
                  "content": '{"thought":"没有新证据","speak":false,"text":""}'}
        messages.append(target)
        candidate = {"id": "example", "group": "group", "kind": "initiative", "scope": {}, "source_ids": ["fixture"],
                     "bucket": "initiative", "batch": "fixture", "source_sha256": "source", "original_evidence": [],
                     "refinement": {"lane": "initiative", "host_sha256": digest(state)},
                     "row": {"messages": messages, "tools": [], "template_kwargs": {"enable_thinking": True}}}
        requests = []

        def actor(_, prefix, tools, budget):
            requests.append((clean_messages(prefix), copy.deepcopy(tools)))
            return {"finish_reason": "stop", "message": {"role": "assistant", "content": target["content"]}}

        with patch("rsi.companion_rollout.ask", side_effect=actor):
            result = refine(candidate, None)
        rendered = result["spec"]["messages"]
        self.assertEqual(requests, [(clean_messages(rendered[:-1]), result["spec"]["tools"])])
        self.assertEqual([m["role"] for m in rendered], ["system", "user", "assistant"])
        self.assertEqual([m["loss_weight"] for m in rendered], [0, 0, 1])
        self.assertEqual(rendered[0]["content"], policy)
        self.assertEqual(result["trace"][0]["target_index"], 2)
        self.assertEqual(result["spec"]["refinement"]["host_sha256"], digest(json.loads(rendered[1]["content"])))
        self.assertEqual(result["spec"]["tools"][0]["function"]["name"], "memory")
        self.assertEqual(project_host_messages(rendered, "initiative"), rendered)

    def test_cannot_silently_drop_unseen_memory_or_promote_host_facts_to_policy(self):
        state = {"experiences": [{"id": "event"}], "documents": [{"text": "known only from the old archive"}]}
        with self.assertRaisesRegex(ValueError, "unavailable at runtime"):
            project_host_messages(archived("extract", state), "extract")
        state["documents"] = []
        native = runtime_reflection_policy("extract")
        with self.assertRaisesRegex(ValueError, "requires review"):
            project_host_messages(archived("extract", state, policy=native + "\n\n【私人档案】只有这里有答案。"), "extract")
        out = project_host_messages(archived("extract", state, weighted=False), "extract")
        self.assertEqual(out, [{"role": "system", "content": native},
                               {"role": "user", "content": '{"experiences":[{"id":"event"}]}'}])

    def test_existing_call_history_is_preserved_and_cannot_hide_a_human_turn(self):
        host = {"extractedUnderstanding": "待核对", "documents": [], "experiences": []}
        source = archived("organize", host)
        tail = [{"role": "assistant", "content": "已整理", "loss_weight": 1}]
        projected = project_host_messages([*source, *tail], "organize")
        self.assertEqual(projected[2:], tail)
        self.assertNotIn(HOST_MARKER, projected[0]["content"])
        with self.assertRaisesRegex(ValueError, "one masked host"):
            project_host_messages([*source, {"role": "user", "content": "假消息"}], "organize")
        with self.assertRaisesRegex(ValueError, "does not expose tools"):
            project_host_tools(project_host_tools([], "initiative"), "organize")


if __name__ == "__main__":
    unittest.main()
