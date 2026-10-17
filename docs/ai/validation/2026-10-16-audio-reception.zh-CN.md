# 本地音频输入验收（2026-10-16）

## 结论与验证边界

本次确认两段输入链路正常：实际麦克风可以在浏览器中产生 PCM，受控合成音频可以经过 AIRI 的真实 VAD 和真实本地 ASR，进入可编辑语音草稿。

实际麦克风探测没有保存或上传录音。完整输入测试使用仓库已有的合成 WAV，通过 Chromium 的文件麦克风替代硬件。不能将两段结果合称为真人语音全链路验收。

本报告的输入测试没有验证语音草稿发送到 LLM 后的回答，也没有验证 TTS。Qwen3.5-2B 的原生输入是文本和视觉，本次音频先由独立 ASR 转成文本。

后续实际桌面已经完成合成语音转写 → 2B BF16 回复 → 本地保存 → 刷新恢复，见[综合验收](2026-10-16-local-audio-chat-persistence.zh-CN.md)。

## 实际硬件段

根任务使用独立 Edge 页面，启动参数不含 fake-device 或 fake-audio。Windows 选中有效录音设备后，结果如下：

| 项目 | 结果 |
| --- | --- |
| 音频输入设备数 | 3 |
| 实际选中设备 | 默认 MOMENTUM 4 Bluetooth |
| 音轨状态 | live |
| 设备 / AudioContext 采样率 | 48 kHz / 16 kHz |
| 采样值 / 非零值 | 61,440 / 59,661 |
| peak / RMS | 0.01915 / 0.001843 |

此前浏览器返回 `NotFoundError`。Windows 恢复有效 capture endpoint 后，同一类无模拟参数探测通过。这不是音频代码故障的证据。

根任务随后在实际 AIRI 桌面主窗口（CDP 9250）调用公开的 `settings-audio-devices.askPermission()` 和 `devices.input`，再次确认应用本身可以采集真实麦克风。该进程不含模拟麦克风参数。

| 实际 AIRI 应用检查 | 结果 |
| --- | --- |
| 访问许可 / 选中设备 | true / default |
| 输入设备数 | 3 |
| 采样率 / 帧数 | 16 kHz / 16,000 |
| 非零采样值 / RMS | 15,357 / 0.0116022 |
| 收尾 | 关闭测试输入，保留原先 `enabled=false` |

这次应用侧探测同样没有保存或上传录音。它比独立 Edge 探测更直接地覆盖了 AIRI 的实际设备适配器，但没有将真人录音发给 ASR。

## 受控输入段

测试文件为 `packages/testing-audio/cases/local-hearing/case.audio.web.test.ts`。

链路为：仓库合成 WAV → Edge 文件麦克风 → AIRI 16 kHz PCM → Silero VAD → WAV 上传 → 本地 Speaches / faster-whisper-small → Voice draft 的 textarea 当前值。

ASR 位于 `http://127.0.0.1:18082/v1/`，显式模型名为 `Systran/faster-whisper-small`。服务使用已有离线缓存，运行在 CPU，以 CTranslate2 `int8` 计算。这与 Qwen3.5-2B 的 BF16 要求是两个独立模型。

直接上传同一个合成 fixture 的真实 ASR 请求耗时 3.19 秒，识别出 `Hello, Ari. Please say hello.`。最终 Web 输入测试通过，耗时 25.39 秒。测试同时检查了上传字节数、真实转写和草稿 textbox 中的 `say hello`。

## 运行方式

已有 ASR Python 位于父目录 `.tools/speaches-0.8.3/.venv/Scripts/python.exe`。启动使用 `speaches.main:create_app`，参数为 `--host 127.0.0.1 --port 18082 --factory`。

启动环境如下；API key 的值不写入报告：

```text
HF_HOME=<工作区父目录>/.tools/voice-models/huggingface
HF_HUB_OFFLINE=1
HF_HUB_DISABLE_IMPLICIT_TOKEN=1
HF_HUB_DISABLE_TELEMETRY=1
ENABLE_UI=false
WHISPER__INFERENCE_DEVICE=cpu
WHISPER__COMPUTE_TYPE=int8
WHISPER__CPU_THREADS=4
WHISPER__TTL=300
ALLOW_ORIGINS=["http://127.0.0.1:4173","http://localhost:4173"]
API_KEY=<仅用于临时本地测试的值>
```

本次未读取生产 `.tools/voice-secrets`。临时 key 只用于此测试服务，通过环境变量交给客户端。根任务的交接文件位于 Git 忽略的 `.temp/audio-receive-check/`，ACL 限定当前用户和 SYSTEM。读取时赋给变量，不向日志输出。服务停止后删除该临时凭据文件。

测试环境使用 `TESTING_AUDIO_LOCAL_ASR_BASE_URL`、`TESTING_AUDIO_LOCAL_ASR_MODEL` 和 `TESTING_AUDIO_LOCAL_ASR_API_KEY`。端点必须是 loopback。

