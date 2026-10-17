# 现代语音输入、输出与桌面闭环验收

日期：2026-10-17。环境：Windows、RTX 5070 12 GB、本地 Electron 舞台。

## 最终配置

| 边界 | 配置 |
| --- | --- |
| 人声检测 | 随应用打包的离线 Silero |
| 说话结束 | Smart Turn v3.2 CPU，`127.0.0.1:18085` |
| 转写 | Whisper small CPU/int8，`127.0.0.1:8082` |
| 对话 | Qwen3.5-2B BF16，`127.0.0.1:18083`，开启空闲 CUDA 缓存释放 |
| 输出 | Qwen3-TTS 1.7B CustomVoice BF16，`127.0.0.1:18084`，Serena |
| 语气 | 角色基调加每段 ACT 情绪快照，完整句优先分段，串行合成 |

输入研究和模型误判见[输入报告](../research/2026-10-17-modern-voice-input.zh-CN.md)。
输出候选、试听、速度和插话限制见[输出报告](../research/2026-10-17-expressive-tts.zh-CN.md)。
统一启动方法见[本地语音对话](../local-voice-conversation.zh-CN.md)。

## 完整自动对话

运行仓库 `local-conversation` 用例，仅用合成 WAV 替换麦克风输入。
其余使用真实 Silero、Smart Turn、Whisper、2B 对话模型、新 TTS 和浏览器播放队列。
用例未点击发送按钮，自动发送、改写和说完判断均开启。

本次一项用例通过，总耗时 67.82 秒，测试主体 66.02 秒。
输入包含 14 秒预热静音，该总时长不是用户说完后的响应延迟。
断言覆盖录音、转写、用户与助手消息、模型输出、非零语音时长和实际播放结束。
所有对应播放追踪均正常结束，没有取消或打断标记。

本次服务日志显示对话请求输入 4,050 token、输出 128 token，模型计算约 9.798 秒。
这些元数据只描述本次合成会话，不能代表所有对话的延迟。

准备好四项服务和应用构建后，在仓库根目录运行：

```powershell
$env:TESTING_AUDIO_LOCAL_CONVERSATION = '1'
$env:TESTING_AUDIO_LOCAL_ASR_BASE_URL = 'http://127.0.0.1:8082/v1/'
$env:TESTING_AUDIO_LOCAL_ASR_API_KEY = [IO.File]::ReadAllText((Resolve-Path '../.tools/voice-secrets/asr.token')).Trim()
$env:TESTING_AUDIO_LOCAL_LLM_BASE_URL = 'http://127.0.0.1:18083/v1/'
$env:TESTING_AUDIO_LOCAL_TTS_BASE_URL = 'http://127.0.0.1:18084/v1/'
$env:TESTING_AUDIO_LOCAL_TTS_MODEL = 'qwen3-tts-1.7b-customvoice'
$env:TESTING_AUDIO_LOCAL_TTS_VOICE = 'serena'
$env:TESTING_AUDIO_LOCAL_TURN_ENDPOINT = 'http://127.0.0.1:18085/v1/turn'
try {
  pnpm --config.verify-deps-before-run=false -F @proj-airi/testing-audio exec vitest run --project audio-web cases/local-conversation/case.audio.web.test.ts
}
finally {
  Remove-Item Env:TESTING_AUDIO_LOCAL_ASR_API_KEY
}
```

本机使用临时配置 `.temp/audio-receive-check/vitest.edge.config.mjs`，只将浏览器选择改为已安装的 Edge。
标准命令使用 Playwright 的 Chromium，需要先准备该浏览器。
ASR 的来源列表必须允许 `http://127.0.0.1:4173`。统一入口已包含该来源。
测试 token 只在内存中读取和发送到本机服务，不进入源码。

## 实际桌面

