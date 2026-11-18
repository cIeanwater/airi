"""Optional offline Transformers backend for actor and proposer adapters."""

import importlib.metadata
import os
import platform
import re
import subprocess
import time
from pathlib import Path

from .common import digest, file_hash, read_json, read_jsonl, tree_hash, write_json, write_jsonl


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
        gpu = "unavailable"
    return {"python": platform.python_version(), "packages": versions, "gpu": gpu,
            "cuda_visible_devices": os.environ.get("CUDA_VISIBLE_DEVICES"), "model_loaded": False}


def validate_config(config):
    required = {"model", "device", "qlora", "rank", "max_length", "learning_rate", "max_steps"}
    if not required <= config.keys() or set(config) - required - {"seed", "gradient_accumulation", "target_modules", "scope"}:
        raise ValueError("Unknown or missing training configuration")
    for key, low, high in (("rank", 1, 64), ("max_length", 128, 4096), ("max_steps", 1, 256),
                           ("device", 0, 32), ("gradient_accumulation", 1, 256)):
        value = config.get(key, 16)
        if type(value) is not int or not low <= value <= high:
            raise ValueError("Training setting exceeds its budget: " + key)
    if not 0 < config["learning_rate"] <= 0.0003 or type(config["qlora"]) is not bool:
        raise ValueError("Invalid learning rate or quantization setting")
    targets = config.get("target_modules", ["q_proj", "v_proj"])
    if not isinstance(targets, list) or not targets or set(targets) - {"q_proj", "k_proj", "v_proj", "o_proj"}:
        raise ValueError("Only attention projections are supported")
    if not isinstance(config["model"], str) or not config["model"]:
        raise ValueError("Use a local Hugging Face model directory")
    if config.get("scope") is not None and (not isinstance(config["scope"], dict) or set(config["scope"]) != {"user_id", "character_id"} or not all(isinstance(v, str) and v for v in config["scope"].values())):
        raise ValueError("Training scope must identify one user and character")
    return config


def model_identity(directory):
    root = Path(directory).resolve(strict=True)
    config = read_json(root / "config.json")
    weights = sorted([*root.glob("*.safetensors"), *root.glob("pytorch_model*.bin")])
    if not weights:
        raise ValueError("Training needs original Hugging Face weights. GGUF files are inference artifacts")
    return {"path": str(root), "config_sha256": digest(config),
            "weights": [{"name": path.name, "sha256": file_hash(path)} for path in weights]}


def load_dataset(directory):
    root = Path(directory)
    manifest = read_json(root / "manifest.json")
    rows = {}
    for split in ("train", "validation", "test"):
        path = root / (split + ".jsonl")
        if file_hash(path) != manifest["files"][split]:
            raise ValueError("Dataset changed after assembly")
        rows[split] = read_jsonl(path)
        if not rows[split]:
            raise ValueError("All dataset splits must be nonempty")
    return rows, digest(manifest)


