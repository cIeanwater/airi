"""Check cleaned chat datasets and run explicit local LoRA training."""

import argparse
import collections
import importlib.metadata
import json
import math
import platform
import re
import subprocess
import sys
import time
from pathlib import Path

from .common import digest, file_hash, read_json, read_jsonl, tree_hash, write_json, write_jsonl
from .dataset import compile_messages


DEFAULTS = {
    "method": "qlora", "precision": "bf16", "device": 0, "rank": 8,
    "max_length": 2048, "learning_rate": 0.00005, "epochs": 1,
    "max_steps": 0, "gradient_accumulation": 16, "seed": 42,
    "save_steps": 20, "minimum_groups": 10, "target_modules": ["q_proj", "v_proj"],
}


def configuration(value):
    if not isinstance(value, dict) or set(value) - set(DEFAULTS) - {"model", "tokenizer", "scope"}:
        raise ValueError("Unknown local training configuration field")
    result = {**DEFAULTS, **value}
    if not isinstance(result.get("model"), str) or not result["model"]:
        raise ValueError("Configure a local original Hugging Face model directory")
    result.setdefault("tokenizer", result["model"])
    if not isinstance(result["tokenizer"], str) or not result["tokenizer"]:
        raise ValueError("Configure a local tokenizer directory")
    if result["method"] not in {"lora", "qlora"} or result["precision"] not in {"bf16", "fp16"}:
        raise ValueError("Use lora or qlora with bf16 or fp16 precision")
    if result["device"] != 0 or type(result["device"]) is not int:
        raise ValueError("This single-GPU runner requires logical CUDA device 0")
    for name, low, high in (("rank", 1, 64), ("max_length", 128, 32768),
                            ("max_steps", 0, 10000), ("gradient_accumulation", 1, 256),
                            ("seed", 0, 2**31 - 1), ("save_steps", 1, 10000), ("minimum_groups", 3, 10000)):
        if type(result[name]) is not int or not low <= result[name] <= high:
            raise ValueError("Local training setting outside bounds: " + name)
    for name, low, high in (("learning_rate", 0.000001, 0.0003), ("epochs", 0.1, 3)):
        if type(result[name]) not in (float, int) or not math.isfinite(result[name]) or not low <= result[name] <= high:
            raise ValueError("Local training setting outside bounds: " + name)
    if (not isinstance(result["target_modules"], list) or not result["target_modules"]
            or any(not isinstance(target, str) for target in result["target_modules"])
            or len(set(result["target_modules"])) != len(result["target_modules"])
            or set(result["target_modules"]) - {"q_proj", "k_proj", "v_proj", "o_proj"}):
        raise ValueError("Use attention projection names for LoRA targets")
    if "scope" in result:
        _scope(result["scope"])
    return result


def _scope(value):
    if not isinstance(value, dict) or set(value) != {"user_id", "character_id"} or not all(isinstance(v, str) and v for v in value.values()):
        raise ValueError("Scope needs one user_id and character_id")
    return value


def row_hash(row):
    return digest({key: value for key, value in row.items() if key != "content_sha256"})


