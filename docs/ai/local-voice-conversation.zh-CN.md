# 本地语音对话

## 当前语音方案

更新日期：2026-10-18。

默认方案使用 Zipformer small 中英双语流式转写、Smart Turn v3.2 判断说话是否结束、Gemma 4 E2B INT4 回复、GPT-SoVITS v2ProPlus 合成。
当前默认声音为 `anime-playful`，另有四种声音可在设置页试听、选择。
它是原始合成参考的升调变体，具体效果与清晰度需试听确认，见[声音选择与参考配置](local-voice-profiles.zh-CN.md)。
声线与韵律由参考声音控制，不支持自然语言情绪指令。ACT 继续驱动人物表情。

ASR 与说话结束判断使用 CPU。Gemma 使用 Vulkan GPU 推理，默认上下文为 65,536 tokens，KV 缓存为 Q8。
TTS 合成核心使用 CUDA FP16，辅助特征模型使用 CPU。合成核心约 176M 参数，CPU 辅助模型另计。
声线选择、固定版本、显存与实测延迟见[小型学生声线研究](research/2026-10-18-compact-student-voice.zh-CN.md)。
Gemma 的准备与运行方式见[Gemma INT4](local-gemma4-int4.zh-CN.md)。
人格、记忆和上下文修改入口见[上下文编辑](companion-context-editing.zh-CN.md)。

| 作用 | 当前本地服务 |
| --- | --- |
| 说话结束判断 | `http://127.0.0.1:18085/v1/turn`，Smart Turn v3.2 CPU |
| 语音转写 | `http://127.0.0.1:18086/v1/`，`zipformer-small-zh-en-int8` CPU |
| 实时转写 | `ws://127.0.0.1:18087/v1/stream`，在对应 Provider 配置 `localStreamingUrl` |
| 对话 | `http://127.0.0.1:18083/v1/`，`gemma4-e2b-int4`，本地记录网关 |
| 对话推理内核 | `http://127.0.0.1:18088/`，llama.cpp Vulkan，仅供本地网关使用 |
| 语音输出 | `http://127.0.0.1:18084/v1/`，`gpt-sovits-v2proplus`，`anime-playful` |

此前 MOSS Nano、Qwen BF16、Whisper 与 Qwen3-TTS 1.7B 的记录保留为历史。
见[轻量语音研究](research/2026-10-17-fast-local-voice.zh-CN.md)、[原轻量方案验收](validation/2026-10-17-fast-local-companion.zh-CN.md)、[输入研究](research/2026-10-17-modern-voice-input.zh-CN.md)和[输出研究](research/2026-10-17-expressive-tts.zh-CN.md)。
这些记录不代表当前默认模型。

本机双击仓库根目录 `Start-Humanning.cmd` 即可启动。入口会复用健康服务，保留原桌面 profile 和聊天数据。
当前仍是源码运行，需要已有 Node/pnpm 依赖、Python 环境与权重；不是独立安装包。

桌面入口优先使用父目录 `.tools/node-v26.7.0-win-x64/node.exe`，其次查找系统 PATH 中的 Node。
启动器直接运行 `electron-vite.js`，并为桌面子进程加入该 Node 目录。
因此，双击入口不要求 Windows 全局 PATH 中已有 Node，也不依赖调试终端的环境。
自备 Node 可用 `--node-path D:\path\to\node.exe` 指定，或设置 `HUMANNING_NODE`；显式指定时优先使用该路径。
运行时须满足 `electron-vite` 的版本要求：Node 20.19 至 20.x，或 Node 22.12 及以上。
缺失或不可用时，入口会在启动模型前报错。

准备好各服务的运行环境和权重后，在仓库根目录运行：

```powershell
python scripts/start-local-voice.py --check
python scripts/start-local-voice.py
```

该入口使用本机已有的独立环境，不执行安装或下载。
默认 TTS 环境是父目录 `.tools/gpt-sovits-venv`，运行目录是 `.tools/gpt-sovits-v2proplus`。
参考声音是父目录 `.tools/modern-voice-2026-10-17/vivian-curious.wav`。
它后台启动服务和原桌面 profile，并复用通过健康检查的已有服务。
`--check` 检查文件路径与桌面 Node 版本。服务启动时另行校验模型权重。
其他目录通过 `--tools-dir` 与 `--models-dir` 指定。
只启动服务时使用 `--services-only`，此模式不需要 Node 或桌面依赖。

| 参数 | 用途 |
| --- | --- |
| `--llm gemma` | 默认 Gemma INT4，启动推理内核与消息记录网关 |
| `--llm qwen` | 显式运行已准备的 Qwen3.5-2B BF16 |
| `--context-size 65536` | Gemma 默认 64K 上下文，范围为 4,096 至 131,072 |
| `--voice-profiles PATH` | 使用本地声音配置，界面可选择其中的声音 |
| `--tts gpt-sovits` | 默认学生声线，需要已准备的 CUDA 运行环境 |
| `--tts moss` | 显式运行原 MOSS Nano CPU 服务，需要其独立环境 |
| `--no-message-recording` | 关闭完整模型请求 JSON，保留聊天历史 |

