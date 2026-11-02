"""Single-device LoRA training, offline inference, and full-precision adapter merge."""

import argparse
import hashlib
import importlib.metadata
import json
import os
import platform
import re
import subprocess
from pathlib import Path

from pipeline import check_messages, check_tools, content_hash, digest, lines, read, write, write_lines


def preflight():
    versions = {}
    for name in ("torch", "transformers", "peft", "accelerate", "bitsandbytes"):
        try:
            versions[name] = importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:
            versions[name] = None
    try:
        gpu = subprocess.run(["nvidia-smi", "--query-gpu=index,name,memory.total,memory.free", "--format=csv"],
                             capture_output=True, text=True, timeout=10, check=True).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        gpu = "nvidia-smi unavailable"
    return {"python": platform.python_version(), "platform": platform.system(), "packages": versions,
            "gpu": gpu, "cuda_visible_devices": os.environ.get("CUDA_VISIBLE_DEVICES"),
            "note": "No model loaded. CUDA kernels and peak memory require an execution smoke test."}


def load_dataset(directory):
    root = Path(directory)
    manifest = read(root / "manifest.json")
    result = {}
    for split in ("train", "validation", "test"):
        path = root / (split + ".jsonl")
        if hashlib.sha256(path.read_bytes()).hexdigest() != manifest["files"][split]:
            raise ValueError("Dataset manifest mismatch")
        rows = list(lines(path))
        expected = {r["id"]: r["sha256"] for r in manifest["records"] if r["split"] == split}
        if len(rows) != len(expected) or {r["id"]: r["content_sha256"] for r in rows} != expected:
            raise ValueError("Dataset record manifest mismatch")
        for row in rows:
            if content_hash(row) != row["content_sha256"]:
                raise ValueError("Dataset content mismatch")
            check_messages(row["messages"])
            check_tools(row.get("tools", []))
        result[split] = rows
    if any(not rows for rows in result.values()):
        raise ValueError("All three dataset splits must be nonempty")
    return result


def encode(row, tokenizer, max_length, reasoning_format):
    messages = row["messages"]
    prompt_messages = messages[:-1]
    answer = dict(messages[-1])
    thinking = reasoning_format == "gemma4-native" and row["kind"] == "reasoning"
    tools = row.get("tools", [])
    if row["kind"] == "reasoning":
        if reasoning_format == "summary":
            tools = []
            prompt_messages = [{"role": "user", "content": "用简短中文概括已提供的思考。保留依据、行动和不确定性，不增加事实。\n" + row["reasoning_source"]}]
            answer["content"] = row["short_reasoning"]
        elif reasoning_format == "gemma4-native":
            if "<|channel>" not in str(tokenizer.chat_template):
                raise ValueError("Gemma 4 native reasoning requires the Gemma 4 chat template")
            answer["reasoning_content"] = row["short_reasoning"]
        else:
            raise ValueError("Reasoning records require an explicit target format")
    prompt = tokenizer.apply_chat_template(prompt_messages, tools=tools or None, tokenize=False, add_generation_prompt=True, enable_thinking=thinking)
    full = tokenizer.apply_chat_template([*prompt_messages, answer], tools=tools or None, tokenize=False, add_generation_prompt=False, enable_thinking=thinking)
    if not full.startswith(prompt):
        raise ValueError("Template prompt and answer boundaries disagree")
    prompt_ids = tokenizer.encode(prompt, add_special_tokens=False)
    full_ids = tokenizer.encode(full, add_special_tokens=False)
    if full_ids[:len(prompt_ids)] != prompt_ids:
        raise ValueError("Tokenizer merged across the response boundary")
    answer_ids = full_ids[len(prompt_ids):]
    if not answer_ids:
        raise ValueError("Template removed the answer")
    if len(prompt_ids) + len(answer_ids) > max_length:
        return None
    return {"input_ids": prompt_ids + answer_ids, "attention_mask": [1] * (len(prompt_ids) + len(answer_ids)),
            "labels": [-100] * len(prompt_ids) + answer_ids}


