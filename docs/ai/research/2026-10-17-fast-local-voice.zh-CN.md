# 本地语音的延迟与轻量模型

## ASR：常驻 CPU 流式识别，讲话期间出字

输入采用小型中英 Zipformer，模型 ID 为 `zipformer-small-zh-en-int8`。
它直接接收 16 kHz 单声道 PCM16，通过 WebSocket 返回可修订的中间文字。
结束录音时只刷新最后的解码状态，不再等整段声音上传后运行 Whisper。
CPU 模型在服务启动时加载一次，不在每句话开始时重新启动模型或浏览器 Worker。

模型来自 [Sherpa 官方小型中英 Zipformer 部署说明](https://k2-fsa.github.io/sherpa/onnx/pretrained_models/online-transducer/zipformer-transducer-models.html#sherpa-onnx-streaming-zipformer-small-bilingual-zh-en-2023-02-16)。
固定原始资产版本为 `csukuangfj/k2fsa-zipformer-bilingual-zh-en-t@e2382758de9a0219b4efe682b95af30b399db3b8`。
运行时使用 `sherpa-onnx==1.13.8`、`sherpa-onnx-core==1.13.8` 和 `websockets==15.0.1`。
这些包安装在父目录 `.tools/fast-asr/venv`，不改动 Whisper、对话模型或 TTS 的环境。

### 选择与实际限制

| 候选 | 实测或部署结果 | 本轮选择 |
| --- | --- | --- |
| Whisper small CPU | 同句中文通常还需约 1.6 秒文件转写，且录音中没有中间结果。 | 保留独立入口，取消默认使用。 |
| Zipformer 中文 14M INT8，约 25 MB | 5 秒中文约 70–90 ms。中文合成句可用，但公开中文首字有错误，不支持英文。 | 已实测，未作为默认。 |
| 小型中英 Zipformer，约 60 MB | 支持真正流式。三段中文可懂，英文测试保留请求内容。 | 当前默认。 |
| SenseVoiceSmall INT8，约 228 MB | 是快速非自回归候选，但需要整段或模拟流式。此次大文件下载超时，没有运行速度数据。 | 不等待下载，不虚报实测。 |

全 INT8 的小型中英配置出现明显吞字。
按官方示例保留 FP32 decoder 后，三段中文与英文用例通过可用性检查。
当前 encoder 与 joiner 是 INT8，decoder 为 FP32，总运行资产为 60,142,871 字节。
启动前逐项验证四个运行文件的 SHA-256，数值已与下载时固定的发布清单核对。

| 文件 | SHA-256 |
| --- | --- |
| `encoder-epoch-99-avg-1.int8.onnx` | `db6f51551762e40e549166fe041ea3e45464370b595e9ad23f06478ec3794fbb` |
| `decoder-epoch-99-avg-1.onnx` | `89be509a83175261695bdef5fd1c7b9ab1129a663d1284e7ba9f8507b21e0906` |
| `joiner-epoch-99-avg-1.int8.onnx` | `bdda356d6f9b8c2d7cee9ee0e26075fa537490f7fd06520be408d287073667b9` |
| `tokens.txt` | `a8e0e4ec53810e433789b54a5c0134a7eaa2ffca595a6334d54c00da858841d3` |

这次选择优先降低交互延迟，不宣称中文识别率超过 Whisper。
三句合成中文中，两句去标点后逐字一致，一句多识别了一个“我”。
公开中文句“开放时间”仍把首字识别为“太”，说明口音和轻声仍需更多现场测试。
英文输出使用大写，原始记录保持大写，没有把错误改写成预期文字。
这些少量样本不能外推为真实麦克风准确率。

### 同句 CPU 延迟

每句分别发起三次完整 WAV 请求，取中位数。
两种服务使用完全相同的三段已知中文 SAPI 合成 WAV。
Whisper 语言固定为中文，避免把自动语言识别成本算成轻量模型优势。

| 已知中文样本 | 新 ASR HTTP 中位 | 其中推理 | 原 Whisper HTTP 中位 |
| --- | --- | --- | --- |
| 你好，我叫小林，今天想聊学校趣事 | 172.05 ms | 152.234 ms | 1692.84 ms |
| 请告诉我明天上午九点的天气 | 168.84 ms | 154.850 ms | 1634.52 ms |
| 我想先问一个问题，然后再决定 | 150.78 ms | 130.583 ms | 1607.29 ms |

表格名称是片段摘要，实际比较使用相同的完整录音。
这轮文件转写耗时约降到此前的十分之一。
服务、构建和操作系统负载会影响结果，不能把它当成全场景速度保证。

进一步按真实速度每 100 ms 发送一批音频，讲话结束前已收到 13 次中间文字。
同一 5.83 秒中文样本的全部解码计算分摊到讲话期间，累计约 151 ms。
发送结束控制后，最后刷新约 8.09 ms，收到最终文字约 8.56 ms。
这才是流式输入在句末新增的识别等待，不是等完整 151 ms 再开始识别。
另一轮实时发送测试在约 780.54 ms 收到第一段文字，讲话结束后约 9.04 ms 收到最终文字。
该轮记录位于同目录 `validation.json`，同样有 13 次中间结果。

性能与已知文字保存在父目录 `.tools/fast-asr/validation-with-whisper.json`。
没有读取私人录音或真实聊天正文。

### 停顿和额外模型调用

保留 Smart Turn v3.2 CPU，在约 600 ms 停顿后判断是否说完。
三句完整合成中文的完成概率为 0.942、0.988、0.986，判断耗时约 25–27 ms。
正常完成路径不需要等待最长静音上限。

模型也有误判。此前 13 段公开中文小样本中，两个完整句被判为未完成。
原来的 6 秒兜底会把这种情况变成明显的长尾等待。
因此新增“最长思考停顿”设置，普通默认保留 6 秒，当前用户低延迟配置设为 2 秒。
继续讲话会取消旧判断并重新计算停顿，最短 300 ms 有效人声门槛保持不变。
2 秒更快，也更容易把长思考停顿切成两条消息。需要长思考时可调回 6 秒。

语音转写改写现在默认关闭。
原先每句话在对话前还会请求一次对话模型，最慢可以额外等待 10 秒。
关闭后直接提交 ASR 文字，草稿编辑、手动改写开关和原始转写仍保留。

### 本地服务与配置

```powershell
..\.tools\fast-asr\venv\Scripts\python.exe -u scripts/serve-fast-asr.py --model-dir ..\.tools\fast-asr\zipformer-bilingual-small --port 18086 --ws-port 18087 --threads 2
```

| 项目 | 当前值 |
| --- | --- |
| HTTP 健康检查 | `http://127.0.0.1:18086/health` |
| 兼容模型目录与文件转写 | `http://127.0.0.1:18086/v1/` |
| 原生实时输入 | `ws://127.0.0.1:18087/v1/stream` |
| Provider | `openai-compatible-audio-transcription` |
| Model | `zipformer-small-zh-en-int8` |
| Provider 配置 `localStreamingUrl` | `ws://127.0.0.1:18087/v1/stream` |
| 语音转写改写 | 关闭 |
| 说完判定 | Smart Turn，停顿约 600 ms |
| 当前最长思考停顿 | 2000 ms |

两个端口仅监听 `127.0.0.1`，HTTP 与 WebSocket 均拒绝外部浏览器 Origin。
每条 socket 独占当前话语状态。取消、断连或输入错误会释放对应流，不影响下一条话语。
服务不保存音频、转写文字或请求正文。

WebSocket 首帧是 `{"type":"start","model":"zipformer-small-zh-en-int8","sample_rate":16000}`。
随后是 PCM16 二进制消息，结束时发送 `{"type":"finish"}`。
服务返回 `partial` 或 `final` JSON，包含完整的当前 `text`。
中间文字可以修订前面的识别结果，客户端不能简单累加每条文字。

### 输入检查

- 三段已知中文、英文用例、实时中间文字和句末刷新均通过真实 CPU 服务检查。
- HTTP 与 WebSocket 外部 Origin 均返回 403，错误模型首帧被拒绝。
- 真实 Edge 检查增量转写、调用取消、错误返回和非本地地址拒绝。
- 跨窗口检查最长思考停顿同步，默认/reset 检查改写默认关闭。
- 两秒与六秒安全上限、续说取消和短噪声门槛有独立回归。

可复现的服务基准：

```powershell
..\.tools\fast-asr\venv\Scripts\python.exe scripts/benchmark-fast-asr.py --fixtures ..\.tools\fast-asr --report ..\.tools\fast-asr\validation.json
```

比较旧 Whisper 时，添加 `--whisper-base-url http://127.0.0.1:8082 --whisper-token-file ..\.tools\voice-secrets\asr.token`。
令牌只在进程内用于请求头，不写入报告。
完整应用验收通过 `TESTING_AUDIO_LOCAL_ASR_STREAM_URL` 启用原生流式输入。

## TTS：采用能实测流式输出的中文 CPU 模型

当前低延迟语音输出采用 `OpenMOSS-Team/MOSS-TTS-Nano-100M-ONNX`。
它配合 `MOSS-Audio-Tokenizer-Nano-ONNX` 在 CPU 上运行。
本次没有用较小但不支持中文的模型替换中文语音能力。

官方项目在 2026-04-10 发布 Nano，2026-04-17 发布独立 ONNX CPU 路径。
它支持中文、48 kHz 双声道和流式解码，代码采用 Apache-2.0。
资料见 [MOSS-TTS-Nano 官方仓库](https://github.com/OpenMOSS/MOSS-TTS-Nano)。

本次服务只导入官方 `ort_cpu_runtime.OrtCpuRuntime`，再用 SentencePiece 编码文本。
它没有导入会带入 PyTorch 的高层 `onnx_tts_runtime.py`。
真实运行检查中 `torch_loaded=false`，ORT 使用 `CPUExecutionProvider`。
因此 TTS 不再与 BF16 对话模型竞争 CUDA 显存。

## 固定资产与环境

| 项目 | 当前值 |
| --- | --- |
| 对外模型 ID | `moss-tts-nano-100m` |
| 服务入口 | `scripts/serve-local-moss-tts.py` |
| 地址 | `http://127.0.0.1:18084/v1/` |
| 默认线程 | 2，可以用 `--threads` 修改 |
| 运行目录 | 父目录 `.tools/moss-tts-nano` |
| 独立环境 | `.tools/moss-tts-nano/venv` |
| NumPy | 2.2.6 |
| ONNX Runtime | 1.23.2，CPU 版本 |
| SentencePiece | 0.2.1 |
| 音频格式 | 48,000 Hz，2 声道，PCM16 little-endian |

| 公开模型 | 固定 revision | 本机资产体积 |
| --- | --- | --- |
| [TTS ONNX](https://huggingface.co/OpenMOSS-Team/MOSS-TTS-Nano-100M-ONNX) | `f52645cb467506d8e18e746ddd59482685b74e58` | 672,619,352 字节，10 文件 |
| [Audio Tokenizer ONNX](https://huggingface.co/OpenMOSS-Team/MOSS-Audio-Tokenizer-Nano-ONNX) | `ceff0d0749bfb3fa2d61149794ec6feef0d1e1ae` | 90,572,161 字节，6 文件 |

安装阶段下载清单中的 16 个运行文件，并按发布者 LFS SHA256 检查。
权重与环境保存在本机工具目录，没有进入 GitHub 源码仓库。
服务启动后只读本地文件，不临时下载模型。
`--check` 检查运行文件与库版本，不加载 ONNX 会话，也不重复计算大型文件哈希。

## 声线与表现力边界

服务提供三个官方内置中文女声音色：

| Voice ID | 官方音色名 |
| --- | --- |
| `xiaoyu` | Xiaoyu，当前默认 |
| `yuewen` | Yuewen |
| `lingyu` | Lingyu |

音色由官方模型清单中的提示编码提供。
本次没有另外采集用户录音或下载外部参考人声。
服务也没有暴露任意参考录音克隆接口。

Nano 根据文本与标点生成语调，不提供 Qwen3-TTS 1.7B 的自然语言风格指令接口。
传入非空 `instructions` 时，服务返回明确错误。
ACT 仍能驱动人物表情，但不能据此宣称 Nano 已接受情绪指令。
学生形象是否适合当前声线，需要通过试听判断。

Qwen3-TTS 1.7B BF16 作为用户可选的表现力方案保留。
它的情绪指令与测试结果见[表现力语音研究](2026-10-17-expressive-tts.zh-CN.md)。
低延迟 Nano 与较慢的指令控制语音各有取舍。

## 线程选择的实际测量

下表对同一中文句子、同一 Xiaoyu 音色分别运行独立进程。
句子与此前 Serena 试听相同，内容为原创测试文本。
下表和三句初测先解码 1 个音频帧，再按 8 帧解码。

| 线程 | 加载 | 首次 PCM | 完整生成 | 音频时长 | RTF |
| --- | --- | --- | --- | --- | --- |
| 1 | 4.781 秒 | 0.513 秒 | 3.673 秒 | 5.52 秒 | 0.665 |
| 2 | 3.753 秒 | 0.329 秒 | 2.873 秒 | 5.52 秒 | 0.520 |
| 4 | 3.514 秒 | 0.210 秒 | 2.965 秒 | 5.52 秒 | 0.537 |

RTF 是生成时间除以音频时长，数值小于 1 表示生成快于播放。
本次默认选择完整生成较快的 2 线程。
4 线程在这一轮的首包更快，完整生成没有更快。
线程结果受 CPU 负载、缓存和系统调度影响，不能推广到所有机器。

## 真实 HTTP 流与此前 Serena 的同文比较

以下测量使用已经加载的 CPU 服务。
首包从客户端发起 HTTP 请求开始计时，直到读到第一批 PCM。
它不是简单的响应头到达时间。

| 原创测试片段 | Nano 首 PCM | Nano 完整生成 | Nano 音频时长 | Nano RTF | 此前 Serena 完整生成 |
| --- | --- | --- | --- | --- | --- |
| 你回来啦，今天过得怎么样 | 0.3073 秒 | 2.3628 秒 | 5.52 秒 | 0.4280 | 15.61 秒 |
| 我在听，先别着急 | 0.2888 秒 | 2.5569 秒 | 6.16 秒 | 0.4151 | 17.64 秒 |
| 真的吗，这个想法好有意思 | 0.2986 秒 | 2.4053 秒 | 5.44 秒 | 0.4422 | 15.56 秒 |

这三段完整生成耗时约为此前方案的六分之一。
各行使用相同的完整原文，但音色和生成音频时长不同。
这些数据不包括停顿检测、ASR、对话模型生成和播放设备延迟。
它们不能直接当成完整对话延迟。

WAV 和测量 JSON 位于父目录 `.tools/moss-tts-nano/benchmark`：

- `xiaoyu-friendly.wav`
- `xiaoyu-gentle.wav`
- `xiaoyu-curious.wav`
- `http-three-phrases.json`
- `threads-1.json`、`threads-2.json`、`threads-4.json`

三个 PCM 结果均有非零音频，并能正常封装为原生采样率的 WAV。
同一服务的非流式 WAV 接口也完成真实检查。

### 播放开始阶段的分块修正

首块只有 80 ms，紧接着等待 8 帧会让播放队列短暂耗尽。
当前保留首个 1 帧，在累计输出少于 0.64 秒时按 2 帧解码，然后恢复 8 帧。
这不增加首块等待，也不改变 HTTP 或音频格式。

同一句 28 字问候复测，HTTP 首 PCM 为 316.6 ms，第二块间隔为 66.9 ms。
前五块为 80 ms、160 ms、160 ms、160 ms、160 ms，之后恢复 640 ms。
生成 5.52 秒音频用了 2.2731 秒，服务 RTF 为 0.4118。
此前同句 HTTP RTF 为 0.4280，这一轮没有出现明显减速。

按收到首块后等待 20 ms 的理想 HTTP 队列模拟，最大断流间隔为 0。
这项模拟不包含浏览器调度和播放设备，桌面实际间隔需要单独验证。
记录见 `http-startup-chunk-tuning.json`，音频见 `xiaoyu-friendly-startup-tuned.wav`。

## 可懂度检查

三个新 WAV 经本地 Whisper small 回读，语言固定为中文。
前两个片段去掉标点后与原文逐字一致。
第三个片段把开头“诶”写成“哎”，并输出了繁体字。
没有发现整句丢失或大段吞字。

回读耗时分别为 2.437、1.266 和 1.406 秒。
结果保存在同目录的 `asr-readback.json`。
这些文件只包含已知原创合成样本，没有读取用户录音或真实对话。
ASR 回读不能替代用户对音色、语调和情感的现场试听。

## 流式接口与取消

标准完整音频请求保持 OpenAI Compatible 的请求形式：

```json
{
  "model": "moss-tts-nano-100m",
  "voice": "xiaoyu",
  "input": "你回来啦！今天过得怎么样？",
  "response_format": "wav"
}
```

边生成边播放使用：

```json
{
  "model": "moss-tts-nano-100m",
  "voice": "xiaoyu",
  "input": "你回来啦！今天过得怎么样？",
  "stream": true,
  "response_format": "pcm"
}
```

响应使用 HTTP chunked body，音频类型为 `audio/pcm`。
每个 body 字节都是原始 PCM16 little-endian，不包含 WAV 头或 JSON 包装。
采样按左右声道交错排列。

客户端必须读取以下响应头，不能硬编码为 24 kHz 或单声道：

| Header | 当前值 |
| --- | --- |
| `X-Audio-Sample-Rate` | `48000` |
| `X-Audio-Channels` | `2` |
| `X-Audio-Sample-Format` | `pcm_s16le` |

TCP 数据块不保证与一个采样或一帧对齐。
客户端需要保留未凑齐的字节，累计后再解码与播放。
浏览器 CORS 显式公开上述响应头，只允许回环 HTTP(S) Origin。

模型与 codec 状态由一个串行锁拥有。
重叠请求返回 429，不把已经过时的语音排入长队。
服务逐帧检查连接，在检测到客户端断开时终止生成并重置 codec。

真实取消测试先读取长句的首批 PCM，再关闭连接。
45.6 毫秒内观察到取消计数增加，失败计数保持为零。
它不再像旧 Qwen 入口一样必须等待整个旧音频生成完成。
取消仍需等待正在运行的一次 ONNX 调用返回，不是操作系统级强制中断。
记录见 `http-cancellation.json`。

输入上限为 240 个字符，单次生成最长检查为 60 秒。
服务不会记录请求正文、用户录音或生成的音频。
控制台仅输出请求 ID、字数、音色和时间等元数据。
验收脚本明确保存的试听 WAV 是例外。

## 启动与检查

在仓库根目录运行：

```powershell
..\.tools\moss-tts-nano\venv\Scripts\python.exe .\scripts\serve-local-moss-tts.py --check
..\.tools\moss-tts-nano\venv\Scripts\python.exe -u .\scripts\serve-local-moss-tts.py --threads 2
```

更换路径时，使用 `--runtime-dir` 和 `--model-dir`。
更换端口时，同步修改 AIRI 的 Provider 地址。

检查命令与结果：

| 检查 | 结果 |
| --- | --- |
| `python -m py_compile scripts/serve-local-moss-tts.py scripts/test-local-moss-tts.py` | 通过 |
| `python scripts/test-local-moss-tts.py` | 6 项真实 TCP 协议回归通过 |
| `python scripts/serve-local-moss-tts.py --check` | 16 个运行文件和 3 项库版本检查通过 |
| 三句真实 CPU HTTP PCM | 全部完成，首 PCM 均小于 0.31 秒 |
| 真实 WAV 请求 | 48 kHz 双声道解码正常 |
| 真实长句断连 | 合成取消，锁释放，失败计数为零 |

协议回归用受控运行时替换模型边界，检查传输行为。
真实模型测量另外进行，没有用模拟结果代替模型速度。
完整应用播放、输入结束判断和对话延迟见统一验收记录。
