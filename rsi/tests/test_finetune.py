"""Check cleaned training contracts and causal labels without loading model weights."""

import copy
import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from rsi.common import file_hash, read_json, write_json, write_jsonl
from rsi.finetune import _weight_manifest, check, configuration, environment_check, inspect_dataset, merge, row_hash, tokenize, train, validate_row


class NativeTokenizer:
    chat_template = "native <|channel>thought"
    eos_token_id = 1

    def __init__(self):
        self.calls = []

    def apply_chat_template(self, messages, tools=None, tokenize=False, add_generation_prompt=False,
                            continue_final_message=False, **kwargs):
        self.calls.append(copy.deepcopy(messages))
        result = "<bos>" + (json.dumps(tools, sort_keys=True, ensure_ascii=False) if tools else "")
        for message in messages:
            result += "<" + message["role"] + ">"
            if message.get("reasoning_content"):
                result += "<|channel>thought:" + message["reasoning_content"] + "<|channel>final:"
            result += message.get("content") or ""
            if message.get("tool_calls"):
                result += "<call>" + json.dumps(message["tool_calls"], sort_keys=True, ensure_ascii=False)
            result += "<end>"
        if continue_final_message:
            result = result.removesuffix("<end>")
        if add_generation_prompt:
            result += "<assistant>"
        return result

    def encode(self, value, add_special_tokens=False):
        return [ord(char) for char in value]


def cleaned_row(index):
    result = {"id": "row-" + str(index), "group": "group-" + str(index), "source_ids": ["source-" + str(index)],
              "scope": {"user_id": "local", "character_id": "default"}, "kind": "dialogue",
              "messages": [{"role": "system", "content": "系统"},
                           {"role": "user", "content": "独立问题" + str(index)},
                           {"role": "assistant", "content": "中文回答" + str(index)}], "train_indices": [2]}
    result["content_sha256"] = row_hash(result)
    return result