def model_identity(directory):
    root = Path(directory)
    if not (root / "config.json").is_file():
        raise ValueError("Use a local Hugging Face model directory with config.json")
    files = sorted([*root.glob("*.safetensors"), *root.glob("*.bin")])
    if not files:
        raise ValueError("Hugging Face weights are missing. GGUF files are inference artifacts")
    weights = []
    for path in files:
        sha = hashlib.sha256()
        with path.open("rb") as stream:
            for block in iter(lambda: stream.read(8 * 1024 * 1024), b""):
                sha.update(block)
        weights.append({"name": path.name, "bytes": path.stat().st_size, "sha256": sha.hexdigest()})
    return {"path": str(root.resolve()), "config_sha256": hashlib.sha256((root / "config.json").read_bytes()).hexdigest(), "weights": weights}


def validate_config(config):
    if not 1 <= config["rank"] <= 64 or not 128 <= config["max_length"] <= 4096:
        raise ValueError("Rank or context exceeds the bounded experiment range")
    if not 0 < config["learning_rate"] <= 0.0003 or not 1 <= config["epochs"] <= 3:
        raise ValueError("Learning rate or epochs exceed the bounded experiment range")
    if config["reasoning_format"] not in {"none", "summary", "gemma4-native"}:
        raise ValueError("Invalid reasoning format")
    if not isinstance(config["qlora"], bool) or config.get("gradient_accumulation", 16) < 1:
        raise ValueError("Invalid quantization or accumulation configuration")


def model_class(path):
    from transformers import AutoModelForCausalLM, AutoModelForImageTextToText

    return AutoModelForImageTextToText if read(Path(path) / "config.json").get("model_type") == "gemma4" else AutoModelForCausalLM