同一原舞台刷新后，全局配置与当前角色卡均保留新模型与 Serena 声线。
已通过公开语音入口发送原创短句和 `happy`、`Flick` ACT 标记。
捕获的实际 HTTP 请求包含新模型、`serena`、`emotion: happy`、本段情绪指令和 `response_format: wav`。
试听没有创建聊天消息，也没有切换用户当前会话。

| 检查 | 结果 |
| --- | --- |
| 语音响应 | `finished` |
| 实际音频 | 1.6 秒，峰值约 0.394，非零 |
| 音频源 | `start` 和实际 `ended` 事件均发生 |
| AudioContext | `running` |
| 舞台画布 | 可见，867 × 1055 |
| 麦克风 | 枚举到四个输入，开启后音轨为 `live`，无设备或语音错误 |
| 原会话 | 保持选中，无读取或上传其消息正文 |

麦克风检查只确认设备和活跃音轨，没有要求用户现场说话作为测试数据。
完整自动链路使用已知合成输入，不能代替用户口音、噪声环境和主观声线体验的验收。

## 显存共存

两个模型同时以 BF16 驻留，未降低对话模型精度。
4,488 token 合成上下文加 22 token 输出在 2.738 秒完成，没有显存不足。
LLM 轮后分配约 4.435 GB、预留约 4.452 GB。
新的 `--release-cuda-cache` 释放空闲分配，模型权重继续保留。
TTS 活跃分配约 4.18 GB。
该检查与完整对话均通过，但不代表所有输入长度和并发负载均通过压力评测。

## 回归与修复

| 范围 | 结果 |
| --- | --- |
| 音频分段与情绪快照 | pipelines-audio 75 项通过 |
| 语音输出分段与并发契约 | core-agent 输出 11 项通过 |
| 人声区间证据 | core-agent 3 项通过，含重叠去重与间隙拒绝 |
| 输入插件、VAD 与文字判断 | 19 项通过，含 128 ms 短声拒绝和 320 ms 语音通过 |
| 真实 Edge 输入与跨窗口设置 | 3 项通过 |
| 真实 Edge speech 回归 | 20 项通过 |
| Provider 请求体 | 2 项通过，包含显式模型与连接参数隔离 |
| 消息记录 Python 回归 | 6 项通过 |
| Qwen TTS Python 回归 | 6 项通过 |
| 官方特征对齐 | 15 组最大误差 0.0 |
| Smart Turn 中文小样本 | 13 段中 10 段与标注一致，3 段错误 |

修复了 Provider 丢弃额外合成选项的问题，保证 `instructions` 和 WAV 格式真正进入请求。
同时排除连接配置字段，防止其成为 `base_url` 正文或覆盖角色卡所选模型。
音频结束插件增加真实人声累计时长门槛，避免语义模型绕过 300 ms 短噪声过滤。

最终全仓命令：

| 命令 | 结果 |
| --- | --- |
| `pnpm --config.verify-deps-before-run=false typecheck` | 55/55 任务通过 |
| `pnpm --config.verify-deps-before-run=false lint` | 0 错误，655 条既有警告 |
| `pnpm --config.verify-deps-before-run=false -F @proj-airi/stage-tamagotchi build` | 通过 |
| `pnpm --config.verify-deps-before-run=false -F @proj-airi/stage-web build` | 通过 |
| `python -m py_compile scripts/start-local-voice.py scripts/serve-qwen35-2b-bf16.py` | 通过 |
| `python scripts/start-local-voice.py --check` | 通过 |
| `python scripts/start-local-voice.py --services-only` | 成功启动 ASR 并复用其余健康服务 |
| `python scripts/start-local-voice.py` | 全部服务与原桌面健康复用 |

首轮类型检查遇到并行包构建清空 dist 的竞态。
共享包构建稳定后，完整重跑通过，没有修改类型检查配置来隐藏错误。

本机运行元数据位于父目录 `.tools/modern-voice-2026-10-17`。
权重、运行环境、凭据、浏览器 profile 和用户消息记录均不进入源码发布。