class FinetuneTests(unittest.TestCase):
    def setUp(self):
        temporary_root = Path(__file__).resolve().parents[1] / "private/test-temp"
        temporary_root.mkdir(parents=True, exist_ok=True)
        self.temporary = tempfile.TemporaryDirectory(dir=temporary_root)
        self.root = Path(self.temporary.name)
        self.config = {"model": str(self.root / "missing-model"), "minimum_groups": 3, "max_length": 4096}
        self.rows = {"train": [cleaned_row(0)], "validation": [cleaned_row(1)], "test": [cleaned_row(2)]}

    def tearDown(self):
        self.temporary.cleanup()

    def dataset(self):
        directory = self.root / "dataset"
        for split, rows in self.rows.items():
            for row in rows:
                row["content_sha256"] = row_hash(row)
            write_jsonl(directory / (split + ".jsonl"), rows)
        manifest = {"files": {split: file_hash(directory / (split + ".jsonl")) for split in self.rows},
                    "records": [{"id": row["id"], "split": split, "content_sha256": row["content_sha256"]}
                                for split, rows in self.rows.items() for row in rows]}
        write_json(directory / "manifest.json", manifest)
        return directory

    def test_inspection_accepts_independent_signed_rows(self):
        _, report = inspect_dataset(self.dataset(), 3)
        self.assertTrue(report["ready"])
        self.assertEqual(report["independent_groups"], 3)

    def test_changed_file_is_rejected_before_training(self):
        directory = self.dataset()
        with (directory / "train.jsonl").open("a", encoding="utf-8") as stream:
            stream.write("\n")
        with self.assertRaisesRegex(ValueError, "changed: train"):
            inspect_dataset(directory, 3)

    def test_changed_row_is_rejected_after_rebound_file(self):
        directory = self.dataset()
        self.rows["train"][0]["messages"][-1]["content"] = "改动"
        write_jsonl(directory / "train.jsonl", self.rows["train"])
        manifest = read_json(directory / "manifest.json")
        manifest["files"]["train"] = file_hash(directory / "train.jsonl")
        write_json(directory / "manifest.json", manifest)
        with self.assertRaisesRegex(ValueError, "row hash changed"):
            inspect_dataset(directory, 3)

    def test_manifest_record_cannot_refer_to_another_target(self):
        directory = self.dataset()
        manifest = read_json(directory / "manifest.json")
        manifest["records"][0]["content_sha256"] = "0" * 64
        write_json(directory / "manifest.json", manifest)
        with self.assertRaisesRegex(ValueError, "record bindings"):
            inspect_dataset(directory, 3)

    def test_source_and_group_leakage_is_rejected(self):
        for key in ("group", "source_ids"):
            with self.subTest(key=key):
                self.rows["test"][0] = cleaned_row(2)
                self.rows["test"][0][key] = self.rows["train"][0][key]
                with self.assertRaisesRegex(ValueError, "Cross-split"):
                    inspect_dataset(self.dataset(), 3)

    def test_identical_model_context_is_not_an_independent_holdout(self):
        self.rows["test"][0]["messages"][:-1] = copy.deepcopy(self.rows["train"][0]["messages"][:-1])
        with self.assertRaisesRegex(ValueError, "Identical model context"):
            inspect_dataset(self.dataset(), 3)

    def test_later_selected_target_cannot_leak_into_validation(self):
        row = self.rows["train"][0]
        row["messages"].extend([{"role": "user", "content": "追问"}, {"role": "assistant", "content": "后续答案"}])
        row["train_indices"] = [2, 4]
        self.rows["validation"][0]["messages"] = copy.deepcopy(row["messages"])
        self.rows["validation"][0]["train_indices"] = [4]
        with self.assertRaisesRegex(ValueError, "Identical model context"):
            inspect_dataset(self.dataset(), 3)

    def test_missing_weight_shard_and_adapter_only_directory_are_not_a_base(self):
        base = self.root / "weights"
        write_json(base / "model.safetensors.index.json", {"weight_map": {
            "layer.a": "model-00001-of-00002.safetensors", "layer.b": "model-00002-of-00002.safetensors"}})
        (base / "model-00001-of-00002.safetensors").write_bytes(b"first")
        with self.assertRaisesRegex(ValueError, "missing or empty"):
            _weight_manifest(base)
        (base / "model-00002-of-00002.safetensors").write_bytes(b"second")
        self.assertEqual(len(_weight_manifest(base)["weights"]), 2)
        adapter = self.root / "adapter-only"
        adapter.mkdir()
        (adapter / "adapter_model.safetensors").write_bytes(b"adapter")
        with self.assertRaisesRegex(ValueError, "Original Hugging Face"):
            _weight_manifest(adapter)

    def test_single_gpu_configuration_rejects_nonzero_or_boolean_device(self):
        for device in (1, True, -1):
            with self.subTest(device=device):
                with self.assertRaisesRegex(ValueError, "logical CUDA device 0"):
                    configuration({**self.config, "device": device})

    @unittest.skipUnless(os.name == "nt" and shutil.which("pwsh"), "PowerShell wrapper requires Windows")
    def test_powershell_method_is_only_an_explicit_override(self):
        probe = self.root / "python-probe.ps1"
        probe.write_text('$global:LASTEXITCODE = 0\nConvertTo-Json -InputObject @($args) -Compress\n', encoding="utf-8")
        wrapper = Path(__file__).resolve().parents[1] / "train-local.ps1"
        command = ["pwsh", "-NoProfile", "-File", str(wrapper), "-Dataset", "fixture", "-Config", "fixture", "-Python", str(probe)]
        for extra, override in (([], False), (["-Method", "lora"], True)):
            completed = subprocess.run(command + extra, capture_output=True, text=True, check=True,
                                       env={**os.environ, "CUDA_VISIBLE_DEVICES": "0"}, timeout=20)
            arguments = json.loads(completed.stdout)
            self.assertEqual("--method" in arguments, override)
            if override:
                self.assertEqual(arguments[arguments.index("--method") + 1], "lora")

    def test_missing_holdout_can_be_tokenized_but_cannot_train(self):
        self.rows["test"] = []
        encoded, report = tokenize(self.dataset(), self.config, NativeTokenizer())
        self.assertEqual(len(encoded["train"]), 1)
        self.assertIn("empty_split:test", report["dataset"]["errors"])
        self.assertFalse(report["ready_for_training"])

    def test_assistant_only_thoughts_keep_native_channels_and_mask_context(self):
        row = self.rows["train"][0]
        row["messages"] = [{"role": "system", "content": "自己的目标0"},
                           {"role": "assistant", "content": "整理记忆。", "reasoning_content": "先检查记录。"}]
        row["train_indices"] = [1]
        encoded, report = tokenize(self.dataset(), self.config, NativeTokenizer())
        sample = encoded["train"][0]
        target = "".join(chr(token) for token in sample["labels"] if token != -100)
        self.assertEqual(target, "<|channel>thought:先检查记录。<|channel>final:整理记忆。<end>")
        self.assertTrue(any(token == -100 for token in sample["labels"]))
        self.assertTrue(report["ready_for_training"])

    def test_each_selected_decision_keeps_an_independent_loss_mask(self):
        row = self.rows["train"][0]
        row["messages"].extend([{"role": "user", "content": "继续"}, {"role": "assistant", "content": "下一步"}])
        row["train_indices"] = [2, 4]
        encoded, report = tokenize(self.dataset(), self.config, NativeTokenizer())
        self.assertEqual(report["sequences"]["train"], 2)
        target = "".join(chr(token) for token in encoded["train"][1]["labels"] if token != -100)
        self.assertEqual(target, "下一步<end>")

    def test_tool_call_uses_native_dict_arguments_and_verified_receipt(self):
        row = self.rows["train"][0]
        row["tools"] = [{"type": "function", "function": {"name": "lookup", "parameters": {"type": "object"}}}]
        row["messages"][-1] = {"role": "assistant", "content": None, "tool_calls": [
            {"id": "call-1", "type": "function", "function": {"name": "lookup", "arguments": '{"query":"记忆"}'}}]}
        row["messages"].append({"role": "tool", "content": "记忆结果", "tool_call_id": "call-1"})
        tokenizer = NativeTokenizer()
        encoded, report = tokenize(self.dataset(), self.config, tokenizer)
        call_messages = [message for batch in tokenizer.calls for message in batch if message.get("tool_calls")]
        self.assertEqual(call_messages[0]["tool_calls"][0]["function"]["arguments"], {"query": "记忆"})
        target = "".join(chr(token) for token in encoded["train"][0]["labels"] if token != -100)
        self.assertIn("<call>", target)
        self.assertNotIn("记忆结果", target)
        self.assertTrue(report["ready_for_training"])
        row["messages"].pop()
        row["content_sha256"] = row_hash(row)
        with self.assertRaisesRegex(ValueError, "result evidence"):
            validate_row(row)

    def test_overlength_reports_full_size_without_truncation(self):
        self.rows["train"][0]["messages"][-1]["content"] = "长" * 300
        encoded, report = tokenize(self.dataset(), {**self.config, "max_length": 128}, NativeTokenizer())
        self.assertEqual(encoded["train"], [])
        self.assertGreater(report["errors"][0]["tokens"], 300)
        self.assertEqual(report["errors"][0]["target_index"], 2)
        self.assertEqual(report["errors"][0]["reason"], "sequence_exceeds_configured_context")
        self.assertFalse(report["ready_for_training"])

    def test_missing_cuda_blocks_training_even_with_package_metadata(self):
        versions = {name: version for name, version in (("torch", "2.9.1"), ("transformers", "5.17.0"),
                    ("peft", "0.19.0"), ("accelerate", "1.12.0"), ("bitsandbytes", "0.48.0"))}
        with patch("rsi.finetune._versions", return_value=versions), \
                patch("rsi.finetune._cuda_probe", return_value={"available": False, "kernel_ok": False}), \
                patch("rsi.finetune.subprocess.run", side_effect=FileNotFoundError):
            result = environment_check(configuration(self.config))
        self.assertIn("original_hf_weights_missing", result["blockers"])
        self.assertIn("selected_cuda_device_unavailable", result["blockers"])
        self.assertFalse(result["ready"])
        self.assertFalse(result["model_loaded"])

    def test_preflight_does_not_load_training_libraries_or_write_output_when_blocked(self):
        directory = self.dataset()
        output = self.root / "run"
        with patch("rsi.finetune.environment_check", return_value={"ready": False, "tokenizer_files_present": False,
                    "packages": {"transformers": None}, "blockers": ["original_hf_weights_missing"]}):
            result = check(directory, self.config)
            with self.assertRaisesRegex(ValueError, "Preflight is blocked"):
                train(directory, self.config, output)
        self.assertFalse(result["ready_for_training"])
        self.assertFalse(result["training_executed"])
        self.assertFalse(output.exists())

    def test_configuration_rejects_unsupported_training_and_accidental_large_epochs(self):
        for change in ({"method": "full"}, {"epochs": 100}, {"target_modules": ["vision_tower"]}, {"max_length": True}):
            with self.subTest(change=change):
                with self.assertRaises(ValueError):
                    configuration({**self.config, **change})

    def test_merge_refuses_existing_output_before_loading_weights(self):
        output = self.root / "merged"
        output.mkdir()
        with self.assertRaisesRegex(ValueError, "new merge output"):
            merge(self.config, self.root / "absent-adapter", output)

    def test_merge_refuses_a_different_base_before_loading_training_libraries(self):
        base, adapter, output = self.root / "base", self.root / "adapter", self.root / "merged"
        write_json(base / "config.json", {"model_type": "gemma4"})
        (base / "model.safetensors").write_bytes(b"test-weight-identity")
        write_json(adapter / "finetune-lineage.json", {"base": {"path": "another-base"}})
        with self.assertRaisesRegex(ValueError, "Merge base differs"):
            merge({**self.config, "model": str(base)}, adapter, output)
        self.assertFalse(output.exists())

    def test_source_cleaning_gate_cannot_be_bypassed_by_lowering_group_threshold(self):
        directory = self.dataset()
        manifest = read_json(directory / "manifest.json")
        manifest["readiness"] = {"ready": False, "reasons": ["insufficient_independent_groups"]}
        write_json(directory / "manifest.json", manifest)
        _, report = inspect_dataset(directory, 3)
        self.assertFalse(report["ready"])
        self.assertIn("source_cleaning_readiness_blocked", report["errors"])


if __name__ == "__main__":
    unittest.main()
