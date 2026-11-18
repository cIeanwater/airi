import tempfile
import unittest
from pathlib import Path

from rsi.common import digest, file_hash, write_json, write_jsonl
from rsi.repair_release import verify_reviews


class ReviewBindingTests(unittest.TestCase):
    def test_only_supervision_bound_to_real_actor_files_is_accepted(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            inp, out, ledger = (root / name for name in ("in.jsonl", "out.jsonl", "ledger.jsonl"))
            row = {"id": "sample", "messages": [{"role":"user", "content":"你好", "loss_weight":0},
                   {"role":"assistant","content":"你好呀","loss_weight":1}]}
            corrected = {**row, "messages":[row["messages"][0],{**row["messages"][1],"content":"嗨！"}]}
            trace = root / (digest(["sample",1]) + ".json")
            write_jsonl(inp,[row])
            write_jsonl(out,[corrected])
            write_json(trace,{"binding":{"id":"sample","index":1,"candidate":digest(row)}, "text":"嗨"})
            entry = {"id":"sample","index":1,"candidate_sha256":digest(row),"actor_file":trace.name,
                     "actor_sha256":file_hash(trace),"reviewed_row_sha256":digest(corrected),
                     "reviewed_target_sha256":digest(corrected["messages"][1]),"note":"此处需要问候。"}
            write_jsonl(ledger,[entry])
            self.assertEqual(verify_reviews(out,inp,ledger,root)["decisions"],1)
            write_jsonl(ledger,[entry,entry])
            with self.assertRaisesRegex(ValueError,"duplicate"):
                verify_reviews(out,inp,ledger,root)
            write_jsonl(ledger,[entry])
            write_json(trace,{"binding":{"id":"other"},"text":"嗨"})
            with self.assertRaisesRegex(ValueError,"Broken review binding"):
                verify_reviews(out,inp,ledger,root)
