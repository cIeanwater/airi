# 查看模型真实输入与输出 JSON

记录日期：2026-10-17。

本地 Qwen BF16 服务可以保存每次请求的完整 JSON。文件包括传入模型的消息上下文、工具声明、实际提示模板和模型输出。

它只记录经过这个本地服务的请求。更换其他 Provider 后，那些请求不会进入此目录。

## 当前目录与查看方法

本机记录目录：

```text
D:\coding\humanning\.tools\airi-message-records
```

这个目录位于 AIRI Git 仓库之外。记录只留在本机，不随代码提交或推送。

在资源管理器中打开该目录。每个请求对应一个 UTF-8 JSON 文件，用文本编辑器打开即可。

文件名包含 UTC 时间和请求 ID，例如：

```text
20261016T031500123456Z-chatcmpl-0123456789abcdef0123456789abcdef.json
```

北京时间为 UTC 加八小时。文件内的 `started_at` 和 `finished_at` 也使用 UTC。

在 PowerShell 中查看最新记录：

```powershell
$recordsDir = 'D:\coding\humanning\.tools\airi-message-records'
$latestRecord = Get-ChildItem -LiteralPath $recordsDir -Filter '*-chatcmpl-*.json' |
  Sort-Object Name -Descending |
  Select-Object -First 1
Get-Content -LiteralPath $latestRecord.FullName -Raw -Encoding utf8
```

只查看传入的 messages 和最终回复：

```powershell
$record = Get-Content -LiteralPath $latestRecord.FullName -Raw -Encoding utf8 | ConvertFrom-Json
$record.request.messages | ConvertTo-Json -Depth 100
$record.response | ConvertTo-Json -Depth 100
```

完整 JSON 文件本身就是可导出的记录。复制所需文件即可。共享记录前，先检查其中的私人内容。

浏览器 Network 面板中的 `X-Request-ID` 响应头与文件内 `request_id` 相同。它可以把一次网络请求对应到磁盘文件。

## 文件字段

| 字段 | 内容 |
| --- | --- |
| `request_id` | 与网络请求对应的 ID |
| `started_at`、`finished_at`、`seconds` | 请求开始、结束时间及耗时 |
| `status` | `started`、`ok`、`error` 或 `canceled` |
| `http_status` | HTTP 状态。SSE 已开始后发生的错误仍使用 HTTP 200 |
| `request` | 原始 JSON 请求，包含完整 `messages`、`tools`、`stream` 和采样参数 |
| `model_input.messages` | 实际送入模板的消息，包含角色提示、历史、用户输入和工具结果 |
| `model_input.tools` | 实际传入模板的工具声明 |
| `model_input.rendered_prompt` | 模板处理后完整输入 token 解码出的文本 |
| `model_input.prompt_tokens` | 实际输入 token 数 |
| `model_input.max_tokens`、`temperature`、`top_p` | 生效的生成参数，包含服务默认值 |
| `model_input.generation_options` | 实际 `generate` 参数。关闭采样时，不包含 temperature 和 top_p |
| `response` | 普通 JSON 响应，或 SSE 聚合后的同形响应 |
| `response.choices[].message` | 最终助手文本或 `tool_calls` |
| `response.choices[].finish_reason` | `stop`、`length` 或 `tool_calls` |
| `response.usage` | 输入、输出和合计 token 数 |
| `model_output_text` | 生成文本，清理结束标记，保留工具调用的文本封装 |
| `stream_events` | 成功写给客户端的 SSE JSON 事件，按原顺序保存 |
| `stream_done` | 服务是否成功发出 `[DONE]` |
| `error` | 输入错误、生成错误或客户端断开信息 |

服务先保存 `started` 记录，结束时原子替换文件。读取时不会看到写了一半的 JSON。

进程被强制终止时，文件可以保留为 `started`。客户端断开时，`canceled` 记录只能包含已经获得的部分输出。

输入校验失败的请求也会保存。此时 `model_input` 可以不存在，因为请求尚未送入模型。

服务不保存 HTTP 请求头，因此不会记录 `Authorization`。完整消息正文仍包含对话中主动提供的内容。

## 开启、关闭与保留期限

从仓库根目录运行：

```powershell
.\local-models\airllm-cuda-venv\Scripts\python.exe -u .\scripts\serve-qwen35-2b-bf16.py --port 18083 --record-messages-dir 'D:\coding\humanning\.tools\airi-message-records'
```

该命令默认保留七天内的最新 1,000 个请求。服务在启动和写入记录时清理超限文件。

服务没有定时清理任务。停止服务后，旧文件会保留到下一次启动，或由你手动删除。

保留期限通过 `--record-messages-days` 设置。数量上限通过 `--record-messages-max-files` 设置。两者必须是正整数。

关闭记录时，停止当前服务，删除启动参数中的 `--record-messages-dir`，再启动服务。默认启动命令不记录消息。

关闭记录不会删除已有文件。需要清理时，关闭记录后再删除目录中的 JSON 文件。

统一语音启动入口 `python scripts/start-local-voice.py` 显式开启上述记录，符合本机当前配置。
使用 `--no-message-recording` 可选择关闭。已经运行的服务需要先重新启动才能应用不同设置。
单独运行模型服务且没有传入记录目录时，记录仍默认关闭。

`GET http://127.0.0.1:18083/health` 的 `message_recording` 显示开关、目录、保留限制及最近写入错误。

服务没有提供下载这些文件的 HTTP 端点。只读文件需要本机文件权限。

记录只从启用后开始。此前未记录的完整模型请求无法从聊天记录中准确重建。

## 与现有记录的区别

| 入口 | 用途 | 是否含最终模型完整 messages |
| --- | --- | --- |
| 设置 → 数据 → 聊天记录 | 导出持久会话，适合备份和恢复 | 否。它不是每次请求的最终快照 |
| 开发工具 → Context Flow → Last Actual Compose → Details | 查看当前运行期最近一次上下文和 `composedMessage` | 含最近请求的消息投影，不保存模型输出或磁盘历史 |
| 设置 → 系统 → 开发者 → 自动记录 IO traces | 查看耗时、调用关系和错误计数 | 否。span 不保存提示词或回复 |
| 本文的本地服务 JSON | 查看模型真实输入、输出和错误 | 是。只覆盖通过该服务的请求 |

已有聊天持久化说明见[本地聊天记录](adr/2026-10-16-local-chat-persistence.zh-CN.md)。本地模型启动说明见[Qwen BF16 服务](local-qwen35-2b-bf16.zh-CN.md)。

## 检查命令

不加载模型的记录测试：

```powershell
.\local-models\airllm-cuda-venv\Scripts\python.exe .\scripts\test-local-chat-records.py
```

服务启用记录后，运行合成请求验收：

```powershell
.\local-models\airllm-cuda-venv\Scripts\python.exe .\scripts\smoke-qwen35-2b-bf16.py --recording-dir 'D:\coding\humanning\.tools\airi-message-records'
```

验收只读取这轮合成请求对应的文件，不读取已有私人记录。它检查 JSON、SSE、错误记录和请求头排除。
