# Local memory evaluation

This benchmark measures evidence retrieval from long conversation histories. It does not measure generated answer accuracy.

The benchmark uses the complete, cleaned LongMemEval-S release from Hugging Face. Each question has a separate history with distractor sessions. The runner selects all 500 questions by default. It excludes 30 abstention questions from evidence recall because these questions have no valid answer to retrieve.

## Run the benchmark

From the repository root, download the pinned dataset:

```sh
python packages/memory-local/benchmarks/download.py --split s
```

If Hugging Face is unavailable, select an HTTPS mirror explicitly:

```sh
python packages/memory-local/benchmarks/download.py --split s --base-url https://hf-mirror.com
```

The downloader checks the official file size and SHA-256 before it publishes the file. It records the actual download URL in a provenance file. Dataset files and individual query results remain in ignored directories. No API key, model download, or paid service is required.

Run the complete benchmark:

```sh
pnpm -F @proj-airi/memory-local benchmark
```

For a short development run, select the first 50 source questions:

```sh
pnpm -F @proj-airi/memory-local benchmark --limit 50
```

For a report under version control, give an output path relative to the package directory:

```sh
pnpm -F @proj-airi/memory-local benchmark --output benchmarks/reports/longmemeval-s.json
```

Run the smaller application budget, including the context formatter:

```sh
pnpm -F @proj-airi/memory-local benchmark --profile app --output benchmarks/reports/longmemeval-s-app.json
```

The runner records the selected range and exclusions. It writes individual scores and source IDs to `runs/s-research-details.json` or `runs/s-app-details.json`. The summary includes source hashes, the MiniSearch version, host details, and measurement settings.

The complete source file is 277 MB. The runner loads it into memory. The measured Windows run used approximately 1.7 GB of process memory, including the dataset. This figure is not the memory footprint of one AIRI memory engine.

## Dataset provenance

