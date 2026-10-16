# 本地语音、2B BF16 和消息保存综合验收

日期：2026-10-16。

## 实际桌面链路

使用既有 Electron profile 和 `http://localhost:5173`，没有读取个人会话正文。
测试新建标有“合成消息”的独立会话，没有切换用户正在查看的会话。

| 环节 | 结果 |
| --- | --- |
| 真实麦克风 | AIRI 设备适配器采到 16,000 个 PCM 值，其中 15,357 个非零 |
| 设备 | 默认 MOMENTUM 4 Bluetooth，3 个可用输入选项 |
| ASR 输入 | 仓库已有合成 WAV，662,478 字节 |
| ASR | 本地 `8082`，`Systran/faster-whisper-small`，CPU/int8 |
| 转写 | 两次为 3.18 与 3.61 秒，69 字符，命中预期的 `say hello` |
| LLM | 本地 `18083`，`qwen3.5-2b-bf16`，RTX 5070 |
| 回复 | 两次为 5.84 与 6.52 秒，86 与 55 字符，普通用户和助手消息各一条 |
| 立即读取 IndexedDB | 已有 system、user、assistant 三种消息 |
| 页面刷新 | 助手消息、角色的 2B 选择及 ASR 配置保留 |
| 原会话选择 | 保留 |
| 录音收尾 | 真实麦克风测试已关闭，没有保存或上传真人录音 |

硬件段与合成语音段分别验证。完整合成链路经过真实 ASR、真实本地模型和真实 IndexedDB。
独立自动测试还覆盖文件麦克风 → Silero VAD → 本地 ASR → 可编辑语音草稿。
这些结果不能解释成真人说话的识别准确率评测。本次没有验证 TTS。

## 模型配置与精度

实际参数全部为 `torch.bfloat16`，位于 `cuda:0`，没有低位量化。
启动重算全部发布文件的 SHA-256。原始检查点中的 36 个 F32 张量在加载时转换为 BF16。
模型完整证据与启动命令见[本地 2B BF16 服务](../local-qwen35-2b-bf16.zh-CN.md)。

Provider 模型选择与当前角色卡模型选择都已更新。原本仅修改全局模型时，已有角色卡仍指向旧的 8B。
服务的默认输入与输出合计预算从 4,096 增至 8,192 token，使当前角色提示可以完整进入模型。
没有截断历史来绕过超限检查。更长会话超过预算时会明确报错。

## 数据沉淀

本地会话原本已经自动保存。用户消息、助手消息和相关元数据使用 IndexedDB，附件有独立数据库。
这次修复保存完成时机、导出旧快照和取消过程重复保存的问题，并为账号切换的异步保存补上生命周期检查。
设计、路径、导出、备份和长期记忆关系见[本地对话持久化](../adr/2026-10-16-local-chat-persistence.zh-CN.md)。

测试报告只包含耗时、长度、角色列表和断言结果，位于 Git 忽略的工作区工具目录。
模型权重、凭据、浏览器 profile 与真实对话没有作为源码提交。

## 检查命令

| 命令 | 结果 |
| --- | --- |
| `pnpm --config.verify-deps-before-run=false typecheck` | 全仓 55 个任务通过 |
| `pnpm --config.verify-deps-before-run=false lint` | 通过，655 条既有警告 |
| `pnpm --config.verify-deps-before-run=false -F @proj-airi/stage-tamagotchi build` | 桌面类型检查与构建通过 |
| `pnpm --config.verify-deps-before-run=false -F @proj-airi/testing-audio typecheck` | 通过 |
| `python -m py_compile scripts/serve-qwen35-2b-bf16.py scripts/smoke-qwen35-2b-bf16.py` | 通过，使用已有 CUDA Python 环境 |
| `python scripts/smoke-qwen35-2b-bf16.py` | 真实 JSON/SSE 推理、模型精度、错误输入与 CORS 检查通过 |

具体音频测试命令及 40 项通过结果见[音频输入验收](2026-10-16-audio-reception.zh-CN.md)。
会话和编排回归覆盖消息保存等待、导出、取消与账号生命周期，不使用个人消息。