def train(config, dataset, output, *, parent=None, role="actor", execute=False):
    validate_config(config)
    if role not in {"actor", "proposer"}:
        raise ValueError("Unknown model role")
    plan = {"role": role, "config": config, "parent": str(parent) if parent else None, "execute": execute}
    if not execute:
        return plan
    out = Path(output)
    if out.exists():
        raise ValueError("Use a new training directory")
    rows, dataset_sha = load_dataset(dataset)
    if role == "actor" and (not config.get("scope") or any(row["scope"] != config["scope"] for split in rows.values() for row in split
                                                           if row.get("context", {}).get("dataset_kind") != "base")):
        raise ValueError("Actor training needs one fixed scope matching its personalized episodes")
    identity = model_identity(config["model"])
    if parent:
        previous = read_json(Path(parent) / "lineage.json")
        if previous["base"] != identity or previous["role"] != role or previous["rank"] != config["rank"] or previous.get("scope") != config.get("scope"):
            raise ValueError("Parent adapter base, role or rank changed")
    from .dataset import compile_episode, compile_messages
    import torch
    from peft import LoraConfig, PeftModel, get_peft_model, prepare_model_for_kbit_training
    from transformers import AutoModelForCausalLM, AutoModelForImageTextToText, AutoTokenizer, BitsAndBytesConfig, Trainer, TrainingArguments, set_seed

    if not torch.cuda.is_available() or config["device"] >= torch.cuda.device_count():
        raise ValueError("Selected CUDA device is unavailable")
    torch.cuda.set_device(config["device"])
    set_seed(config.get("seed", 42))
    dtype = torch.bfloat16 if torch.cuda.is_bf16_supported() else torch.float16
    tokenizer = AutoTokenizer.from_pretrained(config["model"], local_files_only=True, trust_remote_code=False)
    if not tokenizer.chat_template or tokenizer.eos_token_id is None:
        raise ValueError("Model needs a native chat template and EOS token")
    if tokenizer.pad_token_id is None:
        tokenizer.pad_token = tokenizer.eos_token
    encoded = {}
    for split in ("train", "validation"):
        encoded[split] = []
        for row in rows[split]:
            if role == "actor":
                encoded[split].extend(compile_episode(row, tokenizer, config["max_length"]))
            else:
                encoded[split].append(compile_messages(row["messages"], tokenizer, config["max_length"]))
        if not encoded[split]:
            raise ValueError("No eligible assistant decisions in " + split)
    gemma = read_json(Path(config["model"]) / "config.json").get("model_type") == "gemma4"
    quantization = BitsAndBytesConfig(load_in_4bit=True, bnb_4bit_quant_type="nf4", bnb_4bit_use_double_quant=True,
                                     bnb_4bit_compute_dtype=dtype) if config["qlora"] else None
    loader = AutoModelForImageTextToText if gemma else AutoModelForCausalLM
    model = loader.from_pretrained(config["model"], local_files_only=True, trust_remote_code=False,
                                   torch_dtype=dtype, device_map={"": config["device"]},
                                   quantization_config=quantization, attn_implementation="sdpa")
    model.config.use_cache = False
    if config["qlora"]:
        model = prepare_model_for_kbit_training(model, use_gradient_checkpointing=True)
    if parent:
        model = PeftModel.from_pretrained(model, parent, is_trainable=True, local_files_only=True)
    else:
        targets = config.get("target_modules", ["q_proj", "v_proj"])
        if gemma:
            targets = r".*language_model\.layers\.\d+\.self_attn\.(" + "|".join(targets) + ")"
        model = get_peft_model(model, LoraConfig(r=config["rank"], lora_alpha=2 * config["rank"],
                                               target_modules=targets, lora_dropout=0.05, bias="none", task_type="CAUSAL_LM"))
    model.enable_input_require_grads()

    def collate(batch):
        length = max(len(item["input_ids"]) for item in batch)
        return {key: torch.tensor([item[key] + [pad] * (length - len(item[key])) for item in batch])
                for key, pad in (("input_ids", tokenizer.pad_token_id), ("attention_mask", 0), ("labels", -100))}

    args = TrainingArguments(output_dir=str(out), max_steps=config["max_steps"], learning_rate=config["learning_rate"],
                             per_device_train_batch_size=1, per_device_eval_batch_size=1,
                             gradient_accumulation_steps=config.get("gradient_accumulation", 16),
                             gradient_checkpointing=True, gradient_checkpointing_kwargs={"use_reentrant": False},
                             bf16=dtype == torch.bfloat16, fp16=dtype == torch.float16, optim="adamw_torch",
                             save_strategy="no", eval_strategy="no", logging_steps=5, report_to=[],
                             dataloader_num_workers=0, remove_unused_columns=False, seed=config.get("seed", 42))
    trainer = Trainer(model=model, args=args, train_dataset=encoded["train"], eval_dataset=encoded["validation"], data_collator=collate)
    started = time.monotonic()
    trainer.train()
    model.save_pretrained(out / "adapter", safe_serialization=True)
    tokenizer.save_pretrained(out / "adapter")
    lineage = {"base": identity, "role": role, "rank": config["rank"], "parent_sha256": tree_hash(parent) if parent else None,
               "dataset_sha256": dataset_sha, "scope": config.get("scope")}
    write_json(out / "adapter/lineage.json", lineage)
    result = {**plan, "lineage": lineage, "artifact": str((out / "adapter").resolve()),
              "artifact_sha256": tree_hash(out / "adapter"), "training_seconds": time.monotonic() - started,
              "target_tokens": sum(sum(t != -100 for t in row["labels"]) for row in encoded["train"]),
              "validation": trainer.evaluate(), "environment": preflight()}
    write_json(out / "run.json", result)
    return result


