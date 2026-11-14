"""Check that later training inputs match earlier outputs."""

import copy
import unittest

from rsi.common import canonical, digest
from rsi.companion_links import check_companion_links


def row(identity, messages, **refinement):
    return {"id": identity, "group": "scenario", "messages": messages, "refinement": refinement}


def message(role, content, weight=0):
    return {"role": role, "content": content, "loss_weight": weight}


class CompanionLinkTests(unittest.TestCase):
    def test_document_link_requires_the_applied_revision_and_prompt(self):
        document = {"path": "people/a.md", "text": "偏好静音。", "sources": ["event-1"]}
        result = {"documents": [{**document, "expectedRevision": 2}]}
        organizer = row("organize", [message("assistant", canonical(result).decode(), 1)])
        expected = {**document, "revision": 3}
        prompt = "【对话对象认知】\n偏好静音。"
        dialogue = row("dialogue", [message("system", prompt)], downstream_of="organize", consumed_document=expected,
                       downstream_context_sha256=digest(prompt))
        self.assertEqual(check_companion_links([organizer, dialogue])["applied_documents"], 1)
        stale = copy.deepcopy(dialogue)
        stale["refinement"]["consumed_document"]["revision"] = 2
        with self.assertRaisesRegex(ValueError, "preceding revision"):
            check_companion_links([organizer, stale])
        altered = copy.deepcopy(dialogue)
        altered["messages"][0]["content"] = "偏好欢快音乐。"
        with self.assertRaisesRegex(ValueError, "lacks the applied document"):
            check_companion_links([organizer, altered])

    def test_only_delivered_proposal_enters_dialogue_history(self):
        proposal = {"thought": "接住新话题。", "speak": True, "text": "门后藏着什么呀？"}
        initiative = row("initiative", [message("assistant", canonical(proposal).decode(), 1)])
        followup = row("followup", [message("assistant", proposal["text"])], downstream_of="initiative",
                       observed_decision=proposal, previous_delivery={"state": "delivered", "event_id": "sent-1", "text": proposal["text"]})
        self.assertEqual(check_companion_links([initiative, followup])["recorded_decisions"], 1)
        followup["refinement"]["previous_delivery"] = {"state": "not_sent"}
        with self.assertRaisesRegex(ValueError, "Undelivered proposal"):
            check_companion_links([initiative, followup])

    def test_followup_cannot_use_a_later_or_different_group_output(self):
        parent = row("organize", [])
        child = row("followup", [], downstream_of="organize")
        with self.assertRaisesRegex(ValueError, "Parent must appear first"):
            check_companion_links([child, parent])
        parent["group"] = "unrelated"
        with self.assertRaisesRegex(ValueError, "same timeline group"):
            check_companion_links([parent, child])


if __name__ == "__main__":
    unittest.main()