def validate_row(row):
    required = {"id", "group", "source_ids", "scope", "kind", "messages", "train_indices", "content_sha256"}
    if not isinstance(row, dict) or not required <= row.keys():
        raise ValueError("Cleaned row is missing training fields")
    for name in ("id", "group", "kind"):
        if not isinstance(row[name], str) or not row[name].strip():
            raise ValueError("Cleaned row needs a nonempty " + name)
    _scope(row["scope"])
    if row["content_sha256"] != row_hash(row):
        raise ValueError("Cleaned row hash changed")
    sources = row["source_ids"]
    if not isinstance(sources, list) or not sources or any(not isinstance(s, str) or not s for s in sources) or len(set(sources)) != len(sources):
        raise ValueError("Cleaned row requires unique source lineage")
    messages = row["messages"]
    if not isinstance(messages, list) or not messages:
        raise ValueError("Cleaned row requires messages")
    tools = row.get("tools", [])
    if not isinstance(tools, list):
        raise ValueError("Tool definitions must be an array")
    names = set()
    for tool in tools:
        function = tool.get("function", {}) if isinstance(tool, dict) else {}
        if not isinstance(function, dict):
            raise ValueError("Tool function definition must be an object")
        name = function.get("name")
        if not isinstance(tool, dict) or tool.get("type") != "function" or not isinstance(name, str) or not name or name in names:
            raise ValueError("Invalid or duplicate function definition")
        if not isinstance(function.get("parameters"), dict) or function["parameters"].get("type") != "object":
            raise ValueError("Tool parameters must describe an object")
        names.add(name)
    calls, receipts = {}, set()
    for index, message in enumerate(messages):
        if not isinstance(message, dict) or message.get("role") not in {"system", "user", "assistant", "tool"}:
            raise ValueError("Unsupported message role")
        content = message.get("content")
        if not isinstance(content, str) and not (content is None and message["role"] == "assistant" and message.get("tool_calls")):
            raise ValueError("Multimodal content requires a dedicated trainer")
        if message.get("reasoning_content") is not None and not isinstance(message["reasoning_content"], str):
            raise ValueError("Reasoning must be plain text")
        if message["role"] != "assistant" and message.get("tool_calls"):
            raise ValueError("Only assistant messages can request tools")
        if not isinstance(message.get("tool_calls", []), list):
            raise ValueError("Tool calls must be an array")
        for call in message.get("tool_calls", []):
            if not isinstance(call, dict) or not isinstance(call.get("function"), dict):
                raise ValueError("Tool call must contain a function object")
            function = call.get("function", {})
            if not isinstance(call.get("id"), str) or not call["id"] or call["id"] in calls or call.get("type") != "function" or function.get("name") not in names:
                raise ValueError("Tool call has an unknown definition or duplicate identity")
            arguments = function.get("arguments")
            if isinstance(arguments, str):
                arguments = json.loads(arguments)
            if not isinstance(arguments, dict):
                raise ValueError("Tool arguments must form an object")
            digest(arguments)
            calls[call["id"]] = index
        if message["role"] == "tool":
            call_id = message.get("tool_call_id")
            if not isinstance(call_id, str) or call_id not in calls or call_id in receipts:
                raise ValueError("Tool receipt is orphaned or repeated")
            if message.get("status", "finished") != "finished":
                raise ValueError("Tool result evidence is incomplete")
            receipts.add(call_id)
    indices = row["train_indices"]
    if not isinstance(indices, list) or not indices or any(type(index) is not int for index in indices) or len(set(indices)) != len(indices):
        raise ValueError("Train indices must select unique assistant decisions")
    for index in indices:
        if type(index) is not int or not 0 <= index < len(messages) or messages[index]["role"] != "assistant":
            raise ValueError("Loss target must be an assistant decision")
        target = messages[index]
        if target.get("status", "finished") != "finished":
            raise ValueError("An incomplete assistant response cannot be a target")
        if not (target.get("content") or target.get("reasoning_content") or target.get("tool_calls")):
            raise ValueError("The assistant target is empty")
        if any(call["id"] not in receipts for call in target.get("tool_calls", [])):
            raise ValueError("Selected tool decisions need complete result evidence")
    if not isinstance(row.get("template_kwargs", {}), dict):
        raise ValueError("Template settings must be an object")
    return row