增加上下文会增加 KV 缓存占用。当前默认不预分配 128K 上下文。
Gemma 网关在开始提供服务前，用公开合成文本预热常用提示词。
首次启动会等待模型加载与预热，这些文本不写入用户的消息记录。
`--llm` 与 `--tts` 选择后台服务，桌面当前角色卡仍须选择对应模型和声音。
切换时，先停止原先占用同一端口的服务，再运行新选项。
启动器不会把占用 `18084` 的旧 MOSS 服务误认为新学生声线。

此前桌面启动环境与 Node 的回归结果见[启动验收](validation/2026-10-18-windows-launcher.zh-CN.md)。

默认开启本地模式，不需要账号登录。账号、云同步、官方托管 Provider 和产品遥测入口已关闭。
切换模型时要同时保存当前角色卡的语音设置和全局设置。本机已完成此项。
本地 ASR/TTS 的 API Key 填 `local` 作为兼容占位值，两者只监听回环地址，不向外提供服务。
默认入口不加载 Qwen3-TTS 1.7B。

开发工具的两个 V 浮标默认关闭。只有显式设置 `AIRI_VUE_DEVTOOLS=true` 后重新启动开发服务器才会出现。

统一入口按本机当前配置开启完整消息 JSON，保存至父目录 `.tools/airi-message-records`。
默认保留七天内最新 1,000 份。使用 `--no-message-recording` 可关闭此项。
复用中的模型服务配置不同于请求参数时，入口报错并要求重新启动该服务。
记录字段及查看方法见[消息 JSON](local-message-json.zh-CN.md)。
消息右键菜单的“查看完整上下文”可以读取该轮保存的输入和输出 JSON。
历史记录已过期、未开启记录或不能唯一匹配时，界面会说明原因，不重建猜测的请求。

日志目录为父目录 `.tools/local-voice-logs`。
该启动入口不注册开机任务。电脑重启后需再次运行。
当前准备目录面向 Windows。其他系统可按研究文档分别启动服务。

## 此前的 Kokoro 配置与验收

以下内容保留此前 Kokoro 部署的检查记录。当前默认输出配置见上表。

此前的桌面与角色卡配置如下：

| 配置 | 当前值 |
| --- | --- |
| Provider | `openai-compatible-audio-speech` |
| Base URL | `http://127.0.0.1:8880/v1/` |
| API Key | `local`，仅作为兼容客户端的占位值 |
| Model | `tts-1`，由本地 Kokoro 服务提供 |
| Voice | `zf_xiaoxiao`，中文女声 |
| 输出静音 | 关闭 |

先前的配置为 `speech-noop`，它会直接跳过合成。角色卡也保存相同的关闭配置，因此只修改全局 Provider 不够。

在仓库根目录运行以下命令：

```powershell
.\local-models\airllm-cuda-venv\Scripts\python.exe .\scripts\serve-local-kokoro.py
```

保留此终端，或者由自己的本地进程管理器运行该命令。重启电脑后，需要再次启动服务。

此入口复用父目录 `.tools/Kokoro-FastAPI-0.8.1` 的 Python 环境、权重与声音文件。它不安装依赖，也不修改系统启动策略。

服务使用 CPU，仅监听 `127.0.0.1:8880`。Hugging Face 与 Transformers 均使用离线模式。

浏览器 CORS 仅允许本地开发及测试页面：`localhost:5173`、`127.0.0.1:5173`、`127.0.0.1:5174`、`localhost:4173`、`127.0.0.1:4173`。

这些 CORS 配置限制浏览器跨域访问。它们不提供本机进程认证。

只检查已有文件：

```powershell
.\local-models\airllm-cuda-venv\Scripts\python.exe .\scripts\serve-local-kokoro.py --check
```

使用其他运行目录或端口：

```powershell
python .\scripts\serve-local-kokoro.py --runtime-dir D:\path\to\Kokoro-FastAPI-0.8.1 --port 8880
```

如果改变端口，请同步修改 AIRI Provider 的 Base URL。

## 2026-10-17 Kokoro 桌面验收

验收使用已知合成中文，保留原先选中的聊天会话。

| 检查 | 结果 |
| --- | --- |
| 本地模型启动 | CPU 正常启动，68 个声音包可用 |
| 实际 AIRI Provider 合成 | 664.5 ms，返回 46,892 字节 |
| 解码后语音 | 2.928 秒，44,100 Hz，120,688 个非零采样，RMS 0.102 |
| 实际语音队列 | `startResponse → write/end → finishResponse` 返回 `finished` |
| 浏览器播放 | `AudioBufferSourceNode` 已启动并触发 `ended`，峰值 0.505 |
| 播放后状态 | `AudioContext` 为 `running`，角色已结束说话 |
| 当前会话 | 保持不变 |

这里验证了浏览器实际输出音频及播放完成。操作系统输出设备和用户主观听感需要用户现场确认。

本机验收元数据在父目录 `.tools/tts-synthesis-2026-10-17.json` 与 `.tools/tts-playback-2026-10-17.json`。

这些报告不包含用户录音或真实聊天内容。