```powershell
$env:TESTING_AUDIO_LOCAL_ASR_BASE_URL = 'http://127.0.0.1:18082/v1/'
$env:TESTING_AUDIO_LOCAL_ASR_MODEL = 'Systran/faster-whisper-small'
# 在同一可信终端中，将 ASR 使用的临时 key 赋给此变量，避免打印。
$env:TESTING_AUDIO_LOCAL_ASR_API_KEY = $env:API_KEY
pnpm --config.verify-deps-before-run=false -F @proj-airi/testing-audio exec vitest run --project audio-web cases/local-hearing/case.audio.web.test.ts
```

本机未安装 Playwright 对应版本的 Chromium，因此实际验收使用忽略目录中的 Edge 配置：`--config ../../.temp/audio-receive-check/vitest.edge.config.mjs`。它只将该测试 runtime 的浏览器改为已安装的 Edge。

应用内配置使用 OpenAI-compatible audio transcription Provider，填入上述 base URL、模型名及同一个临时 key。在 Hearing 中选择该 Provider 与模型。中文输入可将语言设为 `zh`。只有启用自动发送，转写才直接发送到聊天；本次测试关闭自动发送，检查语音草稿。

此临时 ASR 的 CORS 仅允许测试站点 4173。其他 Web 端口需要明确加入服务的 `ALLOW_ORIGINS` 后重启；本报告没有将其描述为任意来源可用。

## 自动检查

以下 40 项检查通过，三个集合没有重复计数：

| 检查 | 命令与范围 | 结果 |
| --- | --- | --- |
| Node 输入回归 | `pnpm --config.verify-deps-before-run=false exec vitest run --config packages/stage-ui/vitest.config.ts --project node`，限定 `hearing-transcriber.test.ts`、`voice-activity-plugin.test.ts`、`voice-input-segment.test.ts`、`voice-input-transcription-chain.test.ts`、`hearing.test.ts` | 5 文件，31 项通过；Provider/VAD 分数受控，不能代替真实 ASR |
| 浏览器设备与状态 | `pnpm --config.verify-deps-before-run=false exec vitest run --config .temp/memory-vitest-edge.config.ts --project browser src/stores/settings/audio-device.browser.test.ts src/stores/modules/hearing-status.browser.test.ts` | 2 文件，8 项通过；模拟麦克风 |
| 受控真实 ASR 输入 | 上述 local-hearing 用例，以 Edge 配置运行 | 1 项通过；真实 VAD 和本地 ASR，文件麦克风 |

`pnpm --config.verify-deps-before-run=false -F @proj-airi/testing-audio typecheck` 通过。新增测试的定向 lint 通过。`pnpm --config.verify-deps-before-run=false -F @proj-airi/stage-web build` 通过。

## 桌面的持久配置

根任务的实际桌面来源为 `http://localhost:5173`。为避免将用户配置长期指向测试端口，已启动原有的 `.tools/start-airi-asr.ps1`，正常服务地址为 `http://127.0.0.1:8082/v1/`。

只调整该脚本的 CORS 来源列表，加入 `http://localhost:5173` 和 `http://127.0.0.1:5173`，并保留原来的 `http://127.0.0.1:5174`。真实 OPTIONS 预检返回 200，允许来源精确为 `http://localhost:5173`。脚本原有的离线缓存、CPU 和 `int8` 设置保持原值。

生产凭据文件为工作区父目录的 `.tools/voice-secrets/asr.token`，格式是 44 字节 UTF-8 Base64 文本，脚本读取后执行 `Trim()`。本次只检查文件元数据和受保护 ACL，没有读取内容。根任务可将其直接读入内存并配置应用，不显示到日志。

桌面应选择 `openai-compatible-audio-transcription`，base URL 为上述 8082 地址，模型必须明确选择 `Systran/faster-whisper-small`。原有 `whisper-1` 别名指向未缓存的 large-v3，离线环境下不能代替 small。

根任务已在匿名本地配置中选择这个 Provider、8082 和 small，凭据只保存在本地 opaque 设置，未输出其内容。LLM 另行选择 `http://127.0.0.1:18083/v1/` 的 `qwen3.5-2b-bf16`，不与 ASR 共用端点。

后续恢复服务时，系统执行策略拒绝运行该 PowerShell 脚本。因此改用上述现有 Python 环境的正常 uvicorn 入口。
端口使用 `8082`，沿用脚本中的离线设置、CORS 和受保护凭据，通过进程环境传递 key。
没有修改执行策略。正常 Python 入口不依赖运行 `.ps1` 文件。

受控 fixture 验收结束后，已停止临时 18082 服务并删除其临时凭据交接文件。正常 8082 服务及原有生产凭据保持运行。

旧音频测试工具的 Electron 图标属性选择器和 Web `Hearing` 按钮选择器已不匹配当前界面，因此旧入口测试未到达音频捕获。本次新增用例明确限定 Web，通过正式的麦克风启用设置启动采集，没有修改主应用或这些旧工具。