| Field | Value |
| --- | --- |
| Official dataset | [xiaowu0162/longmemeval-cleaned](https://huggingface.co/datasets/xiaowu0162/longmemeval-cleaned) |
| Pinned revision | `98d7416c24c778c2fee6e6f3006e7a073259d48f` |
| File | `longmemeval_s_cleaned.json` |
| Bytes | `277383467` |
| SHA-256 | `d6f21ea9d60a0d56f34a05b609c79c88a451d2ae03597821ea3d5a9678c3a442` |
| License metadata | MIT, as stated in the Hugging Face dataset card |
| Original publication | [LongMemEval, ICLR 2025](https://arxiv.org/abs/2410.10813v2) |

The [pinned Hugging Face commit](https://huggingface.co/datasets/xiaowu0162/longmemeval-cleaned/commit/98d7416c24c778c2fee6e6f3006e7a073259d48f) supplies the file size and SHA-256. The cleaned release removes history sessions that interfere with answer correctness. Results from this release are not directly interchangeable with the original release.

The optional `oracle` split contains only evidence sessions. It is useful for diagnostics but cannot measure retrieval among unrelated sessions. Its manifest contains a separate pinned size and SHA-256. The runner labels oracle reports as diagnostics.

## Protocol and metrics

The index contains only dialogue content with its speaker role. It never contains the evaluation question, expected answer, `has_answer`, or `answer_session_ids`. Each question uses a fresh engine with the complete released history. Explicit chunk IDs retain repeated text and its source references.

The runner imports both user and assistant turns. This includes assistant-history questions in LongMemEval. It evaluates the engine with full transcripts, not the default AIRI policy that automatically captures only user turns.

Each turn uses chunks of at most 1,200 UTF-16 code units, with 120 units of overlap. Every method uses the same history and chunk budget. No method expands a retrieved chunk into a complete session.

| Profile | Search budget | Final context budget | Cutoffs |
| --- | --- | --- | --- |
| `research` | 20 chunks, 24,000 units | No formatter | 1, 5, 10, 20 |
| `app` | 6 chunks, 4,000 units | 4,000 units, including source JSON and instructions | 1, 3, 6 |

The application profile scores only records that survive the public context formatter. The benchmark uses its own source IDs. It measures application budgets, not the complete AIRI conversation flow.

| Method | Behavior |
| --- | --- |
| `lexical` | The production engine with lexical retrieval and `diversity: 0` |
| `lexical-diversity` | The same engine with `diversity: 0.15` |
| `recency` | The most recent chunks, ordered by session date and transcript position |

Embeddings and storage are disabled. Recency decay is disabled because questions refer to historical events. All records fit within the engine capacity. The runner stops if ingestion loses records or retrieval loses source references.

Metrics use the first k ranked chunks at each profile cutoff:

| Metric | Definition |
| --- | --- |
| Session Recall@k | Mean fraction of supporting session IDs found in the first k chunks |
| Turn Recall@k | Mean fraction of labeled source turns found in the first k chunks |
| Hit@k | Fraction of questions with at least one supporting source found |
| Complete@k | Fraction of questions with every supporting source found |
| MRR@k | Mean reciprocal rank of the first supporting chunk, or zero when none appears |
| Characters@k | Mean raw text size of the first k chunks |
| Query p50/p95 | Median and nearest-rank 95th percentile of in-memory search duration |

Repeated chunks from one source count once toward recall. Session metrics are less strict than turn metrics. A chunk from a supporting session can omit the required evidence. Even a labeled turn can span several chunks, so a turn hit does not prove answer-span recall.

Query timing starts after ingestion. The order of the two engine methods alternates between questions. Timing excludes dataset parsing, storage, embeddings, and answer generation. The summary reports ingestion duration separately. A single run describes this host, not every browser or device.

## Local results

The checked reports contain aggregate results and results for each question type:

- [Research budget](reports/longmemeval-s.json)
- [Application budget after formatting](reports/longmemeval-s-app.json)

Both reports include all 470 answerable questions. The run used Node.js 26.7.0 on Windows with an AMD Ryzen 7 7800X3D.

| Profile and cutoff | Method | Session recall | Turn recall | Query p50 | Query p95 |
| --- | --- | ---: | ---: | ---: | ---: |
| Research @10 | Lexical | 88.68% | 71.90% | 0.20 ms | 0.60 ms |
| Research @10 | Lexical + diversity | 88.73% | 71.05% | 21.34 ms | 27.24 ms |
| Research @10 | Recency | 3.05% | 0.68% | 0.14 ms | 0.21 ms |
| Application @6 | Lexical | 79.61% | 60.00% | 0.16 ms | 0.49 ms |
| Application @6 | Lexical + diversity | 79.63% | 58.43% | 0.46 ms | 0.83 ms |
| Application @6 | Recency | 2.84% | 0.00% | 0.10 ms | 0.16 ms |

Full-history ingestion took approximately 52 ms at the median and 60 ms at p95 in the research run. Application context formatting took 0.012 ms at the median and 0.017 ms at p95 for lexical results. Independent Python calculations reproduced the reported recall, hit, complete, MRR, and latency aggregates from per-question source IDs.

The full dataset contains 38–62 sessions per question. The scored sample averages approximately 754 chunks per question. The package default uses `diversity: 0` after the ablation showed lower turn recall with diversity. This choice uses the same evaluation data, so it is not independent validation on held-out data.

These results cannot establish Chinese retrieval quality, semantic paraphrase recall, answer accuracy, or abstention accuracy. They also do not evaluate the AIRI capture policy or persistent storage. Package tests cover storage and isolation separately.

## Research notes

| Source | Relevant design evidence | Application here |
| --- | --- | --- |
| [OpenClaw memory search](https://docs.openclaw.ai/concepts/memory-search) | Combines keyword and vector retrieval, then applies deterministic recency, importance, and diversity ranking | Keep lexical retrieval local. Bound candidate work. Measure diversity instead of assuming that it improves every metric. |
| [OpenClaw memory overview](https://docs.openclaw.ai/concepts/memory) | Separates durable memory, source material, retrieval, and management tools | Keep stored evidence and source references independent of the application interface. |
| [LongMemEval](https://arxiv.org/abs/2410.10813v2) | Separates indexing, retrieval, and reading. Studies session decomposition, index expansion, and temporal search. | Evaluate retrieval separately from generated answers. Keep dates and evidence labels available for analysis. |
| [LoCoMo, ACL 2024](https://aclanthology.org/2024.acl-long.747/) | Uses long conversations with temporal and causal relationships | Add a second benchmark before broad quality claims. |

LongMemEval supplies explicit supporting session and turn labels in its [official format](https://github.com/xiaowu0162/LongMemEval#-dataset-format). Those labels permit local retrieval evaluation without a model judge. LoCoMo remains a useful second dataset, but this run does not claim LoCoMo results. Its [official release](https://github.com/snap-research/locomo) provides the primary provenance reference.

The OpenClaw references were inspected on 2026-08-31. Its implementation and defaults can change. This package borrows design ideas, not OpenClaw source code or its benchmark results.
