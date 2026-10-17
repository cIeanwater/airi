# Validation record

This record describes the local implementation checks on 2026-08-31. The host ran Windows x64 and Node.js 26.7.0. It does not claim a clean repository-wide result.

## Focused checks

| Check | Result |
| --- | --- |
| Core Vitest suite | 28 tests passed |
| Core TypeScript check | Passed |
| Core `tsdown` build, including declarations | Passed |
| Chat contract suite | 23 tests passed |
| Browser memory integration | 9 tests passed in real Microsoft Edge |
| Stage pages Vue TypeScript check | Passed, with existing Vue macro plugin warnings |
| Targeted ESLint for the core, integration, page, translations, storage, data maintenance, and documentation | Passed |
| LongMemEval-S research profile | Completed. Aggregate metrics were independently reproduced from individual results. |
| LongMemEval-S application profile after formatting | Completed. Aggregate metrics were independently reproduced from individual results. |

The core tests cover persistence, isolation, failed writes, concurrent writes, edits, deletion, imports, expiration, multilingual retrieval, and prompt boundaries. They also cover provider failures, provider deadlines, model mismatch, and deletion during an embedding request.

The chat contract run used the stage UI node project:

```sh
pnpm -F @proj-airi/stage-ui exec vitest run src/stores/chat.contract.test.ts --project node
```

Browser checks use real IndexedDB and Web Locks. The local run selects installed Microsoft Edge through Playwright because the bundled Chromium executable is unavailable. This is a browser launch override, not a platform mock.

The nine browser tests cover scoped consent, persistent isolation, concurrent writers, management operations, storage failures, and the recall deadline. They also cover global deletion without changing the outbox, stale capture after deletion, and capture without a successful pre-send storage read.

Concurrency tests use independent clients in one real browser. They do not establish behavior across two separate operating-system windows. Deletion removes evidence snapshots and preserves content-free generation markers whose keys still identify the scope.

The [benchmark guide](benchmarks/README.md) records both protocols, their limits, and the final metrics. The [research report](benchmarks/reports/longmemeval-s.json) and [application report](benchmarks/reports/longmemeval-s-app.json) include engine source hashes. Documentation edits do not change those hashes.

## Manual interface check

The Chinese Long-Term Memory page rendered without a visible layout issue. A synthetic note persisted after reload. Searching `OpenClaw` found the name within the Chinese note text.

The browser safety review blocked the manual deletion action. No bypass was attempted. The synthetic note remains in the `local` / `default` scope, with automatic memory disabled. Isolated automated tests passed the deletion checks.

## Repository-wide checks

Both required root commands were attempted against the existing dependency installation:

```sh
pnpm --config.verify-deps-before-run=false typecheck
pnpm --config.verify-deps-before-run=false lint
```

Root type checking failed in the pre-existing, untracked file `packages/stage-ui/src/components/scenarios/providers/speech-provider-settings.browser.test.ts`. Lines 39 and 40 report `TS2322`: `Type '{}' is not assignable to type 'string'`. The core memory package and the listed application type checks completed before this failure.

Root lint failed on import ordering at line 13 of the same file. The reported rule is `perfectionist/sort-imports`. The run also reported warnings in other existing files. These unrelated files were not changed to make the memory checks pass.

## Dependency installation limits

The package checks used dependencies already present in this workspace. The pnpm command option prevents its automatic dependency refresh before script execution.

A fresh installation could not complete because registry access failed TLS certificate verification. Offline frozen-lock validation also lacked required registry metadata. No certificate validation was disabled. A clean installation remains an external validation step for a host with working registry access.

## Follow-up validation: 2026-09-29

The same Windows workspace passed the memory core suite (28 tests), the chat contract suite (23 tests), and the memory browser suite (9 tests). The browser run used installed Microsoft Edge through a local Vitest launch override. It exercised real IndexedDB and Web Locks.

The repository-wide `pnpm --config.verify-deps-before-run=false typecheck` and `pnpm --config.verify-deps-before-run=false lint` commands passed. Lint still reports warnings in files outside this memory change. A separate speech settings browser test exposed a persistence bug. The component fix passed that browser test (1 test) and the repository-wide checks.

