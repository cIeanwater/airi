# AIRI RSI

This optional package implements the ordered RSI workflow around the local Gemma 4 E2B model.
It owns experience records, verification, candidate experiments, strategy learning records, and persistent goals.
The Python standard library runs capture, evaluation, replay, scheduling, and the synthetic demonstration.
Transformers and PEFT are optional training dependencies.

## Clean captured data and prepare local LoRA

`curate.py` snapshots capture files and prepares private semantic review packets.
`cleaning.py` checks source integrity, tool traces, output status, and scoped identity rules.
The prompts in `prompts/` select examples, compress explicit thinking into Chinese, and review the result.
These commands do not call a remote model or send private data to another provider.

Create three local configuration files under `rsi/private/`:

- `collections.json`: paths named `events`, `companion`, and `gateway` for the capture collections.
- `scope.json`: one `user_id` and `character_id` for this corpus.
- `policy.json`: the scoped `character_name` and `person_names` for identity checks.

Use a new directory for every snapshot, extraction, packet set, and final dataset:

```powershell
python -m rsi.curate snapshot --collections rsi/private/collections.json --output rsi/private/corpus-001/snapshot
python -m rsi.curate extract --snapshot rsi/private/corpus-001/snapshot --scope rsi/private/scope.json --policy rsi/private/policy.json --output rsi/private/corpus-001/extracted
python -m rsi.curate batches --candidates rsi/private/corpus-001/extracted/candidates.jsonl --output rsi/private/corpus-001/packets
```

An event collection uses `*/archive/events/*.json`. Request collections use their top-level JSON files.
Snapshots preserve source bytes and record file hashes.
The extractor keeps the actual model input, output, tools, and causal order.
It does not manufacture dialogue from event prose or insert invented user messages.
Application state can occupy a recorded `user` role. That role does not establish human authorship.

Review each packet with `clean-corpus.txt` and `compress-thinking.txt`.
For truncated packet views, read the full candidate before acceptance.
Use `review-cleaned.txt` for a separate review of selected results.
Store each reviewer response as JSONL under the private run directory.
Each response binds its decision to `candidate_id` and `candidate_sha256`.
Acceptance requires the explicit checks and resolved issues listed in the cleaning prompt.
Reviewers can reject or quarantine an example without replacing its answer.
The collector rejects missing, repeated, stale, or incomplete acceptance decisions.

```powershell
python -m rsi.curate collect-reviews --candidates rsi/private/corpus-001/extracted/candidates.jsonl --reviews rsi/private/corpus-001/packets/suppressed-reviews.jsonl --reviews rsi/private/corpus-001/reviews.jsonl --output rsi/private/corpus-001/all-reviews.jsonl
python -m rsi.curate finalize --candidates rsi/private/corpus-001/extracted/candidates.jsonl --reviews rsi/private/corpus-001/all-reviews.jsonl --output rsi/private/corpus-001/dataset
```

Redundant targets enter quarantine. This decision does not assert semantic failure.
Finalization changes only the selected assistant reasoning channel to its reviewed Chinese summary.
It preserves final answers, structured output fields, tool arguments, and historical thinking in the causal prefix.
The loss compiler masks that prefix. It trains only selected assistant targets.
Invalid reasoning enters quarantine instead of becoming a fluent corrected claim.

Outputs include SFT JSONL, raw-to-short reasoning pairs, review decisions, hashes, and an inspection report.
Group connections use sessions, source lineage, and identical prompts before temporal splitting.
Unlinked requests share a conservative group. They do not become independent examples through random splitting.
Fewer than ten independent groups produce a train-only export with a blocked training-readiness flag.
The corpus export does not automatically supply base replay or establish a successful RSI experiment.

`finetune.py` runs the local LoRA workflow for this cleaned format.
Prepare an isolated Python environment with a compatible CUDA PyTorch build and `requirements.txt`.
Copy `train-config.example.json` into `rsi/private/` and set local original Hugging Face model and tokenizer paths.
An inference GGUF file cannot replace those training weights.
Both ordinary LoRA and four-bit QLoRA use PEFT adapters. The example selects QLoRA for lower memory use.

```powershell
./rsi/train-local.ps1 -Dataset rsi/private/corpus-001/dataset -Config rsi/private/train.json -Action Check
./rsi/train-local.ps1 -Dataset rsi/private/corpus-001/dataset -Config rsi/private/train.json -Action Tokenize -Report rsi/private/tokenization.json
./rsi/train-local.ps1 -Dataset rsi/private/corpus-001/dataset -Config rsi/private/train.json -Action Train -Method qlora -Output rsi/runs/lora-001
./rsi/train-local.ps1 -Config rsi/private/train.json -Action Merge -Adapter rsi/runs/lora-001/adapter -Output rsi/runs/merged-001
```