def inspect_dataset(directory, minimum_groups=10):
    root = Path(directory)
    manifest = read_json(root / "manifest.json")
    splits, errors, groups, sources, prompts, scopes, ids = {}, [], {}, {}, {}, set(), set()
    counts = {}
    if not isinstance(manifest.get("files"), dict) or set(manifest["files"]) != {"train", "validation", "test"}:
        raise ValueError("Manifest must bind train, validation, and test JSONL files")
    for split in ("train", "validation", "test"):
        path = root / (split + ".jsonl")
        if not path.is_file() or file_hash(path) != manifest["files"][split]:
            raise ValueError("Dataset file is missing or changed: " + split)
        splits[split] = read_jsonl(path)
        counts[split] = len(splits[split])
        if not splits[split]:
            errors.append("empty_split:" + split)
        for row in splits[split]:
            validate_row(row)
            if row["id"] in ids:
                raise ValueError("Duplicate cleaned row ID")
            ids.add(row["id"])
            scopes.add(digest(row["scope"]))
            for category, keys in ((groups, [row["group"]]), (sources, row["source_ids"])):
                for key in keys:
                    if key in category and category[key] != split:
                        raise ValueError("Cross-split group or source leakage")
                    category[key] = split
            for index in row["train_indices"]:
                prompt = digest([row["messages"][:index], row.get("tools", []), row.get("template_kwargs", {})])
                if prompt in prompts and prompts[prompt] != split:
                    raise ValueError("Identical model context crosses dataset splits")
                prompts[prompt] = split
    if len(scopes) != 1:
        errors.append("multiple_or_missing_training_scopes")
    if len(groups) < minimum_groups:
        errors.append("insufficient_independent_groups")
    if "readiness" in manifest:
        readiness = manifest["readiness"]
        if not isinstance(readiness, dict) or type(readiness.get("ready")) is not bool:
            raise ValueError("Manifest readiness requires an explicit Boolean gate")
        if readiness["ready"] is not True:
            errors.append("source_cleaning_readiness_blocked")
    if manifest.get("records") is not None:
        records = manifest["records"]
        expected = {(row["id"], split, row["content_sha256"]) for split, values in splits.items() for row in values}
        if not isinstance(records, list) or any(not isinstance(row, dict) for row in records):
            raise ValueError("Manifest record bindings must be an array of objects")
        supplied = {(row.get("id"), row.get("split"), row.get("content_sha256")) for row in records}
        if supplied != expected or len(records) != len(expected):
            raise ValueError("Manifest record bindings do not match the cleaned dataset")
    if manifest.get("scope") is not None:
        scope = _scope(manifest["scope"])
        if scopes and digest(scope) not in scopes:
            raise ValueError("Manifest scope differs from its records")
    return splits, {"ready": not errors, "errors": errors, "rows": counts, "independent_groups": len(groups),
                    "manifest_sha256": digest(manifest), "files": manifest["files"],
                    "scope": next((row["scope"] for values in splits.values() for row in values), None),
                    "kinds": dict(collections.Counter(row["kind"] for values in splits.values() for row in values)),
                    "note": "Content and split checks do not establish factual or preference quality"}


def _versions():
    result = {}
    for package in ("torch", "transformers", "peft", "accelerate", "bitsandbytes"):
        try:
            result[package] = importlib.metadata.version(package)
        except importlib.metadata.PackageNotFoundError:
            result[package] = None
    return result


def _cuda_probe(device, precision):
    code = """import json, sys, torch
result = {"available": torch.cuda.is_available(), "device_count": torch.cuda.device_count()}
device, precision = int(sys.argv[1]), sys.argv[2]
if result["available"] and device < result["device_count"]:
    torch.cuda.set_device(device)
    result["bf16_supported"] = torch.cuda.is_bf16_supported()
    dtype = torch.bfloat16 if precision == "bf16" else torch.float16
    value = torch.ones((2, 2), dtype=dtype, device="cuda")
    result["kernel_ok"] = bool((value @ value).sum().item() == 8)
print(json.dumps(result))
"""
    try:
        completed = subprocess.run([sys.executable, "-c", code, str(device), precision],
                                   capture_output=True, text=True, check=True, timeout=45)
        return json.loads(completed.stdout.strip().splitlines()[-1])
    except (OSError, subprocess.SubprocessError, ValueError, IndexError):
        return {"available": False, "kernel_ok": False, "error": "cuda_probe_failed"}


def _weight_manifest(directory):
    root = Path(directory)
    if (root / "adapter_config.json").exists():
        raise ValueError("Adapter directories are not original model bases")
    indexes, weights = {}, set()
    for name, extension in (("model.safetensors.index.json", ".safetensors"), ("pytorch_model.bin.index.json", ".bin")):
        path = root / name
        if not path.is_file():
            continue
        index = read_json(path)
        mapping = index.get("weight_map") if isinstance(index, dict) else None
        if not isinstance(mapping, dict) or not mapping or any(not isinstance(key, str) or not key for key in mapping):
            raise ValueError("Weight index requires a nonempty tensor map")
        for filename in mapping.values():
            if (not isinstance(filename, str) or Path(filename).name != filename
                    or not filename.endswith(extension) or filename.startswith("adapter_")):
                raise ValueError("Weight index references an invalid shard name")
            weights.add(filename)
        indexes[name] = file_hash(path)
    if not indexes:
        weights.update(name for name in ("model.safetensors", "pytorch_model.bin") if (root / name).is_file())
    if not weights:
        raise ValueError("Original Hugging Face monolithic weights or a complete shard index are required")
    for name in sorted(weights):
        path = root / name
        if not path.is_file() or path.stat().st_size == 0:
            raise ValueError("Original Hugging Face weight shard is missing or empty: " + name)
    return {"weights": sorted(weights), "indexes": indexes}


