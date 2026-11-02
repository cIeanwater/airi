# Local self-evolution

This optional Python service audits AIRI archives and builds reviewed SFT datasets.
It supports bounded LoRA experiments, paired evaluation, adapter merge, and a reversible champion registry.
It uses files as its integration boundary. It does not change the desktop runtime.

## Requirements

Audit, dataset assembly, proposals, and evaluation use Python 3.10 or later and the standard library.
Training uses Transformers, PEFT, Accelerate, and a CUDA PyTorch build.
QLoRA also uses bitsandbytes.
Install these dependencies in a separate environment with `requirements.txt`.
Record the resolved versions before a real experiment.

The default target is **Gemma 4 E2B instruction-tuned**.
The existing QAT Q4_0 GGUF serves inference. Transformers training needs the original Hugging Face checkpoint.
Prepare `google/gemma-4-E2B-it` under `local-models/gemma4-e2b-it-hf` with a pinned Hub revision.
No command downloads weights automatically.

Commands below run from the repository root.
Store private inputs and outputs under `services/self-evolution/private/` or `services/self-evolution/runs/`.
Both directories are ignored by Git.

## Audit and candidate review

```powershell
python services/self-evolution/pipeline.py audit --archive PATH_TO_SHARED_ARCHIVE --requests PATH_TO_REQUEST_LOGS --output services/self-evolution/private/audit.json
python services/self-evolution/pipeline.py export --archive PATH_TO_SHARED_ARCHIVE --requests PATH_TO_REQUEST_LOGS --output services/self-evolution/private/candidates.jsonl
python services/self-evolution/pipeline.py review-template --candidates services/self-evolution/private/candidates.jsonl --output services/self-evolution/private/reviews.jsonl
```

Repeat `--archive` or `--requests` for several sources.
Audit writes metadata only. Export writes private candidate text.
The importer supports companion model logs and gateway `model_input` records.
It retains effective input, response attribution, and explicit reasoning as separate fields.
It rejects failed responses, incomplete responses, images, and incomplete tool protocols.
It preserves function definitions in `tools` and passes them to the native model template.
It requires explicit review of tool-conditioned examples.
Tool execution trajectories, forced tool choices, and structured decoding constraints need a dedicated training path.

Event copies deduplicate by scope and ID. Conflicting originals stop the audit.
Event-only histories and cognition documents require authored prompt-answer pairs.
The importer never invents a prompt from adjacent timestamps.

For authored base replay, persona demonstrations, or repaired examples, use `pipeline.py curate --input INPUT.jsonl --output OUTPUT.jsonl`.
Each input record needs `messages`, `kind`, `group`, and `source_ids`.
Optional fields are `occurred_at`, `reasoning_source`, `short_reasoning`, `tools`, and `issues`.
Kinds are `base`, `dialogue`, `persona`, and `reasoning`.
Messages contain `role` and plain-text `content`. Roles alternate after an optional system message.

Candidate records contain a version, ID, content hash, messages, tool definitions, source IDs, session group, timestamp, and reasoning fields.
All candidates start unapproved.
Review records bind an approval to the candidate content hash.
Required review fields are `reviewer`, `approved`, `privacy_checked`, and `evidence_checked`.
Resolve every candidate issue in `resolved_issues` before approval.
Reasoning also requires `faithfulness_checked` and `answer_checked`.
These attestations come from an actual reviewer. A schema check does not establish factual quality.
Secret scanning and PII patterns supplement that review. They do not replace it.

```powershell
python services/self-evolution/pipeline.py build --candidates CANDIDATES.jsonl --reviews REVIEWS.jsonl --mix services/self-evolution/mix.example.json --character-budget 100000 --output services/self-evolution/runs/dataset-001
```

Merge authored records and exported records into the candidate JSONL before this command.
The builder connects sessions, source lineage, and normalized duplicate prompts before temporal splitting.
It requires ten independent groups for train, validation, and test partitions.
Near duplicates and shared facts still need review.
Mix quotas use characters as a planning proxy. Training reports actual tokenizer counts.
Each nonzero category must fill at least 80% of its quota without repeated records.
Base replay remains at least 30%. The example mix is a starting hypothesis, not a measured optimum.

## Optional Chinese reasoning proposals

```powershell
python services/self-evolution/teacher.py --candidates CANDIDATES.jsonl --output services/self-evolution/private/summaries.jsonl
```

The default command reports a plan without a network call.
To send selected reasoning to DeepSeek, set `DEEPSEEK_API_KEY` and add `--execute`.
The key stays in the process environment.
The default batch contains at most ten records and 50,000 source characters.
Each output remains unapproved and requires independent review.
The teacher preserves final answers. It proposes a short summary of existing reasoning only.
The API model name is configurable. Check the provider model catalog before execution.

