# Humanning

一个围绕本地记忆、可查看的消息记录、语音对话和角色互动开发的桌面伴侣项目。

[English](../README.md) · [项目概览](ai/project-overview.zh-CN.md) · [上游 AIRI](https://github.com/moeru-ai/airi)

本项目基于 [moeru-ai/AIRI](https://github.com/moeru-ai/airi) 开发。
AIRI 提供应用框架、角色渲染与模型 Provider 接口。本仓库重点改进本地对话、记忆、语音和调试能力。

## 已实现的改进

| 方向 | 当前能力 | 说明 |
| --- | --- | --- |
| 本地推理 | Qwen3.5-2B BF16，OpenAI 兼容接口，普通 JSON 与 SSE 输出 | [本地模型服务](ai/local-qwen35-2b-bf16.zh-CN.md) |
| 长期记忆 | 按用户、角色授权，检索历史证据，编辑、删除和 JSON 导入导出 | [记忆模块](../packages/memory-local/README.md) |
| 记忆评测 | 固定数据版本、可复跑的 LongMemEval-S 检索评测和机器可读报告 | [评测协议与结果](../packages/memory-local/benchmarks/README.md) |
| 消息持久化 | 等待助手消息保存再报告完成，导出最新消息，拒绝账号切换后的过期写入 | [本地持久化机制](ai/adr/2026-10-16-local-chat-persistence.zh-CN.md) |
| 完整消息 JSON | 按需保存实际请求上下文、模板展开内容、模型输出、工具调用和错误 | [查看真实输入输出](ai/local-message-json.zh-CN.md) |
| 语音对话 | 随应用分发的离线 VAD、停顿后自动提交、语音播放和完整链路回归 | [语音对话配置](ai/local-voice-conversation.zh-CN.md) |
| 角色互动 | 情绪与动作标记、有效动作选择、Live2D 表情和随音频变化的口型 | [表情联动验收](ai/validation/2026-10-17-voice-expression-link.zh-CN.md) |

当前本机已启用 Smart Turn v3.2 与 Qwen3-TTS-1.7B CustomVoice，默认 Serena 声线。
自动发送、实际播放及 ACT 语气接线已通过[完整闭环验收](ai/validation/2026-10-17-modern-voice.zh-CN.md)。
当前语音合成仍慢于播放速度，音尾模型存在误判。配置与限制见研究文档。

## 消息、记忆与数据沉淀

三类数据分别管理：

- **聊天记录**：自动存入本地 IndexedDB，可从应用导出完整 JSON 备份。
- **长期记忆**：独立授权的跨会话证据，具有自己的用户与角色作用域、编辑和删除入口。
- **模型请求记录**：本地模型服务开启记录后，保存每次请求的实际上下文与输出。

长期记忆默认关闭，授权只对当前用户和角色生效。
检索出的记忆会加入所选模型的请求。如果使用远程 Provider，这部分证据也会发送给它。
聊天备份与模型 JSON 包含对话正文。它们属于本地私人数据，不作为项目源码上传。

仓库不分发模型权重、浏览器 profile、凭据、私人录音和真实聊天导出。
本地模型入口不会自动下载权重。

## 从源码运行

发布工作流使用 Node.js 26.7.0，根目录固定 pnpm 11.24.0。
模型运行环境和权重需要单独准备。各服务文档记录了本机验收环境。

```sh
git clone https://github.com/cIeanwater/airi.git humanning
cd humanning
pnpm install
```

启动桌面应用：

```sh
pnpm dev:tamagotchi
```

或启动浏览器应用：

```sh
pnpm dev:web
```

以上命令只启动应用。模型、转写与语音合成服务需要分别配置和启动。
已有角色卡会保存 Provider 选择。更换模型时，需要同时修改全局配置与当前角色卡。
服务文档中的绝对路径描述已验收的本机环境，其他设备需要替换路径。

本分支尚无单独验收的安装包。上游 AIRI 安装包不包含本仓库的这些改动。

## 评测与开发

LongMemEval-S 应用预算使用六个证据块，在 470 道可回答问题上得到 79.61% 会话召回率和 60.00% 轮次召回率。
这是关键词检索结果，不是模型回答准确率，也不是中文检索质量评测。
[评测文档](../packages/memory-local/benchmarks/README.md)记录数据版本、输入预算、源码哈希和适用边界。

近期验收记录：

- [音频、模型回复与持久化](ai/validation/2026-10-16-local-audio-chat-persistence.zh-CN.md)
- [离线语音检测](ai/validation/2026-10-17-offline-voice-detection.zh-CN.md)
- [语音回合结束判断](ai/adr/2026-10-17-voice-turn-completion.zh-CN.md)
- [语音与表情互动](ai/validation/2026-10-17-voice-expression-link.zh-CN.md)

全仓检查：

```sh
pnpm typecheck
pnpm lint
```

记忆模块测试：

```sh
pnpm -F @proj-airi/memory-local test
```

部分语音集成测试需要本地服务与显式环境变量，对应文档给出了要求。
验收结果描述特定环境的一次运行，不代表所有操作系统或音频设备都已通过测试。

## 上游与许可

本项目基于 Neko Ayaka 与 Moeru AI 贡献者开发的 [Project AIRI](https://github.com/moeru-ai/airi)。
更广泛的平台能力见 [AIRI 上游文档](https://airi.moeru.ai/docs/)。

源码保留原始版权声明与 [MIT 许可证](../LICENSE)。
模型权重、音色、人物素材与第三方组件分别遵守各自的许可证。