def _base_identity(directory):
    root = Path(directory).resolve(strict=True)
    manifest = _weight_manifest(root)
    return {"path": str(root), "config_sha256": digest(read_json(root / "config.json")),
            "weights": [{"name": name, "sha256": file_hash(root / name)} for name in manifest["weights"]],
            "weight_indexes": manifest["indexes"]}


def environment_check(config):
    model, tokenizer = Path(config["model"]), Path(config["tokenizer"])
    blockers, warnings = [], []
    if not (model / "config.json").is_file():
        blockers.append("original_model_config_missing")
    try:
        weight_manifest = _weight_manifest(model)
        weights_error = None
    except (ValueError, OSError) as error:
        weight_manifest = None
        weights_error = str(error)
        blockers.append("original_hf_weights_missing")
    tokenizer_ready = (tokenizer / "tokenizer_config.json").is_file() and ((tokenizer / "tokenizer.json").is_file() or (tokenizer / "tokenizer.model").is_file())
    if not tokenizer_ready:
        blockers.append("local_tokenizer_missing")
    versions = _versions()
    for package in ("torch", "transformers", "peft", "accelerate"):
        if not versions[package]:
            blockers.append("dependency_missing:" + package)
    if config["method"] == "qlora" and not versions["bitsandbytes"]:
        blockers.append("dependency_missing:bitsandbytes")
    for package, minimum in (("transformers", (5, 10, 1)), ("peft", (0, 19, 0))):
        if versions[package]:
            pieces = re.match(r"(\d+)\.(\d+)(?:\.(\d+))?", versions[package])
            parsed = tuple(int(p or 0) for p in pieces.groups()) if pieces else (0, 0, 0)
            if parsed < minimum:
                blockers.append("dependency_too_old:" + package)
    gpu = "unavailable"
    try:
        gpu = subprocess.run(["nvidia-smi", "--query-gpu=index,name,memory.total,memory.free", "--format=csv"],
                             check=True, capture_output=True, text=True, timeout=10).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        blockers.append("nvidia_smi_unavailable")
    cuda = _cuda_probe(config["device"], config["precision"]) if versions["torch"] else {"available": False, "kernel_ok": False}
    if not cuda.get("available") or config["device"] >= cuda.get("device_count", 0):
        blockers.append("selected_cuda_device_unavailable")
    if cuda.get("available") and cuda.get("device_count") != 1:
        blockers.append("select_one_visible_cuda_device")
    if not cuda.get("kernel_ok"):
        blockers.append("cuda_kernel_unverified")
    if config["precision"] == "bf16" and cuda.get("available") and not cuda.get("bf16_supported"):
        blockers.append("selected_device_bf16_unsupported")
    if config["max_length"] > 4096:
        warnings.append("long_context_requires_peak_memory_measurement")
    return {"ready": not blockers, "blockers": blockers, "warnings": warnings, "packages": versions,
            "python": platform.python_version(), "gpu": gpu, "tokenizer_files_present": tokenizer_ready,
            "cuda": cuda, "cuda_kernel_tested": bool(cuda.get("kernel_ok")), "model_loaded": False,
            "weight_manifest": weight_manifest, "weight_error": weights_error}


def check(dataset, config):
    config = configuration(config)
    _, data = inspect_dataset(dataset, config["minimum_groups"])
    environment = environment_check(config)
    scope_match = config.get("scope") is None or data["scope"] == config["scope"]
    return {"dataset": data, "environment": environment, "scope_matches": scope_match,
            "ready_for_tokenizer_check": bool(environment["tokenizer_files_present"] and environment["packages"]["transformers"]),
            "ready_for_training": bool(data["ready"] and environment["ready"] and scope_match),
            "training_executed": False}