Use `-Method lora` for an unquantized base. Set `-Python` to select another isolated environment.
The default action checks readiness. Training requires the explicit `Train` action.
Tokenization uses the native chat template without weights or an optimizer.
It reports full sequence and target lengths, including overlength failures. It does not truncate examples.
To quarantine overlength rows, apply the tokenizer report and finalize a new dataset:

```powershell
python -m rsi.curate budget-reviews --dataset rsi/private/corpus-001/dataset --reviews rsi/private/corpus-001/all-reviews.jsonl --token-report rsi/private/tokenization.json --output rsi/private/corpus-001/budget-reviews.jsonl
python -m rsi.curate finalize --candidates rsi/private/corpus-001/extracted/candidates.jsonl --reviews rsi/private/corpus-001/budget-reviews.jsonl --output rsi/private/corpus-001/dataset-budget
```

Use that new dataset for another tokenizer check and training.
Training checks dataset hashes, scopes, independent splits, dependencies, and a CUDA kernel before model load.
It records adapter lineage, optimizer checkpoints, validation loss, and held-out test loss.
Merge checks the original base, scope, and tokenizer. It loads a full-precision CPU base and saves a separate candidate.
It does not merge into quantized inference weights or replace the active server.
Loss changes alone do not establish persona quality, tool reliability, or promotion eligibility.
The `cycle` workflow below uses normalized episodes and independent evidence for automatic release decisions.
The local runner uses one visible GPU at logical device zero. Select another physical GPU with `CUDA_VISIBLE_DEVICES`.
Capture earlier tool decisions as normalized episodes with per-decision evidence. Corpus extraction selects each request's final assistant target.

## Start with the synthetic walkthrough

Run these commands from the repository root:

```powershell
python -m rsi --help
python -m rsi demo --output rsi/runs/demo-001
python -m unittest discover -s rsi/tests -p 'test_*.py' -q
python -m rsi preflight
```

The demonstration captures 80 synthetic episodes, assembles verified data, compares synthetic predictions, and completes a file-backed goal.
It performs no model calls or training. Its evaluation cannot promote a production model.
Use a new output directory for each demonstration.
Private records, artifacts, and model outputs belong under `rsi/private/` or `rsi/runs/`. Git ignores both directories.

## 1. Capture complete episodes

`episodes.py` defines the versioned capture contract and immutable store.
It supports dialogue, reflection, initiative, consecutive assistant phases, and multi-step function calls.
Each message records its role, origin, status, and stable ID.
An origin identifies human, application, model, or tool authorship independently of the message role.
Scope uses `user_id` and `character_id`.
Watcher and experiment configurations pin one `actor_scope`. Personalized episodes from other identities cannot enter that actor's update.

Episodes also contain source IDs, session, optional person and goal IDs, model identity, tools, template settings, and state references.
The content hash binds every original field. Training indices and verification hashes are separate derived fields.
State references do not replace model input. Capture the actual visible state inside the recorded request.

```powershell
python -m rsi audit --records REQUEST_DIRECTORY --scope SCOPE.json
python -m rsi import --records REQUEST_DIRECTORY --scope SCOPE.json --kind reflection --root rsi/private --execute
python -m rsi capture --episode EPISODE.json --root rsi/private --execute
```

Audit reports metadata. Import supports companion request logs and gateway `model_input` records.
Unknown identity, session, provenance, and decoding constraints remain explicit capture issues.
No correlation is invented from timestamp proximity or matching prose.
The importer rejects unsupported multimodal content. Failed and incomplete outputs remain unsuitable success targets.

Applications can submit normalized episodes to `EpisodeStore.append`, or write complete JSON files to the watch inbox.
Publish inbox files with atomic rename after capture finishes.
This package does not change the current Electron stores or their active data directories.
Request import provides the current file-based integration. A live application can add explicit episode and goal correlation next.

## 2. Verify, split, and diagnose

`dataset.verify_episode` checks external expectations against an exact episode hash.
An evidence record names the reviewer, the episode hash, and checks for specific assistant message IDs.
Supported checks include exact text, required text, parsed JSON, and tool results.
Tool-result checks bind call ID, function name, arguments, completion status, and actual result hash.
Reasoning targets also need an independently checked reasoning summary.
A model-authored success flag cannot replace these checks.

```powershell
python -m rsi dataset --root rsi/private --evidence VERIFICATIONS.json --mix MIX.json --character-budget 12000 --output rsi/runs/dataset-001 --execute
python -m rsi diagnose --gaps GAPS.json --usable-samples 100 --resources-ready --cooldown-ready --budget-ready
```