## Training and merge

```powershell
python services/self-evolution/training.py preflight
python services/self-evolution/training.py train --config services/self-evolution/config.example.json --dataset DATASET_DIR --output services/self-evolution/runs/train-001
```

The training command checks the plan by default. Add `--execute` to start training.
The example uses rank 8, context 512, batch 1, accumulation 16, and one epoch.
It uses one selected GPU. Two 12GB cards do not form one 24GB device.
Gemma adapters target language attention projections. Vision and audio weights remain frozen.
Prompt and padding labels are masked. Native turn endings come from the model template.
Overlength examples are rejected. Excessive rejection stops training instead of truncating answers.

`gemma4-native` supervises short reasoning through the Gemma 4 thought channel and also supervises the final answer.
`summary` trains a separate summarization task. It does not establish shorter native reasoning.
`none` trains answers without reasoning targets.
Chinese character reduction does not guarantee tokenizer savings or reasoning accuracy.

```powershell
python services/self-evolution/training.py merge --base HF_BASE_DIR --adapter RUN_DIR/adapter --output MERGED_DIR
```

Add `--execute` to merge into a new full-precision checkpoint on CPU.
The merge command checks the adapter base path and uses `safe_merge=True`.
Keep the original base, adapter, dataset manifest, and evaluation report.
Requantization and GGUF deployment require separate validation with the serving stack.
The service does not replace the running model or its vision projector.

## Model proposals and bounded iteration

```powershell
python services/self-evolution/proposal.py --config services/self-evolution/config.example.json --inventory INVENTORY.json --metrics DEV_METRICS.json --output PROPOSAL.json
python services/self-evolution/evolution.py cycle --proposal PROPOSAL.json --candidates CANDIDATES.jsonl --reviews REVIEWS.jsonl --suite SUITE.jsonl --output services/self-evolution/runs/cycle-001 --registry services/self-evolution/runs/registry.json
```

Both commands default to a plan. Add `--execute` for local generation or a training cycle.
Inventory contains aggregate category counts and budgets. Metrics contain aggregate development results.
The proposal model sees no held-out prompts or answers.
It can select bounded rank, context, learning rate, epochs, and data proportions.
The controller rejects changes to fixed model, device, and reasoning settings.
This is feedback-based proposal selection. It does not implement gradient influence selection or SEAL outer-loop RL.

Each cycle rebuilds reviewed data, trains from the same original base, and evaluates champion and candidate sequentially.
Each model runs in a separate process to release GPU memory.
Repeated rounds use replay. They do not repeatedly add the same adapter delta.
The registry records successful candidates without changing the serving process.

Suite JSONL records contain `id`, `kind`, `group`, `source_ids`, `messages`, and `expect`.
Suite kinds are `base`, `persona`, `reasoning`, `memory`, and `tools`.
Positive assertions use `expect.all`, `expect.any`, or `expect.exact`.
Optional assertions use `none`, `max_chars`, `chinese`, and `reasoning_max_chars`.
Native reasoning cases also set `enable_thinking: true`.
Substring assertions test contracts. They do not establish personality quality or tool execution correctness.
Use actual independent style and faithfulness judgments for persona and reasoning cases.

Judgment JSONL records contain `id`, `prediction_sha256`, `reviewer`, `independent: true`, and a score from zero to one.
Compute the hash with `evaluation.prediction_hash` over the final answer and reasoning summary.
An independent reviewer can be a person or a separately calibrated model.
The service checks the attestation and content hash. It cannot prove reviewer independence.

Promotion requires 100 cases, 30 independent groups, 20 cases per slice, and complete independent judgments.
It also requires positive paired improvement and no slice regression above two percentage points.
The bootstrap resamples whole groups. These are conservative engineering defaults, not a guarantee of general improvement.
Missing evidence leaves the champion unchanged.
After review, rerun `evolution.py evaluate --help` and use its content-bound inputs to update the cycle report.
Then run `evolution.py promote --cycle CYCLE_DIR --registry REGISTRY.json`.
Run `evolution.py rollback --registry REGISTRY.json` to restore the prior registry entry.

Use development suites for candidate search. Reserve a fresh final test for periodic release decisions.
Model proposals alone do not establish recursive self-improvement.

## Checks

```powershell
python services/self-evolution/test-pipeline.py
python -m compileall -q services/self-evolution
```

Tests use synthetic text and files. They perform no model calls or optimizer steps.
Real training needs the original E2B checkpoint, compatible CUDA dependencies, reviewed data, and a held-out suite.