入口检查命令 `python -m py_compile scripts/serve-local-kokoro.py` 与 `python scripts/serve-local-kokoro.py --check` 均通过。

使用新入口重新启动后，`GET /health` 返回 `healthy`，`localhost:4173` 的 CORS 预检返回 200。

## 自动发送与停顿

本机已启用自动发送，输入模式为 `always-on`，结束判断开启，语音改写关闭。
改写会额外调用一次聊天模型，不适合作为低延迟默认。转写文字仍可手动修改。
音频判断服务在约 600 ms 停顿后读取本轮末尾最多八秒的声音，判断语句是否结束。
未完成时继续等待。续说会取消旧判断；当前速度配置最多等待两秒静音。
设置 → 模块 → 听觉可调整最长静音等待，普通默认仍为六秒，适合说话中有长停顿的情况。
音尾判断直接读音频，不需要先等转写出字。
模型存在误判，详细结果见输入研究文档。

关闭音频语义判断时，原静音模式继续使用配置中的 2,000 ms 发送延迟。
停顿时间不包含 ASR、改写、模型回复或 TTS 的计算耗时。

停顿策略和续句回归见[语音回合结束机制](adr/2026-10-17-voice-turn-completion.zh-CN.md)。

## 流式输出与说话打断

模型以 SSE 增量输出文字，语音按自然句子进入合成队列。
GPT-SoVITS 在整句合成结束前返回 PCM，播放器按响应头中的 32 kHz 单声道格式播放。
此流程不需要等整段回复结束后再播放。

播放期间检测到连续约 96 ms 的有效说话活动后，当前回复被打断。
打断会取消模型请求、TTS 请求、已排队的声音和后续旧片段。
麦克风继续工作，保留开头的音频，新的话语仍按停顿规则自动发送。
浏览器报告缺少回声消除能力时，打断功能也继续启用。
真实扬声器回声与麦克风距离会影响 VAD，实机效果仍需现场试听。

## 状态标记、表情与口型

模型确实输出控制标记。一次真实合成验收回复包含：

```text
<|ACT {"emotion":"happy","motion":"Flick"}|>
```

聊天显示会移除控制标记。后台 JSON 的 `response.choices[].message.content` 保留模型原始回复。
当前人物的动作必须存在于已加载的模型中。自由文字不能创建新的动画。
提示词现提供当前动作组名单，舞台遇到不存在的动作时仍会按情绪选择有效动作。

实际 Hiyori 的笑眼、脸颊和嘴形参数已确认变化，语音期间口型也会跟随音频。
详细自然帧采样见[表情联动验收](validation/2026-10-17-voice-expression-link.zh-CN.md)。

## 此前的 Kokoro 完整自动对话验收

测试文件为 `packages/testing-audio/cases/local-conversation/case.audio.web.test.ts`。
它只把麦克风替换成仓库的合成 WAV，其余使用真实 VAD、Whisper、2B BF16、Kokoro 和播放队列。
自动发送、语音改写与结束判断均开启。测试没有点击发送按钮。

首轮通过，耗时 37.73 秒。输入包含 14 秒预热静音，该时间不是用户响应延迟。
合成会话中的用户和助手消息、模型输出、非零 TTS 音频及播放完成均有断言。
七段实际播放均正常结束，没有被取消或打断，合计语音时长约 7.44 秒。

验收使用隔离的临时 `18082` ASR，测试后已关闭服务并删除临时凭据。
真实 `8082` ASR、`18083` 模型和 `8880` TTS 保持运行。
未修改用户原先选中的会话。

复跑前先准备允许测试页面 `4173` 的本地 ASR，并设置同一临时 API key。
输入测试的服务环境参考[音频验收](validation/2026-10-16-audio-reception.zh-CN.md)。

```powershell
$env:TESTING_AUDIO_LOCAL_CONVERSATION = '1'
$env:TESTING_AUDIO_LOCAL_ASR_BASE_URL = 'http://127.0.0.1:18082/v1/'
$env:TESTING_AUDIO_LOCAL_LLM_BASE_URL = 'http://127.0.0.1:18083/v1/'
$env:TESTING_AUDIO_LOCAL_TTS_BASE_URL = 'http://127.0.0.1:8880/v1/'
pnpm --config.verify-deps-before-run=false -F @proj-airi/testing-audio exec vitest run --config ../../.temp/audio-receive-check/vitest.edge.config.mjs --project audio-web cases/local-conversation/case.audio.web.test.ts
```

该临时配置仅把测试浏览器改为本机已安装的 Edge。标准环境可以直接使用包内 Vitest 配置。

| 检查命令 | 结果 |
| --- | --- |
| `pnpm --config.verify-deps-before-run=false typecheck` | 全仓 55 个任务通过 |
| `pnpm --config.verify-deps-before-run=false lint` | 0 错误，655 条既有警告 |
| `pnpm --config.verify-deps-before-run=false -F @proj-airi/stage-tamagotchi build` | 通过 |
| `pnpm --config.verify-deps-before-run=false -F @proj-airi/stage-web build` | 通过 |

停顿相关 19 项回归、运行时提示词 3 项回归和上述完整链路用例均通过。