The verification file maps episode IDs to independent evidence records.
An unresolved capture issue blocks that episode.
The verifier is a trusted host boundary. Hashes cannot prove that a reviewer or fixture is truthful.
Use isolated reference fixtures or calibrated reviewers, and keep them outside model-write access.

Dataset assembly connects source lineage, sessions, goals, and duplicate prompts before temporal splitting.
It needs at least ten independent groups and nonempty train, validation, and test files.
Mix categories are `base`, `dialogue`, `reflection`, and `initiative`.
Tag base replay with `episode.context.dataset_kind = "base"`.
The initial mix is 40%, 30%, 20%, and 10%. Base replay remains at least 30%.
Character quotas provide a planning proxy. Training records the actual target token count.
Missing unique data stops assembly rather than repeating examples to fill a quota.

The loss compiler makes one causal sample per selected assistant decision.
It masks system, user, tool, and prefix tokens. It retains native turn endings and tool definitions.
Assistant-only episodes do not acquire fabricated human messages.
The compiler rejects overlength sequences and inconsistent native template boundaries.

Diagnosis routes capture faults, tool faults, stale memory, and skill problems to their owners first.
Parameter updates require recurring evidence-backed gaps across independent episodes and available data, cooldown, budget, and resources.

## 3. Run bounded candidate experiments

Prepare a separate environment with a CUDA PyTorch build and `rsi/requirements.txt`.
The local GGUF inference files cannot serve as the original Transformers training checkpoint.
Prepare pinned Hugging Face E2B weights and tokenizer files before execution.
The example uses one GPU, rank 8, context 512, batch 1, and 20 optimizer steps.
Measure peak memory with an actual training smoke test. The cleaned-corpus commands support a separate native tokenizer check.

```powershell
python -m rsi train --config TRAINING.json --dataset DATASET_DIRECTORY --output rsi/runs/train-001
python -m rsi registry initialize --root rsi/private/registry --role actor --config TRAINING.json --execute
python -m rsi cycle --config rsi/config.example.json --plan PLAN.json --dataset DATASET_DIRECTORY --suite SUITE.json --output rsi/runs/cycle-001 --registry rsi/private/registry
```

`TRAINING.json` contains the `training` object from the example configuration, without its outer wrapper.
For direct actor training, also include its fixed `scope` in that object. Cycle supplies the configured actor scope automatically.
Train and cycle report plans by default. Add `--execute` to perform model work.
Training supports actor and proposer roles with separate parent adapters.
It records base weight hashes, adapter lineage, dataset hash, environment, and optimizer budget.

Self-edits declare actor version, task, sources, selected kinds, data instruction, mix, and bounded training settings.
The initial materializer is `verified-replay`. It selects already verified episodes and changes their bounded training mix and settings.
Novel data-generation instructions require another verified materializer. Arbitrary generated code is not executed.

Cycle freezes its dataset and binds the child adapter to that snapshot and the original parent.
Candidate training and evaluation run in separate processes.
The registry uses an expected champion lease, artifact hashes, and atomic replacement.
The default budget permits one experiment per Asia/Shanghai calendar day, including failed attempts.

```powershell
python -m rsi evaluate --suite SUITE.json --baseline BASELINE.jsonl --candidate CANDIDATE.jsonl --policy POLICY.json --judgments JUDGMENTS.jsonl --evidence RECEIPTS.jsonl --output REPORT.json
python -m rsi registry rollback --root rsi/private/registry --role actor --execute
python -m rsi merge --config TRAINING.json --adapter ADAPTER_DIRECTORY --output MERGED_DIRECTORY
```

Evaluation separates baseline and candidate predictions from host receipts and independent judgments.
Tool cases need emitted calls, matching arguments, and result evidence.
Memory and goal cases need their declared state effects.
Persona and reasoning cases need a pinned, calibrated independent judge bound to both case and prediction hashes.
The example leaves judges empty. Production quality gates reject missing calibration.

The local backend parses Gemma native function calls and replays host-recorded tool steps in isolation.
Cases place steps under `environment.tool_replay` and bind the environment hash.
Each step declares `name`, exact `arguments`, `status`, `result`, and observed `effects`.
Only an actual matching model call consumes the step and produces receipts.
These fixtures test recorded scenarios. They do not execute real messages or mutate live AIRI memory.
Other serving stacks can supply a host backend with the same prediction and receipt contracts.

Promotion requires complete coverage, independent groups, target improvement, protected-slice noninferiority, and bounded token and latency regressions.
Bootstrap intervals resample episode groups. Failure to detect a regression is not a noninferiority result.
Merge loads the original base and uses a safe full-precision adapter merge.
The package does not replace the active GGUF server. Serving and requantization need their own acceptance checks.

