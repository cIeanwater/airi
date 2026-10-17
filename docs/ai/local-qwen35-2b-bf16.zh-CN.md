# 本地 Qwen3.5-2B BF16 服务

记录日期：2026-10-16。

这个服务直接加载 `local-models/qwen3.5-2b-bf16` 的原始 safetensors。它不使用旧的 8B GGUF，也不加载低位量化权重。

## 启动

从仓库根目录运行：

```powershell
.\local-models\airllm-cuda-venv\Scripts\python.exe -u .\scripts\serve-qwen35-2b-bf16.py --port 18083
```

该命令常驻运行。后台调用时使用隐藏进程，并把输出写入 Git 忽略的 `local-models/debug/`。本轮通过后台执行会话启动，没有打开额外终端窗口。

服务只监听 `127.0.0.1`。端点和模型如下：

| 项目 | 值 |
| --- | --- |
| AIRI OpenAI Compatible Base URL | `http://127.0.0.1:18083/v1` |
| 模型 ID | `qwen3.5-2b-bf16` |
| 健康与运行证据 | `http://127.0.0.1:18083/health` |
| 模型列表 | `GET /v1/models` |
| 聊天接口 | `POST /v1/chat/completions` |
| 上下文预算 | 输入与请求输出合计最多 8,192 token |
| 输出预算 | `max_tokens` 为 1 到 256，默认 128 |
| 接口输入 | 文本，以及仅含文本的 content 列表 |

端口 `18083` 与原本地 ASR 的 `8082` 分开。旧的 `scripts/start-local-airi.ps1` 保留原样。

桌面需要同时选择 Provider 的模型和当前角色卡的意识模型。只修改全局选择时，已有角色卡仍能使用旧模型。
本次已把这两处配置切换到 `qwen3.5-2b-bf16`，并在页面刷新后确认角色选择保留。

当前默认角色提示超过原先的 4,096 token 预算。因此默认预算改为 8,192，没有裁剪消息。
超限时接口返回输入、请求输出和预算的 token 数，不把提示词正文写入错误。

浏览器 Origin 只接受 `localhost`、`127.0.0.1` 和 `::1` 的 HTTP(S) 地址。服务反射通过检查的 Origin。外域 POST 和预检请求在进入推理前返回 `403`。没有 Origin 的本地 CLI 请求仍可使用接口。

重复启动会在加载权重前检查端口。如果端口已经占用，启动命令退出，已有服务继续运行。

服务使用现有 PyTorch、Transformers 和 Python 标准 HTTP 库。它不安装新依赖，不下载模型。当前环境没有 FastAPI、uvicorn、vLLM 或 SGLang。

## BF16 证据

仅检查模型 ID 无法证明精度。启动时还会执行以下检查：

1. 检查架构、2B 层数与隐藏维度，以及 `text_config.dtype`。
2. 拒绝包含 `quantization_config` 的检查点。
3. 按本地 `publisher-manifest.json` 重算全部文件的 SHA-256。
4. 检查 safetensors 的头部、索引、张量形状、连续偏移和完整文件长度。
5. 将模型以 `torch.bfloat16` 放到 `cuda:0`。
6. 检查实际参数 dtype、设备和量化标志。

单独检查原始权重：

```powershell
.\local-models\airllm-cuda-venv\Scripts\python.exe .\scripts\serve-qwen35-2b-bf16.py --verify-only
```

本轮结果：

| 检查项 | 结果 |
| --- | --- |
| 发布仓库记录 | `Qwen/Qwen3.5-2B` |
| 原始权重文件大小 | 4,548,221,488 字节 |
| 原始张量 | 596 个 BF16，36 个 F32 |
| 原始权重 SHA-256 | `aa33250c4fc64891ddfaba3a314fd9542ea371843c387178b425fbcc5ed680b1` |
| 实际模型类 | `Qwen3_5ForConditionalGeneration` |
| 实际参数数量 | 2,213,241,664 |
| 实际参数 dtype | 617 个参数张量，全部 `torch.bfloat16` |
| 实际参数设备 | 全部 `cuda:0` |
| 权重加载用时 | 3.100 秒，不含此前文件哈希检查 |
| PyTorch / Transformers | `2.9.1+cu128` / `5.17.0` |
| GPU | NVIDIA GeForce RTX 5070 |