def infer(config, cases, output, *, adapter=None, actor_version=None, execute=False):
    validate_config(config)
    if not execute:
        return {"execute": False, "cases": len(cases)}
    import torch
    from peft import PeftModel
    from transformers import AutoModelForCausalLM, AutoModelForImageTextToText, AutoTokenizer, BitsAndBytesConfig

    identity = model_identity(config["model"])
    if adapter and read_json(Path(adapter) / "lineage.json")["base"] != identity:
        raise ValueError("Inference base differs from the trained adapter")
    version = tree_hash(adapter) if adapter else digest(identity)
    if actor_version and actor_version != version:
        raise ValueError("Configured actor version differs from the actual artifact")
    torch.cuda.set_device(config["device"])
    dtype = torch.bfloat16 if torch.cuda.is_bf16_supported() else torch.float16
    gemma = read_json(Path(config["model"]) / "config.json").get("model_type") == "gemma4"
    loader = AutoModelForImageTextToText if gemma else AutoModelForCausalLM
    quantization = BitsAndBytesConfig(load_in_4bit=True, bnb_4bit_quant_type="nf4", bnb_4bit_compute_dtype=dtype) if config["qlora"] else None
    model = loader.from_pretrained(config["model"], local_files_only=True, torch_dtype=dtype,
                                    device_map={"": config["device"]}, quantization_config=quantization, attn_implementation="sdpa")
    if adapter:
        model = PeftModel.from_pretrained(model, adapter, local_files_only=True)
    tokenizer = AutoTokenizer.from_pretrained(config["model"], local_files_only=True)
    model.eval()
    predictions = []
    evidence = []
    with torch.inference_mode():
        for case in cases:
            from .replay import ReplaySession, parse_calls

            started = time.monotonic()
            messages = [{k: v for k, v in m.items() if k in {"role", "content", "reasoning_content", "tool_calls", "tool_call_id"}} for m in case["messages"]]
            session = ReplaySession(case)
            total_tokens, emitted_calls, thoughts, text, complete = 0, [], [], "", False
            for step in range(min(case.get("max_steps", 6), 12)):
                prompt = tokenizer.apply_chat_template(messages, tools=case.get("tools") or None, tokenize=False,
                                                        add_generation_prompt=True, enable_thinking=case.get("enable_thinking", False))
                ids = tokenizer(prompt, add_special_tokens=False, return_tensors="pt")["input_ids"].to(model.device)
                if ids.shape[1] > config["max_length"] or total_tokens >= 2048:
                    break
                generated = model.generate(ids, max_new_tokens=min(case.get("max_new_tokens", 512), 2048 - total_tokens), do_sample=False,
                                            pad_token_id=tokenizer.eos_token_id)[0, ids.shape[1]:]
                total_tokens += len(generated)
                raw = tokenizer.decode(generated, skip_special_tokens=False)
                thought = re.search(r"<\|channel>thought\n(.*?)\n<channel\|>", raw, re.S)
                if thought:
                    thoughts.append(thought.group(1))
                text = re.sub(r"<\|channel>thought\n.*?\n<channel\|>", "", raw, flags=re.S)
                try:
                    calls = parse_calls(text, prefix="call-" + str(step))
                except ValueError:
                    break
                text = re.sub(r"<\|tool_call>.*?<tool_call\|>", "", text, flags=re.S)
                for token in tokenizer.all_special_tokens:
                    text = text.replace(token, "")
                if not calls:
                    stops = model.generation_config.eos_token_id
                    stops = stops if isinstance(stops, list) else [stops]
                    complete = bool(len(generated) and generated[-1].item() in stops)
                    break
                emitted_calls.extend(calls)
                messages.append({"role": "assistant", "content": text.strip(), "reasoning_content": thought.group(1) if thought else "", "tool_calls": calls})
                try:
                    for call in calls:
                        result = session.execute(call)
                        messages.append({"role": "tool", "tool_call_id": call["id"],
                                         "content": result if isinstance(result, str) else __import__("json").dumps(result, ensure_ascii=False)})
                except ValueError:
                    break
            prediction = {"id": case["id"], "actor_version": version, "text": text.strip(), "tool_calls": emitted_calls,
                          "reasoning_summary": "\n".join(thoughts), "tokens": total_tokens,
                          "latency_ms": (time.monotonic() - started) * 1000, "complete": complete}
            predictions.append(prediction)
            evidence.extend(session.receipts(case, prediction))
    write_jsonl(output, predictions)
    write_jsonl(Path(output).with_suffix(".evidence.jsonl"), evidence)
    return {"predictions": len(predictions), "actor_version": version, "receipts": len(evidence)}


def merge(config, adapter, output, *, execute=False):
    if not execute:
        return {"execute": False, "adapter": str(adapter), "output": str(output)}
    if Path(output).exists():
        raise ValueError("Merge output must be a new directory")
    identity = model_identity(config["model"])
    if read_json(Path(adapter) / "lineage.json")["base"] != identity:
        raise ValueError("Adapter base differs from merge base")
    import torch
    from peft import PeftModel
    from transformers import AutoModelForCausalLM, AutoModelForImageTextToText, AutoProcessor, AutoTokenizer

    gemma = read_json(Path(config["model"]) / "config.json").get("model_type") == "gemma4"
    loader = AutoModelForImageTextToText if gemma else AutoModelForCausalLM
    model = loader.from_pretrained(config["model"], local_files_only=True, torch_dtype=torch.bfloat16, device_map={"": "cpu"})
    model = PeftModel.from_pretrained(model, adapter, local_files_only=True).merge_and_unload(safe_merge=True)
    model.save_pretrained(output, safe_serialization=True)
    (AutoProcessor if gemma else AutoTokenizer).from_pretrained(config["model"], local_files_only=True).save_pretrained(output)
    write_json(Path(output) / "merge.json", {"base": identity, "adapter_sha256": tree_hash(adapter)})
    return {"artifact": str(Path(output).resolve()), "artifact_sha256": tree_hash(output)}