def tokenize(dataset, config, tokenizer=None):
    config = configuration(config)
    splits, data = inspect_dataset(dataset, config["minimum_groups"])
    if tokenizer is None:
        from transformers import AutoTokenizer

        tokenizer = AutoTokenizer.from_pretrained(config["tokenizer"], local_files_only=True, trust_remote_code=False)
    if not tokenizer.chat_template or tokenizer.eos_token_id is None:
        raise ValueError("Tokenizer needs a native chat template and EOS token")
    encoded, lengths, errors = {}, [], []
    for split, rows in splits.items():
        encoded[split] = []
        for row in rows:
            for index in row["train_indices"]:
                detail = {"id": row["id"], "split": split, "target_index": index}
                try:
                    sample = compile_messages(row["messages"], tokenizer, 1000000, tools=row.get("tools", []),
                                              train_indices=[index], template_kwargs=row.get("template_kwargs", {}))
                    detail.update(tokens=len(sample["input_ids"]), target_tokens=sum(t != -100 for t in sample["labels"]))
                    if detail["tokens"] > config["max_length"]:
                        raise ValueError("sequence_exceeds_configured_context")
                    encoded[split].append(sample)
                    detail["status"] = "accepted"
                except (ValueError, TypeError, KeyError) as error:
                    detail.update(status="rejected", reason=str(error))
                    errors.append(detail)
                lengths.append(detail)
    token_files = {name: file_hash(Path(config["tokenizer"]) / name)
                   for name in ("tokenizer.json", "tokenizer_config.json", "tokenizer.model", "chat_template.jinja", "special_tokens_map.json")
                   if (Path(config["tokenizer"]) / name).is_file()}
    token_identity = {"files": token_files, "chat_template_sha256": digest(str(tokenizer.chat_template))}
    return encoded, {"dataset": data, "tokenizer_sha256": digest(token_identity), "tokenizer": token_identity, "max_length": config["max_length"],
                     "sequences": {key: len(values) for key, values in encoded.items()}, "rows": lengths, "errors": errors,
                     "target_tokens": {key: sum(sum(t != -100 for t in row["labels"]) for row in values) for key, values in encoded.items()},
                     "ready_for_training": data["ready"] and not errors and all(encoded.values()), "training_executed": False}


