# Humanning

A local companion with persistent memory, inspectable conversations, and voice and avatar interaction.

[简体中文](docs/README.zh-CN.md) · [Project overview](docs/ai/project-overview.zh-CN.md) · [Upstream AIRI](https://github.com/moeru-ai/airi)

Humanning builds on [moeru-ai/AIRI](https://github.com/moeru-ai/airi). This repository focuses on a local desktop companion and evidence for its behavior.
The AIRI application framework, character renderers, and provider interfaces come from upstream.

## What this repository adds

| Area | Current work | Details |
| --- | --- | --- |
| Local inference | Qwen3.5-2B with BF16 parameters, an OpenAI-compatible API, and JSON or SSE responses | [Model service](docs/ai/local-qwen35-2b-bf16.zh-CN.md) |
| Long-term memory | Consent for each user and character, local retrieval, editing, deletion, and JSON import/export | [Memory package](packages/memory-local/README.md) |
| Memory evaluation | Reproducible LongMemEval-S retrieval runs, pinned data, machine-readable reports, and stated limits | [Evaluation](packages/memory-local/benchmarks/README.md) |
| Durable conversations | Wait for assistant storage before completion, export current messages, and reject stale account writes | [Persistence](docs/ai/adr/2026-10-16-local-chat-persistence.zh-CN.md) |
| Actual model messages | Optional local JSON files with request messages, the rendered prompt, responses, tool calls, and errors | [Message JSON](docs/ai/local-message-json.zh-CN.md) |
| Voice interaction | Bundled offline VAD, automatic turn submission, speech playback, and tests of the complete local flow | [Voice conversation](docs/ai/local-voice-conversation.zh-CN.md) |
| Avatar interaction | Emotion and motion controls, valid motion selection, Live2D expressions, and audio-driven mouth movement | [Expression evidence](docs/ai/validation/2026-10-17-voice-expression-link.zh-CN.md) |

The local voice preset uses a 60 MB bilingual streaming Zipformer recognizer, Smart Turn v3.2, and MOSS-TTS-Nano ONNX on the CPU.
Qwen3.5-2B keeps its BF16 parameters on the GPU. The desktop started playing a known Chinese response in 314 ms after receiving its text.
See the [measured latency and model tradeoffs](docs/ai/research/2026-10-17-fast-local-voice.zh-CN.md). This playback measurement excludes recognition and chat-model time.
MOSS uses text and punctuation for prosody. ACT controls avatar expressions; this model does not accept emotion instructions.

## Data that you can inspect

Three separate records serve different purposes:

- **Chat history** preserves conversations in local IndexedDB. The application can export a portable JSON backup.
- **Long-term memory** stores selected evidence with separate consent, scope, and deletion controls.
- **Model request records** preserve exact inputs and outputs from the local model service after recording is enabled.

Memory is disabled by default for each user and character.
Retrieved memory enters the selected chat provider's request. A remote provider receives that evidence.
Local JSON records and exports contain conversation content and require the same care as the conversations.

Model weights, browser profiles, credentials, personal recordings, and private conversation exports are not part of the source distribution.
The local model service does not download weights automatically.

## Run from source

The release workflow uses Node.js 26.7.0. The root package pins pnpm 11.24.0.
Local model runtimes and weights require separate preparation. See the linked service guides for their tested environments.

```sh
git clone https://github.com/cIeanwater/airi.git humanning
cd humanning
pnpm install
```

Start the desktop application:

```sh
pnpm dev:tamagotchi
```

On the prepared Windows installation, double-click **`Start-Humanning.cmd`** to start the local model services and desktop together.
It reuses healthy services and the existing desktop data directory. See the [local startup guide](docs/ai/local-voice-conversation.zh-CN.md) for preparation and settings.
Local mode is the default: login, account sync, hosted providers, and product telemetry are disabled. Developers can explicitly set `VITE_AIRI_LOCAL_MODE=false` to enable connected features.

Or start the browser application:

```sh
pnpm dev:web
```

These commands start the application. Configure and start the model, transcription, and speech services separately.
Existing character cards store provider choices. Change the active card as well as the global provider.
Machine-specific paths in the local service guides describe the tested installation.

This fork has no separately verified binary release. Upstream AIRI installers do not include the changes described here.
Inherited Cloudflare deployments require explicit activation and your own credentials. See [GitHub Actions configuration](docs/ai/local-deployment-and-github-actions.zh-CN.md).

## Evaluation and development

The application-budget memory report records 79.61% session recall and 60.00% turn recall at six chunks on 470 answerable LongMemEval-S questions.
These are lexical retrieval results, not answer accuracy or a Chinese-language evaluation.
See the [protocol and reports](packages/memory-local/benchmarks/README.md) for budgets, source hashes, and limitations.

Recent integration records:

- [Fast local voice, direct startup, and local mode](docs/ai/validation/2026-10-17-fast-local-companion.zh-CN.md)
- [Audio, model replies, and persistence](docs/ai/validation/2026-10-16-local-audio-chat-persistence.zh-CN.md)
- [Offline voice detection](docs/ai/validation/2026-10-17-offline-voice-detection.zh-CN.md)
- [Voice turn completion](docs/ai/adr/2026-10-17-voice-turn-completion.zh-CN.md)
- [Voice and expression interaction](docs/ai/validation/2026-10-17-voice-expression-link.zh-CN.md)

Run the root checks:

```sh
pnpm typecheck
pnpm lint
```

Run the memory package tests:

```sh
pnpm -F @proj-airi/memory-local test
```

Some voice integration tests need local services and explicit environment variables. Their guides record those requirements.
The recorded results describe specific runs, not a guarantee for every operating system or audio device.

## Credits and license

Humanning is a derivative of [Project AIRI](https://github.com/moeru-ai/airi) by Neko Ayaka and the Moeru AI contributors.
The [upstream documentation](https://airi.moeru.ai/docs/) describes the broader AIRI platform.

The source retains the [MIT license and original copyright notice](LICENSE).
Model weights, voices, character assets, and third-party components retain their own licenses.
