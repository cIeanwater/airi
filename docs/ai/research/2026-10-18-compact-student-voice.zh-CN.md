# 小型中文学生声线：GPT-SoVITS v2ProPlus

## 选择与边界

这次选择 GPT-SoVITS v2ProPlus，目标是年轻、明亮、轻快的中文学生声线。
它使用参考声音控制音色与韵律。
本次参考是此前在本机生成的原创合成语音，不是未知真人录音。
声音偏好仍需试听判断，不能通过参数量或 ASR 正确率证明。

合成核心在本机加载后共 **175,944,834 个参数**，约 176M。
此数包含 GPT 与 SoVITS，不包含 CPU 上的 BERT、HuBERT、G2PW 和说话人编码器。
不能把它称为“整个系统只有 176M 参数”。

| 方案 | 声音控制 | 本次决定 |
| --- | --- | --- |
| MOSS Nano 100M | 内置三种声线，不接受情绪指令 | 保留为原有方案，不满足本次音色要求 |
| Qwen3-TTS 1.7B | 预设声线与自然语言风格指令 | 用户已排除，不再加载 |
| GPT-SoVITS v2ProPlus | 用参考声音控制音色和部分韵律 | 已完成本机服务与流式适配 |

官方实现支持 v2ProPlus 和流式推理。
本文所有速度与显存数字均为本机测量，不使用官方演示性能代替。
来源：[官方固定版本源码](https://github.com/RVC-Boss/GPT-SoVITS/tree/1b47ca07ef43a284e0b8bd7f29d3e1e3ff76082a)、[官方模型仓库](https://huggingface.co/lj1995/GPT-SoVITS/tree/8f8857563c040d542de108a2c18b0425abd6c9c5)。

## 资源分配

| 部分 | 设备 | 精度与缓存 |
| --- | --- | --- |
| GPT、SoVITS 合成核心 | RTX 5070 12GB | FP16，保留 CUDA 分配器缓存 |
| 中文 BERT | CPU | FP32，4 线程 |
| HuBERT | CPU | 启动时提取固定参考特征 |
| SV 说话人编码器 | CPU | 启动时提取，重复请求复用嵌入 |
| G2PW 多音字、语言检测 | CPU | ONNX Runtime CPU 与 fastText |

主对话模型仍拥有自己的显存和 KV 缓存。
TTS 不启动第二个 Qwen 1.7B 模型。
CPU 辅助模型仍占内存，不能把低显存等同于零内存成本。
本次服务进程工作集约 5.3GiB，其中包括辅助模型和 Python、PyTorch 运行时。

首次准备下载 11 个固定资产，共 2,023,370,274 字节。
其中包含 CPU 辅助权重与 G2PW 压缩包。
解压后的磁盘占用会更大，下载大小也不等于显存占用。

## 声线与接口

| 设置 | 值 |
| --- | --- |
| Provider | `openai-compatible-audio-speech` |
| Model | `gpt-sovits-v2proplus` |
| Voice | `student-bright` |
| 正式本地端口 | `18084`，由启动器传入 |
| 独立验收端口 | `18089` |
| PCM | `pcm_s16le`，32,000 Hz，单声道 |
| 参考时长 | 5.60 秒 |
| 参考文件 | 父目录 `.tools/modern-voice-2026-10-17/vivian-curious.wav` |

参考文本：

> 诶，真的吗？这个想法好有意思！你是怎么想到的呀？

参考 SHA-256：

```text
4a66d4add77898f58bddbc1cf7a51bee3ac6519deaaa0a3d29ab25e91e6e68be
```

流式调用：

```json
{
  "model": "gpt-sovits-v2proplus",
  "voice": "student-bright",
  "input": "今天放学以后，我们一起去看看新开的书店，好不好呀？",
  "stream": true,
  "response_format": "pcm"
}
```

发送到 `POST http://127.0.0.1:18089/v1/audio/speech`。
响应头声明真实的 `X-Audio-Sample-Rate` 和 `X-Audio-Channels`。
前端按响应头解码，不把它伪装成 MOSS 的 48 kHz 双声道。

非流式请求支持 WAV。
单次输入上限为 240 个字符。
不支持的模型、音色、格式和风格指令返回明确错误。
该模型不接受 `instructions`，没有把 ACT 表情标签误称为声音情绪指令。

## 连续输出与打断

前端优先保留完整句子，避免把每个逗号切成独立合成请求。
分段器在标点处按 24 词目标切分较长句子。
无标点的超长内容按最多 240 个 Unicode 字符继续切分，避免服务拒绝整段语音。
模型在句内生成语义片段，SoVITS 增量解码 PCM。
这可以在整句合成结束前播放声音。

音频重叠为 4 个语义 token。
每片最少 24 个语义 token，优先在自然停顿处切分。
HTTP 断开会在每个生成 token 前被检查。
取消关闭生成器并释放独占合成锁，旧请求不会排队继续说。

上游的异常路径会重新加载两个模型。
服务使用单独的取消信号绕过该路径。
取消也不执行每轮全量 Python GC 和 CUDA 缓存清空。
张量仍按正常引用生命周期释放，分配器缓存留给下一轮。

启动时先生成一段公开测试句，完成 CUDA 暖机后才报告 ready。
初始化时间不会被隐藏为用户首轮语音延迟。
本次重启初始化为 43.76 秒，之前一次为 19.18 秒。
磁盘缓存、其他进程负载会影响冷启动。

## 本机测量

时间：2026-10-18。
设备：RTX 5070 12GB，Windows，CPU 辅助模型 4 线程。
测量时 Gemma4 E2B INT4 已驻留，未同时进行 LLM 生成。

以下为最后一轮完整基准，均为新请求生成的声音。

| 原创样本 | 音频时长 | 首 PCM | 完整合成 | RTF |
| --- | ---: | ---: | ---: | ---: |
| `gpt-student-greeting.wav` | 5.12 秒 | 0.538 秒 | 1.459 秒 | 0.285 |
| `gpt-student-school.wav` | 4.48 秒 | 0.520 秒 | 1.500 秒 | 0.335 |
| `gpt-student-gentle.wav` | 6.00 秒 | 1.042 秒 | 1.587 秒 | 0.265 |

前一轮相同三句首 PCM 为 0.424–0.882 秒。
RTF 为合成耗时除以音频时长，低于 1 表示合成快于播放。
这不包含 ASR、LLM 首 token、文本成句等待和实际扬声器延迟。
三句在首包后加入 100ms 缓冲时，按包到达时间计算的供给断流均为 0。
这项传输测量不能代替声卡播放和主窗口端到端检查。

Zipformer 回读结果与三句原文一致，只有问候句的儿化“儿”没有回读。
标点差异未计入内容比较。
ASR 回读只能检查可懂度，不能证明音色偏好和情绪自然度。

最后一轮取消：

- 首 PCM 前断开，到锁释放为 95.8ms。
- 首 PCM 后断开，到锁释放为 24.4ms。
- 取消后新请求成功生成 3.16 秒声音，没有重载模型或返回旧片段。
- 当时累计 7 次合成、4 次取消、0 次失败。

显存：

- PyTorch 峰值 reserved 为 **770MiB**。
- 稳态 allocated 约 **473MiB**。
- 重启前整卡占用 3735MiB，观测峰值 4707MiB，增量约 **972MiB**。
- WDDM 不提供该进程的精确 NVML 显存，整卡差值包含桌面负载波动。
- 整卡采样不能证明瞬间绝对峰值，后续并行生成和更长句子仍需实测。

本次观测未接近 2GB TTS 预算，没有承诺所有输入永远低于该数。

所有结果与试听文件位于父目录 `.tools/expressive-voice-2026-10-18`：

- `gpt-benchmark.json`：请求时间、包到达时间、回读和取消结果。
- `gpt-vram.json`：重启前后整卡采样与 PyTorch 峰值。
- `gpt-student-*.wav`：原创合成试听。

## 准备与复测

固定依赖清单为 `scripts/requirements-local-gpt-sovits.txt`。
现有 `.tools/gpt-sovits-venv` 通过 `.pth` 复用已安装的 CUDA 基础包。
新增包只安装在新环境，未修改原 Qwen TTS 环境。
新机器可以在独立 Python 3.10 环境中安装完整依赖清单。

在仓库根目录运行：

```powershell
..\.tools\gpt-sovits-venv\Scripts\python.exe scripts\prepare-local-gpt-sovits.py
..\.tools\gpt-sovits-venv\Scripts\python.exe scripts\serve-local-gpt-sovits.py --check
..\.tools\gpt-sovits-venv\Scripts\python.exe scripts\serve-local-gpt-sovits.py --port 18089
```

服务启动后，在另一个终端运行：

```powershell
..\.tools\gpt-sovits-venv\Scripts\python.exe scripts\benchmark-local-gpt-sovits.py
```

准备脚本固定官方源码提交与下载版本。
它按发布者 SHA-256 或 Git blob SHA-1 检查每个文件。
校验结果保存在运行时根目录 `publisher-manifest.json`。
服务启动预检 18 个文件，并检查本地源码补丁的 SHA-256。

Windows 缺少 C++ 编译器，不能安装该版本的 `jieba_fast`。
准备脚本只把三个中文前端文件的导入切换为同接口的 `jieba`。
官方项目同时声明了这两个依赖。
脚本记录补丁路径和哈希，遇到其他源码改动会停止。

服务用 SoundFile 读取固定 PCM WAV 参考，避免要求额外 TorchCodec、FFmpeg 解码链。
原因与 [TorchAudio 2.9 的读取接口变化](https://docs.pytorch.org/audio/2.9.0/generated/torchaudio.load.html)一致。
没有修改已安装的 torchaudio 或替换其全局函数。

服务只监听回环地址，浏览器跨域只接受回环 Origin。
上游会打印输入文本，因此请求合成期间屏蔽上游标准输出。
持久日志只含字数、请求 ID、耗时、错误类型和内存指标。
基准脚本会显式保存其公开合成测试句及音频，不读取个人聊天或录音。

## 已运行检查

- `python scripts/test-local-gpt-sovits.py`：5 项真实 HTTP 边界测试通过。
- `serve-local-gpt-sovits.py --check`：18 个文件预检通过。
- 四个 Python 入口的 `py_compile` 通过。
- `stage-ui` 的 `local-stream.test.ts`：7 项通过，包含 32kHz 单声道、整句分段和无标点超长内容回归。
- 三个修改的 TypeScript 文件 ESLint：0 错误，`speech.ts` 原有两项长注释警告。
- `stage-ui` 的 `vue-tsc --noEmit`：通过。

启动器切换、主窗口实际播放和全仓库检查由集成验收完成。