def train(dataset, config, output, *, adapter=None, resume_checkpoint=None):
    config = configuration(config)
    checked = check(dataset, config)
    if not checked["ready_for_training"]:
        raise ValueError("Preflight is blocked. Run --check and resolve its reported blockers")
    encoded, token_report = tokenize(dataset, config)
    if not token_report["ready_for_training"]:
        raise ValueError("Tokenization is blocked. Resolve overlength or template errors without truncation")
    out = Path(output)
    if out.exists() and not resume_checkpoint:
        raise ValueError("Use a new training output directory")
    import torch
    from peft import LoraConfig, PeftModel, get_peft_model, prepare_model_for_kbit_training
    from transformers import AutoModelForCausalLM, AutoModelForImageTextToText, AutoTokenizer, BitsAndBytesConfig, Trainer, set_seed

    if not torch.cuda.is_available() or torch.cuda.device_count() != 1:
        raise ValueError("Selected CUDA training device is unavailable")
    torch.cuda.set_device(config["device"])
    if config["precision"] == "bf16" and not torch.cuda.is_bf16_supported():
        raise ValueError("Selected CUDA device does not support BF16")
    identity = _base_identity(config["model"])
    scope = config.get("scope") or checked["dataset"]["scope"]
    parent = tree_hash(adapter) if adapter else None
    if resume_checkpoint:
        parent = read_json(out / "finetune-lineage.json")["parent_sha256"]
    lineage = {"schema_version": 1, "base": identity, "scope": scope, "dataset_sha256": checked["dataset"]["manifest_sha256"],
               "config_sha256": digest(config), "tokenizer_sha256": token_report["tokenizer_sha256"],
               "tokenizer_files": token_report["tokenizer"]["files"],
               "rank": config["rank"], "method": config["method"], "parent_sha256": parent}
    if adapter:
        previous = read_json(Path(adapter) / "finetune-lineage.json")
        if previous["base"] != identity or previous["scope"] != scope or previous["rank"] != config["rank"] or previous["tokenizer_sha256"] != token_report["tokenizer_sha256"]:
            raise ValueError("Parent adapter has a different base, scope, rank, or tokenizer")
    if resume_checkpoint:
        checkpoint = Path(resume_checkpoint).resolve(strict=True)
        if adapter or not checkpoint.is_relative_to(out.resolve()) or not (checkpoint / "trainer_state.json").is_file():
            raise ValueError("Resume a Trainer checkpoint inside the same run without another adapter")
        if read_json(out / "finetune-lineage.json") != lineage:
            raise ValueError("Resume configuration, dataset, or base changed")
    dtype = torch.bfloat16 if config["precision"] == "bf16" else torch.float16
    gemma = read_json(Path(config["model"]) / "config.json").get("model_type") == "gemma4"
    quantization = BitsAndBytesConfig(load_in_4bit=True, bnb_4bit_quant_type="nf4", bnb_4bit_use_double_quant=True,
                                     bnb_4bit_compute_dtype=dtype) if config["method"] == "qlora" else None
    loader = AutoModelForImageTextToText if gemma else AutoModelForCausalLM
    set_seed(config["seed"])
    model = loader.from_pretrained(config["model"], local_files_only=True, trust_remote_code=False,
                                   torch_dtype=dtype, quantization_config=quantization,
                                   device_map={"": config["device"]}, attn_implementation="sdpa")
    model.config.use_cache = False
    if config["method"] == "qlora":
        model = prepare_model_for_kbit_training(model, use_gradient_checkpointing=True)
    if adapter:
        model = PeftModel.from_pretrained(model, adapter, is_trainable=True, local_files_only=True)
    else:
        targets = config["target_modules"]
        if gemma:
            targets = r".*language_model\.layers\.\d+\.self_attn\.(" + "|".join(targets) + ")"
        model = get_peft_model(model, LoraConfig(r=config["rank"], lora_alpha=config["rank"] * 2,
                                               lora_dropout=0.05, bias="none", target_modules=targets, task_type="CAUSAL_LM"))
    unexpected = [name for name, value in model.named_parameters() if value.requires_grad and "lora_" not in name]
    if unexpected:
        raise ValueError("Non-LoRA parameters became trainable")
    if not any(value.requires_grad for value in model.parameters()):
        raise ValueError("No trainable LoRA parameters were attached")
    model.enable_input_require_grads()
    tokenizer = AutoTokenizer.from_pretrained(config["tokenizer"], local_files_only=True, trust_remote_code=False)
    if tokenizer.pad_token_id is None:
        tokenizer.pad_token = tokenizer.eos_token

    def collate(batch):
        length = max(len(row["input_ids"]) for row in batch)
        return {key: torch.tensor([row[key] + [pad] * (length - len(row[key])) for row in batch])
                for key, pad in (("input_ids", tokenizer.pad_token_id), ("attention_mask", 0), ("labels", -100))}

    args = training_arguments(config, out, len(encoded["train"]))
    out.mkdir(parents=True, exist_ok=True)
    write_json(out / "finetune-lineage.json", lineage)
    write_json(out / "tokenization-report.json", token_report)
    trainer = Trainer(model=model, args=args, train_dataset=encoded["train"], eval_dataset=encoded["validation"], data_collator=collate)
    started = time.monotonic()
    trainer.train(resume_from_checkpoint=str(resume_checkpoint) if resume_checkpoint else None)
    model.save_pretrained(out / "adapter", safe_serialization=True)
    tokenizer.save_pretrained(out / "adapter")
    write_json(out / "adapter/finetune-lineage.json", lineage)
    result = {"training_executed": True, "adapter": str((out / "adapter").resolve()), "adapter_sha256": tree_hash(out / "adapter"),
              "lineage": lineage, "seconds": time.monotonic() - started, "validation": trainer.evaluate(),
              "held_out_test": trainer.evaluate(eval_dataset=encoded["test"], metric_key_prefix="test"), "packages": _versions()}
    write_json(out / "training-report.json", result)
    return result


def training_arguments(config, output, train_sequences):
    """Build the installed Transformers arguments without loading model weights."""
    from transformers import TrainingArguments

    steps = config["max_steps"]
    total_steps = steps or math.ceil(train_sequences / config["gradient_accumulation"]) * config["epochs"]
    return TrainingArguments(output_dir=str(output), num_train_epochs=config["epochs"], max_steps=steps or -1,
                             learning_rate=config["learning_rate"], per_device_train_batch_size=1, per_device_eval_batch_size=1,
                             gradient_accumulation_steps=config["gradient_accumulation"], optim="adamw_torch",
                             gradient_checkpointing=True, gradient_checkpointing_kwargs={"use_reentrant": False},
                             bf16=config["precision"] == "bf16", fp16=config["precision"] == "fp16",
                             eval_strategy="steps" if steps else "epoch", save_strategy="steps" if steps else "epoch",
                             eval_steps=config["save_steps"], save_steps=config["save_steps"], save_total_limit=2,
                             logging_steps=5, report_to=[], dataloader_num_workers=0, remove_unused_columns=False,
                             prediction_loss_only=True, warmup_steps=math.floor(total_steps * 0.05), weight_decay=0.01, seed=config["seed"])