def train(config, dataset, output, execute=False):
    validate_config(config)
    identity = model_identity(config["model"])
    rows = load_dataset(dataset)
    plan = {"base": identity, "config": config, "dataset_sha256": digest(read(Path(dataset) / "manifest.json")),
            "records": {k: len(v) for k, v in rows.items()}, "execute": execute}
    if not execute:
        return plan
    out = Path(output)
    if out.exists():
        raise ValueError("Use a new training output directory")
    import torch
    from peft import LoraConfig, get_peft_model, prepare_model_for_kbit_training
    from transformers import AutoTokenizer, BitsAndBytesConfig, Trainer, TrainingArguments, set_seed

    if not torch.cuda.is_available():
        raise ValueError("CUDA training is required")
    set_seed(config.get("seed", 42))
    device = config.get("device", 0)
    if device >= torch.cuda.device_count():
        raise ValueError("Selected CUDA device is not visible")
    torch.cuda.set_device(device)
    dtype = torch.bfloat16 if torch.cuda.is_bf16_supported() else torch.float16
    tokenizer = AutoTokenizer.from_pretrained(config["model"], local_files_only=True, trust_remote_code=False)
    if tokenizer.pad_token_id is None:
        tokenizer.pad_token = tokenizer.eos_token
    if tokenizer.eos_token_id is None or not tokenizer.chat_template:
        raise ValueError("Model needs EOS and a chat template")
    encoded, dropped = {}, {}
    for split in ("train", "validation"):
        values = [encode(row, tokenizer, config["max_length"], config["reasoning_format"]) for row in rows[split]]
        encoded[split] = [value for value in values if value is not None]
        dropped[split] = len(values) - len(encoded[split])
        if not encoded[split] or dropped[split] / len(values) > 0.1:
            raise ValueError("More than 10% of records exceed context. Rebuild the dataset instead of truncating answers")
    quantization = BitsAndBytesConfig(load_in_4bit=True, bnb_4bit_quant_type="nf4", bnb_4bit_use_double_quant=True,
                                     bnb_4bit_compute_dtype=dtype) if config["qlora"] else None
    model = model_class(config["model"]).from_pretrained(config["model"], local_files_only=True, trust_remote_code=False,
                                                torch_dtype=dtype, device_map={"": device}, quantization_config=quantization,
                                                attn_implementation="sdpa")
    model.config.use_cache = False
    if config["qlora"]:
        model = prepare_model_for_kbit_training(model, use_gradient_checkpointing=True)
    gemma4 = read(Path(config["model"]) / "config.json").get("model_type") == "gemma4"
    targets = config.get("target_modules", ["q_proj", "v_proj"])
    if not isinstance(targets, list) or not targets or set(targets) - {"q_proj", "k_proj", "v_proj", "o_proj"}:
        raise ValueError("Use attention projection names for LoRA targets")
    if gemma4:
        targets = r".*language_model\.layers\.\d+\.self_attn\.(" + "|".join(targets) + ")"
    model = get_peft_model(model, LoraConfig(r=config["rank"], lora_alpha=config["rank"] * 2,
                                           lora_dropout=0.05, target_modules=targets,
                                           task_type="CAUSAL_LM", bias="none"))
    model.enable_input_require_grads()

    def collate(batch):
        length = max(len(item["input_ids"]) for item in batch)
        return {key: torch.tensor([item[key] + [pad] * (length - len(item[key])) for item in batch])
                for key, pad in (("input_ids", tokenizer.pad_token_id), ("attention_mask", 0), ("labels", -100))}

    args = TrainingArguments(output_dir=str(out), num_train_epochs=config["epochs"], learning_rate=config["learning_rate"],
                             per_device_train_batch_size=1, per_device_eval_batch_size=1,
                             gradient_accumulation_steps=config.get("gradient_accumulation", 16),
                             gradient_checkpointing=True, gradient_checkpointing_kwargs={"use_reentrant": False},
                             bf16=dtype == torch.bfloat16, fp16=dtype == torch.float16, optim="adamw_torch",
                             eval_strategy="epoch", save_strategy="epoch", save_total_limit=2,
                             logging_steps=5, report_to=[], dataloader_num_workers=0, remove_unused_columns=False,
                             seed=config.get("seed", 42), warmup_ratio=0.05, weight_decay=0.01)
    trainer = Trainer(model=model, args=args, train_dataset=encoded["train"], eval_dataset=encoded["validation"], data_collator=collate)
    trainer.train()
    model.save_pretrained(out / "adapter", safe_serialization=True)
    tokenizer.save_pretrained(out / "adapter")
    write(out / "adapter/base-identity.json", identity)
    plan.update({"dropped_overlength": dropped, "tokens": {s: sum(len(r["input_ids"]) for r in rs) for s, rs in encoded.items()},
                 "target_tokens": {s: sum(sum(x != -100 for x in r["labels"]) for r in rs) for s, rs in encoded.items()},
                 "metrics": trainer.evaluate(), "environment": preflight()})
    write(out / "run.json", plan)
    return plan