This follow-up does not include a clean dependency installation or a separate-window concurrency test. The memory implementation remains uncommitted on `stage1` at this date.

## Follow-up validation: 2026-10-13

The upstream merge changed the chat API from message arrays to `Conversation` objects. The memory adapter now uses `conversationId` and `turnId`. Target session metadata supplies the user and character scope. An account change during recall discards the previous account's evidence and capture.

The Windows host ran Node.js 24.19.0 and pnpm 11.25.0. The historical benchmark reports retain their original Node.js 26.7.0 metadata.

| Check | Result |
| --- | --- |
| Core Vitest suite | 28 tests passed |
| Core TypeScript check | Passed |
| Core `tsdown` build, including declarations | Passed |
| Chat contract suite | 58 tests passed, including seven memory regressions |
| Stage UI TypeScript check | Passed |
| Browser memory and data maintenance suites | 12 tests passed: nine memory tests and three maintenance tests |
| Targeted ESLint for merged chat integration and documentation | Passed, with two existing comment-length warnings |
| Frozen dependency installation with scripts disabled | Passed |
| Workspace package builds | 35 tasks passed |
| Repository-wide typecheck | 55 tasks passed |
| Repository-wide lint | Passed, with 655 warnings |
| Desktop build | Passed with the Hugging Face mirror endpoint |

The character regression reproduced the former scope selection: an Alice session used Bob's selected character. The assertion failed with `expected 'bob' to be 'alice'`. Restoring session metadata ownership made the regression pass. The temporary reproduction code was removed.

The added contract tests also cover successful raw-text capture, unchanged history, concurrent sessions with identical message IDs, and stale account metadata. They cover account changes during recall, completion-hook failures, and session resets during generation.

Focused commands ran from the repository root:

```sh
pnpm --config.verify-deps-before-run=false -F @proj-airi/memory-local test
pnpm --config.verify-deps-before-run=false -F @proj-airi/memory-local typecheck
pnpm --config.verify-deps-before-run=false -F @proj-airi/memory-local build
node node_modules/vitest/vitest.mjs run --config packages/stage-ui/vitest.config.ts --project node packages/stage-ui/src/stores/chat.contract.test.ts
node node_modules/vitest/vitest.mjs run --config .temp/memory-vitest-edge.config.ts --project browser packages/stage-ui/src/stores/modules/memory.browser.test.ts packages/stage-ui/src/composables/use-data-maintenance.browser.test.ts
pnpm --config.verify-deps-before-run=false -F @proj-airi/stage-ui typecheck
```

Repository-wide commands completed:

```sh
pnpm --config.verify-deps-before-run=false install --frozen-lockfile --ignore-scripts
pnpm --config.verify-deps-before-run=false run build:packages
pnpm --config.verify-deps-before-run=false typecheck
pnpm --config.verify-deps-before-run=false lint
```

The ignored local browser configuration retains the upstream Vitest project and selects Playwright's installed `msedge` channel. Browser tests use real IndexedDB and Web Locks. They still do not establish concurrency across separate operating-system windows.

Both benchmark reports contain only aggregate metrics. All six source hashes in each report match the current core and benchmark runner. The documented recall metrics match those reports. No benchmark rerun or new answer-accuracy evaluation is claimed.

Git ignore checks exclude benchmark datasets, query details, and the local browser configuration. A strong-pattern scan found no credentials in the memory source, reports, architecture documents, or local model scripts. This scan does not establish the absence of every possible credential format.

The official Hugging Face endpoint failed during desktop resource preparation. The desktop build completed with the mirror endpoint:

```powershell
$env:HF_ENDPOINT='https://hf-mirror.com'
pnpm --config.verify-deps-before-run=false -F @proj-airi/stage-tamagotchi build
```

The installation disabled lifecycle scripts. Desktop native dependency scripts did not run. The Electron bundle build passed, but no installer package or native application launch was checked. This build result does not establish live model inference or every renderer behavior.