def merge(config, adapter, output):
    """Merge one bound adapter into its original full-precision CPU base."""
    config = configuration(config)
    out, source = Path(output), Path(adapter)
    if out.exists():
        raise ValueError("Use a new merge output directory")
    previous = read_json(source / "finetune-lineage.json")
    identity = _base_identity(config["model"])
    if previous.get("base") != identity:
        raise ValueError("Merge base differs from the adapter training base")
    scope = _scope(previous.get("scope"))
    if config.get("scope") is not None and config["scope"] != scope:
        raise ValueError("Merge scope differs from the adapter training scope")
    token_files = {name: file_hash(Path(config["tokenizer"]) / name)
                   for name in ("tokenizer.json", "tokenizer_config.json", "tokenizer.model", "chat_template.jinja", "special_tokens_map.json")
                   if (Path(config["tokenizer"]) / name).is_file()}
    if not token_files or previous.get("tokenizer_files") != token_files:
        raise ValueError("Merge tokenizer differs from the adapter training tokenizer")
    adapter_hash = tree_hash(source)
    import torch
    from peft import PeftModel
    from transformers import AutoModelForCausalLM, AutoModelForImageTextToText, AutoProcessor, AutoTokenizer

    gemma = read_json(Path(config["model"]) / "config.json").get("model_type") == "gemma4"
    loader = AutoModelForImageTextToText if gemma else AutoModelForCausalLM
    model = loader.from_pretrained(config["model"], local_files_only=True, trust_remote_code=False,
                                   torch_dtype=torch.float32, device_map={"": "cpu"}, low_cpu_mem_usage=True)
    model = PeftModel.from_pretrained(model, source, local_files_only=True, is_trainable=False)
    merged = model.merge_and_unload(safe_merge=True)
    merged.save_pretrained(out, safe_serialization=True)
    tokenizer = AutoTokenizer.from_pretrained(config["tokenizer"], local_files_only=True, trust_remote_code=False)
    tokenizer.save_pretrained(out)
    if gemma:
        processor = AutoProcessor.from_pretrained(config["model"], local_files_only=True, trust_remote_code=False)
        processor.save_pretrained(out)
        tokenizer.save_pretrained(out)
    lineage = {"schema_version": 1, "kind": "merged-candidate", "base": identity, "scope": scope,
               "adapter_sha256": adapter_hash, "adapter_lineage_sha256": file_hash(source / "finetune-lineage.json"),
               "tokenizer_sha256": previous["tokenizer_sha256"], "quantized_merge": False, "deployed": False}
    write_json(out / "merge-lineage.json", lineage)
    return {"merge_executed": True, "model": str(out.resolve()), "model_sha256": tree_hash(out),
            "lineage": lineage, "deployed": False}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset")
    parser.add_argument("--config", required=True)
    action = parser.add_mutually_exclusive_group()
    action.add_argument("--check", action="store_true")
    action.add_argument("--tokenize-only", action="store_true")
    action.add_argument("--train", action="store_true")
    action.add_argument("--merge-only", action="store_true")
    parser.add_argument("--output")
    parser.add_argument("--report")
    parser.add_argument("--adapter")
    parser.add_argument("--resume-from-checkpoint")
    parser.add_argument("--method", choices=["lora", "qlora"])
    args = parser.parse_args(argv)
    config = read_json(args.config)
    if args.method:
        config["method"] = args.method
    if args.merge_only:
        if not args.adapter or not args.output:
            parser.error("Merging requires --adapter and a new --output directory")
        if args.dataset or args.resume_from_checkpoint:
            parser.error("Merging accepts no dataset or Trainer checkpoint")
        report = merge(config, args.adapter, args.output)
    elif not args.dataset:
        parser.error("Checking, tokenization, and training require --dataset")
    elif args.train:
        if not args.output:
            parser.error("Training requires a new --output directory")
        report = train(args.dataset, config, args.output, adapter=args.adapter, resume_checkpoint=args.resume_from_checkpoint)
    elif args.tokenize_only:
        encoded, report = tokenize(args.dataset, config)
        if args.output:
            output = Path(args.output)
            if output.exists():
                parser.error("Tokenization output must be a new directory")
            for split, values in encoded.items():
                write_jsonl(output / (split + ".jsonl"), values)
            write_json(output / "tokenization-report.json", report)
    else:
        report = check(args.dataset, config)
    if args.report:
        write_json(args.report, report)
    print(json.dumps(report, ensure_ascii=False, allow_nan=False))


if __name__ == "__main__":
    main()