def infer(model_path, adapter, suite, output, qlora=False, device=0):
    import torch
    from peft import PeftModel
    from transformers import AutoTokenizer, BitsAndBytesConfig

    tokenizer = AutoTokenizer.from_pretrained(model_path, local_files_only=True)
    torch.cuda.set_device(device)
    dtype = torch.bfloat16 if torch.cuda.is_bf16_supported() else torch.float16
    quantization = BitsAndBytesConfig(load_in_4bit=True, bnb_4bit_quant_type="nf4", bnb_4bit_compute_dtype=dtype) if qlora else None
    model = model_class(model_path).from_pretrained(model_path, local_files_only=True, torch_dtype=dtype,
                                                   quantization_config=quantization, device_map={"": device}, attn_implementation="sdpa")
    if adapter:
        if read(Path(adapter) / "base-identity.json") != model_identity(model_path):
            raise ValueError("Adapter base weights changed")
        model = PeftModel.from_pretrained(model, adapter, local_files_only=True)
    model.eval()
    predictions = []
    with torch.inference_mode():
        for case in lines(suite):
            prompt = tokenizer.apply_chat_template(case["messages"], add_generation_prompt=True, tokenize=False,
                                                    tools=case.get("tools") or None,
                                                    enable_thinking=case.get("enable_thinking", False))
            ids = tokenizer(prompt, add_special_tokens=False, return_tensors="pt")["input_ids"].to(model.device)
            result = model.generate(ids, max_new_tokens=case.get("max_new_tokens", 512), do_sample=False,
                                    pad_token_id=tokenizer.eos_token_id)
            generated = result[0, ids.shape[1]:]
            decoded = tokenizer.decode(generated, skip_special_tokens=False)
            thought = re.search(r"<\|channel>thought\n(.*?)\n<channel\|>", decoded, re.S)
            final = re.sub(r"<\|channel>thought\n.*?\n<channel\|>", "", decoded, flags=re.S)
            for token in tokenizer.all_special_tokens:
                final = final.replace(token, "")
            stops = model.generation_config.eos_token_id
            stops = stops if isinstance(stops, list) else [stops]
            predictions.append({"id": case["id"], "text": final.strip(), "reasoning_summary": thought.group(1) if thought else "",
                                "complete": bool(len(generated) and generated[-1].item() in stops), "tokens": len(generated)})
    write_lines(output, predictions)


def merge(base, adapter, output, execute=False):
    identity = model_identity(base)
    config = read(Path(adapter) / "adapter_config.json")
    saved = Path(config["base_model_name_or_path"]).resolve()
    if saved != Path(base).resolve():
        raise ValueError("Adapter base path does not match the selected base")
    if read(Path(adapter) / "base-identity.json") != identity:
        raise ValueError("Adapter base weights changed")
    if Path(output).exists():
        raise ValueError("Merge output must be a new directory")
    if not execute:
        return {"base": identity, "adapter": adapter, "output": output, "execute": False}
    import torch
    from peft import PeftModel
    from transformers import AutoProcessor, AutoTokenizer

    # Merge against original full-precision weights. Quantization is a separate deployment step.
    model = model_class(base).from_pretrained(base, local_files_only=True, torch_dtype=torch.bfloat16, device_map={"": "cpu"})
    merged = PeftModel.from_pretrained(model, adapter, local_files_only=True).merge_and_unload(safe_merge=True)
    merged.save_pretrained(output, safe_serialization=True)
    AutoTokenizer.from_pretrained(base, local_files_only=True).save_pretrained(output)
    if read(Path(base) / "config.json").get("model_type") == "gemma4":
        AutoProcessor.from_pretrained(base, local_files_only=True).save_pretrained(output)
    write(Path(output) / "merge.json", {"base": identity, "adapter_config_sha256": digest(config)})
    return {"output": output, "merged": True}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("preflight")
    sub = commands.add_parser("train")
    sub.add_argument("--config", required=True)
    sub.add_argument("--dataset", required=True)
    sub.add_argument("--output", required=True)
    sub.add_argument("--execute", action="store_true")
    sub = commands.add_parser("merge")
    sub.add_argument("--base", required=True)
    sub.add_argument("--adapter", required=True)
    sub.add_argument("--output", required=True)
    sub.add_argument("--execute", action="store_true")
    sub = commands.add_parser("infer")
    sub.add_argument("--model", required=True)
    sub.add_argument("--adapter")
    sub.add_argument("--suite", required=True)
    sub.add_argument("--output", required=True)
    sub.add_argument("--execute", action="store_true")
    sub.add_argument("--qlora", action="store_true")
    sub.add_argument("--device", type=int, default=0)
    args = parser.parse_args()
    if args.command == "preflight":
        result = preflight()
    elif args.command == "train":
        result = train(read(args.config), args.dataset, args.output, args.execute)
    elif args.command == "merge":
        result = merge(args.base, args.adapter, args.output, args.execute)
    elif args.execute:
        infer(args.model, args.adapter, args.suite, args.output, args.qlora, args.device)
        result = {"output": args.output}
    else:
        result = {"execute": False, "note": "Add --execute to load the model and generate evaluation responses"}
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