## 4. Learn update strategies

`strategies.py` stores actual measured wins, failures, manual baselines, and diverse plans for each actor version.
The archive does not reuse stale scores as rewards for a different actor version.

```powershell
python -m rsi propose --connection CONNECTION.json --context AGGREGATES.json --output PLAN.json
python -m rsi meta-build --archive rsi/private/strategies --actor-version ACTOR_HASH --output rsi/runs/meta-001
python -m rsi meta-evaluate --trials TRIALS.json --actor-version ACTOR_HASH --budget-sha256 BUDGET_HASH --training-task-ids TRAINING_TASKS.json --output META_REPORT.json
python -m rsi meta-promote --trials TRIALS.json --actor-version ACTOR_HASH --budget-sha256 BUDGET_HASH --training-task-ids TRAINING_TASKS.json --adapter PROPOSER_ADAPTER --registry rsi/private/registry --execute
```

Proposal generation uses aggregate learning context. It does not receive sealed test answers.
Model calls require `--execute`. Remote connections also need `allow_remote` and credentials named through `key_env`.
Provider response bodies and credentials are omitted from request errors.

Meta data comes only from measured model-proposed wins on independent adaptation tasks.
At least ten independent tasks are needed to materialize training splits.
Train the resulting dataset with `train --role proposer --parent CURRENT_PROPOSER`.
Proposer release compares actual held-out adaptations with the same actor and verified budget ceilings.
Training tasks cannot overlap those adaptation trials.
The candidate proposer is not promoted from its own quality claims or from archive size.

Collect an adaptation cohort against a frozen actor before publishing an actor change.
Set `auto_publish: false` in the cycle configuration to retain that actor while collecting independent adaptation results.
After selection, run `release-actor --run CYCLE_DIRECTORY --registry REGISTRY_DIRECTORY --execute`.
Release recomputes the report from frozen predictions, evidence, policy, and data before updating the registry.
Meta adaptation trials remain an explicit workflow. The watcher does not automatically manufacture or certify these trials.

## 5. Persist autonomous goals

`GoalStore` owns interests, model-authored or human-authored goals, milestones, next actions, budgets, and event history.
It supports revision, pause, resume, abandonment, and evidence-backed completion.
An action reservation survives restarts. An expired or uncertain effect needs reconciliation before another dispatch.
Goal selection requires no new user message.

```powershell
python -m rsi goals create --root rsi/private/goals --scope SCOPE.json --input GOAL.json --command-id create-001 --execute
python -m rsi goals select --root rsi/private/goals --scope SCOPE.json
python -m rsi goals tick --root rsi/private/goals --scope SCOPE.json
```

`tick` reports the next action by default.
To permit the built-in scoped artifact writer, add `--execute --allow-artifact-writes`.
Other real tools require registered host adapters and completion verifiers.
The runner executes at most one reserved action per wake.
Technical artifact assertions establish file effects, not artistic or factual quality.
Semantic project quality needs the configured independent verifier.

The scheduler persists model-authored choices. It does not establish subjective desire.
New interests and next actions can arrive from the model through the same checked API.
The active Electron initiative lane is not automatically replaced by this scheduler.
Goal event history records execution outcomes. A live model producer must also submit its real decision episode and goal correlation.
The scheduler does not fabricate model input or create approved training episodes from a goal specification alone.

## Watch integration

```powershell
python -m rsi watch --config rsi/watch.example.json --once
```

The default reports a plan without changing capture data.
With `--execute`, watch captures the normalized inbox and checks verified learning readiness.
Enable `automation` only after the actor, proof files, release suite, and resource policy are ready.
Watch can then select a fixed plan or invoke a registered proposer, assemble data, and execute an actor cycle.
Set `use_registered_proposer` to load the pinned proposer adapter through the local backend.
A remote proposer must report the registered artifact version as its model identity.
Daily budgets apply before repeated proposal calls.
An optional `goals` configuration supplies `enabled`, `root`, `scope`, and `allow_artifact_writes` for one goal action per poll.
Uncertain imported records remain excluded. No live service starts when this module is imported.

## Implementation limits

The tests exercise synthetic protocols, controlled receipts, state recovery, and orchestration boundaries.
No real E2B optimizer step or production deployment was performed for this implementation.
Actual training needs the original checkpoint, compatible CUDA packages, verified episodes, and independent evaluation fixtures.
The initial materializer does not reproduce SEAL's complete synthetic-data search.
The package exposes the proposer learning and release path without claiming that a better proposer already exists.
