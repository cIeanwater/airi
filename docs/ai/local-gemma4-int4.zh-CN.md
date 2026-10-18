# 本地 Gemma 4 E2B INT4

## 模型来源与范围

使用 Google 官方的 [Gemma 4 E2B QAT Q4_0 GGUF](https://huggingface.co/google/gemma-4-E2B-it-qat-q4_0-gguf)。固定版本为 `675cff42a74c774d6cb76f76d8eacb49b48c9b93`。

- 文件：`gemma-4-E2B_q4_0-it.gguf`。
- 大小：3,349,516,256 字节，约 3.12 GiB。
- SHA-256：`fa401b55b07ee70a54c6dae3903c783a6e65064312529ea57175cb5f8dec6634`。
- 本地目录：`local-models/gemma4-e2b-int4/`。
- API 模型名：`gemma4-e2b-int4`。

E2B 表示约 2.3B 有效参数。包括逐层嵌入时，总参数量约 5.1B。上述文件大小和实际运行占用比名称更能说明内存需求。

当前接入仅处理文字。没有下载约 987 MB 的多模态投影文件。语音识别与语音合成继续使用各自的独立服务。Gemma 本身不生成少女声线。

## 准备与启动

在仓库根目录运行：

```powershell
& .\local-models\airllm-cuda-venv\Scripts\python.exe scripts/prepare-local-gemma.py --download
```

下载使用公开地址，不读取账号令牌。中断后再次运行会续传。只有大小与官方 SHA-256 都一致时，程序才会发布最终文件。

省略 `--download` 可再次校验文件。校验结果保存在模型目录的 `publisher-manifest.json`。模型权重及清单均处于 Git 忽略目录。

现有 `llama.cpp-b11146-vulkan` 包含 Gemma 4 支持。固定版本的 [架构注册代码](https://github.com/ggml-org/llama.cpp/blob/b11146/src/llama-arch.cpp)包含 `gemma4`。以下参数已通过本机 CLI 帮助核验：

```text
--host 127.0.0.1 --port 18088
--alias gemma4-e2b-int4
--ctx-size 65536 --parallel 1
--gpu-layers all --flash-attn on
--cache-type-k q8_0 --cache-type-v q8_0
--reasoning off
```

默认上下文已由最初的 16K 调为 **64K（65,536 tokens）**。
16K 是排查实时链路时的保守起点，不是 12GB 显卡或模型的上限。
原生服务报告训练上下文上限为 131,072 tokens，启动器允许 `--context-size 131072`。
128K 完整负载的显存与延迟尚未验收，不能用权重大小直接保证。

本机 64K 服务实际接收了 22,844 个输入 tokens，正确返回输入开头的合成验收码。
正文未缓存时首段约 5.416 秒，全程约 5.436 秒，仅复用 18 个 tokens。
相同输入命中 22,843 个缓存 tokens 时，首段约 0.132 秒。
当时整卡占用约 5,006MiB / 12,227MiB，包含语音服务、桌面进程和其他负载。
这是快照，不是满 64K、并发语音或 128K 的绝对峰值。

扩大窗口不会主动填满历史，但长请求会增加预填充时间。
因此需要同时考虑显存、首字等待、语音合成和对话质量。
启动器现在核对原生服务实际 `n_ctx`，不会把旧 16K 进程误报成已按 64K 启动。

本机使用 Vulkan GPU 推理。K、V 缓存均为 Q8_0。启动器默认选择 Gemma，显式传入 `--llm qwen` 可使用原来的 Qwen 服务。

## 首次响应与预热

以下是此前 16K 配置的三条公开合成问题。输入为 482–532 tokens，输出为 27–53 tokens。

| 场景 | 首段文字 | 总耗时 | 缓存输入 |
| --- | --- | --- | --- |
| 冷启动后的第一条问题 | 20.638 秒 | 20.770 秒 | 0 tokens |
| 已缓存人格前缀的第二条问题 | 0.718 秒 | 0.997 秒 | 470 tokens |
| 已缓存人格前缀的第三条问题 | 0.133 秒 | 0.339 秒 | 470 tokens |

原生服务日志中的一次生成速度约为 177 tokens/秒。这是短问题的实测结果，不代表长历史、长记忆或所有对话都具有相同速度。

首条请求的开销明显高于后续请求。启动器现在对网关传入 `--warmup`，先预热公开的人格和记忆规则前缀。网关完成预热后才开始监听。预热不读取真实历史，也不写入用户消息记录。

预热测试已检查请求参数和失败处理。本次正式入口启动约 54.10 秒，包含模型加载与语音预热。
Gemma 网关预热为 0.118 秒，单独重启网关后为 0.036 秒。这两次受益于本机已有驱动与磁盘缓存，不代表全新机器的冷启动。
启动后，桌面合成问题约 1.056 秒开始显示正文、2.091 秒开始播放语音。
编辑人格后，原有前缀缓存不再完全相同。完整测试边界见[本轮验收](validation/2026-10-18-streaming-context-companion.zh-CN.md)。

## 消息记录网关

`scripts/serve-local-gemma.py` 在 `127.0.0.1:18083` 提供原有 OpenAI 兼容接口，固定代理本地 `127.0.0.1:18088`。它不加载 PyTorch 或 Qwen 权重。

- `/health` 只有在上游健康时才返回 `ready`。
- `/v1/models` 返回 `gemma4-e2b-int4`。
- `/v1/chat/completions` 支持普通 JSON 与逐行 SSE 转发。
- 客户端取消后，网关关闭上游连接。上游静默时也会检测取消。
- 在首段文字出现前取消，也会关闭上游请求，避免后台继续生成。
- 网关只转发允许的请求参数，不转发客户端 URL、密钥或 HTTP 头。

上游 JSON 损坏、SSE 提前结束或模型返回错误时，记录状态为 `error`。SSE 已经发出响应头时，网关通过错误事件结束客户端。它不会把不完整的回复记录成成功。

启用 `--record-messages-dir` 后，记录包括原始 JSON、实际转发参数、模型响应、SSE 事件、用量、耗时与结束状态。HTTP 认证头不进入记录。

网关向上游 [apply-template 接口](https://github.com/ggml-org/llama.cpp/blob/b11146/tools/server/server-context.cpp)提交同一份转发参数。该接口与聊天接口共用模板解析入口，返回真实渲染的提示词。失败时明确标记 `rendered_prompt_status: unavailable`，不会用当前对话重新拼出一份历史上下文。

网关强制传入 `chat_template_kwargs.enable_thinking: false`。这是对实际模板行为的设置。`model_output_raw_text` 表示上游 API 返回的内容，不表示未解析的模型 token 流。

请求可以携带 `X-Humanning-Session-ID`、`X-Humanning-Turn-ID` 和 `X-Humanning-Message-ID`。三项齐全时，网关将其写入 `correlation`，供消息菜单精确查询。

`GET` 和 `POST /v1/debug/message-context` 共用只读记录查询。旧记录仅在正文与时间唯一匹配时可返回。未录制、已过期、损坏或存在歧义时会明确返回不可用。

## 原 Qwen 错例的修复

原 Qwen 检查点使用结束符 `248044`，聊天分词器使用 `248046`。生成现在同时接受这两个结束符。

流式文本还会检测完整的协议边界，即使一个边界分散在多个块中。边界后的伪造下一轮对话不会进入可见回复。普通的 `user`、`assistant` 和时间引用仍然保留。

Qwen 记录同时保存原始解码文本 `model_output_raw_text` 和边界处理后的 `model_output_text`。Gemma 的原始字段来自上游聊天 API，不能用它查看未解析的原生 token 流。

## 自动验证

```powershell
& .\local-models\airllm-cuda-venv\Scripts\python.exe scripts/test-local-gemma-gateway.py
& .\local-models\airllm-cuda-venv\Scripts\python.exe scripts/test-local-message-context.py
& .\local-models\airllm-cuda-venv\Scripts\python.exe scripts/test-local-chat-output.py
```

这些测试使用临时合成记录与本地假上游，不读取真实聊天或加载模型。真实模型的速度、显存及长上下文验证由启动验收记录补充。