原始文件包含 F32 张量。加载时，模型参数统一转换为 BF16。这个操作不是 4 位或 8 位量化。算子和缓存仍能采用 FP32 等内部类型。

完整运行证据写入 `local-models/debug/qwen35-2b-bf16-runtime.json`。它只保存模型、文件哈希和运行信息，不保存聊天正文。

需要查看模型完整上下文与输出时，启用独立的本地消息记录。启动参数、JSON 字段及保留期限见[真实消息 JSON](local-message-json.zh-CN.md)。默认启动命令不记录消息。

## API 回归

服务就绪后运行：

```powershell
.\local-models\airllm-cuda-venv\Scripts\python.exe .\scripts\smoke-qwen35-2b-bf16.py
```

回归使用合成问候，不读取用户会话。报告只保存长度、哈希、token 数和时间。结果写入 `local-models/debug/qwen35-2b-bf16-smoke.json`。

本轮普通 JSON 请求和 SSE 请求均成功。SSE 返回内容块、结束原因、usage 和 `[DONE]`。错误模型 ID、原生音频输入和超大输出预算均返回 `400`。

回归还检查 `http://localhost:5173` 的预检和 SSE 请求，以及外域 POST 与 OPTIONS 的拒绝行为。

一个短文本请求用时约 0.711 秒。随后预热的 SSE 请求用时约 0.248 秒，首个正文块约 0.072 秒。这些数字只代表该合成短输入。

实际桌面使用默认角色提示完成合成语音转写后的请求，用时约 5.84 秒。回复已落入本地 IndexedDB，并在刷新后恢复。
完整链路和验收命令见[桌面综合验收](validation/2026-10-16-local-audio-chat-persistence.zh-CN.md)。

服务按请求串行使用 GPU。已有请求运行时，新生成请求返回 `503`。客户端断开连接后，服务会请求停止生成。请求正文最多 1 MiB。

与独立 TTS 共用显卡时，增加 `--release-cuda-cache`。
它在每轮结束后释放空闲 CUDA 缓存，保持 BF16 模型权重驻留。
`/health.gpu_memory` 返回实时分配与预留字节，`cuda_allocated_bytes` 仍是加载时的记录。
本机与 Qwen3-TTS 1.7B 同时驻留时，4,488 token 合成上下文加 22 token 输出在 2.738 秒完成。
轮后 LLM 分配约 4.435 GB，预留约 4.452 GB，没有出现显存不足。
该测量不包含 ASR 和语音合成，也不代表所有上下文长度的峰值。

函数工具会进入模型的聊天模板。带工具声明的 SSE 请求会等待完整输出，再解析工具调用 JSON。本轮没有完成工具执行质量评测。

## 音频和视觉边界

本地配置只有文本和视觉模块，没有音频编码器。官方模型卡将它定义为带视觉编码器的语言模型，给出文本、图片和视频接口，没有原生音频接口。[Qwen 官方模型卡](https://huggingface.co/Qwen/Qwen3.5-2B)

因此，语音输入使用本地 ASR 转写，然后把文本发送到这个服务。语音回复需要独立 TTS。

原模型具有视觉能力。本次服务只实现文本接口。图片、视频和 `input_audio` 内容会明确拒绝，不会静默丢弃后假装处理成功。

## 启动诊断

首先检查 `/health` 的 PID、模型路径、dtype 和文件哈希。沙盒中的 loopback 查询不一定能访问宿主服务。本轮使用宿主权限确认了实际监听和 API。

如果使用 PowerShell 重定向，输出文件能存在缓冲。模型运行证据 JSON 和 `/health` 不依赖该缓冲。

新增 PowerShell 启动文件在本机执行前后消失。只读事件查询没有找到可归因记录。本轮没有修改安全策略，也没有继续重建该文件。上述正常 Python 启动命令仍可复现服务。
