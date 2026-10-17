# 本地中文表现力语音选型与落地

研究日期：2026-10-17。目标是自然中文、年轻成年女学生声线、情绪表达，以及本机可复现的推理。

## 结论

本机采用 `Qwen/Qwen3-TTS-12Hz-1.7B-CustomVoice`，使用原始 BF16 权重。
默认音色为 `serena`，另一项可选音色为 `vivian`。
声线使用模型自带说话人，不需要用户提供参考录音。

Serena 的官方定位是温暖、轻柔的年轻女声。
Vivian 的官方定位是明亮的年轻女声。
1.7B CustomVoice 接受自然语言语气指令。
0.6B CustomVoice 没有官方指令控制能力，因此没有作为本次主模型。
参见 [Qwen3-TTS 官方说明](https://github.com/QwenLM/Qwen3-TTS#custom-voice-generate)。

语音输入、语义结束判断和语音输出使用不同职责的模型。
同属 Qwen 系列，不等于同一个模型实例能完成这些任务。
当前对话模型继续使用 `Qwen3.5-2B BF16`。

## 候选比较

| 候选 | 中文与表现力 | 本次决定 |
| --- | --- | --- |
| Qwen3-TTS 1.7B CustomVoice | 预置年轻女声，原生语气指令，Apache-2.0 | 已部署，支持按 ACT 情绪逐段合成 |
| Qwen3-TTS 1.7B VoiceDesign | 可以用文字设计声音，无需真人参考 | 可继续定制，目前先用稳定预置声音 |
| Qwen3-TTS 0.6B CustomVoice | 体积较小，预置声音，无官方指令控制 | 不满足本次情绪控制要求 |
| Fun-CosyVoice3-0.5B-2512 | 多语言、中文方言、情绪和语速指令，Apache-2.0 | 保留候选，当前任务采用无需参考音色的 Qwen 预置声音 |
| IndexTTS-2.5 | 情绪、语速、发音控制，零样本音色链 | 官方当前版已是 2.5，使用单独的 bilibili 模型许可，本次不引入 |
| Fish Drama 3 / S2-Pro | 自然语言标签、多角色与情绪表达 | Drama 3 为 35B-A3B，S2-Pro 为 4B，当前显存和许可不适合直接替换 |

候选资料来自 [Qwen](https://github.com/QwenLM/Qwen3-TTS)、[CosyVoice](https://github.com/QwenAudio/CosyVoice)、[IndexTTS](https://github.com/index-tts/index-tts)、[Fish Speech](https://github.com/fishaudio/fish-speech)。
没有把较早的 IndexTTS-2 或 Fish 1.5 当成当前版本。

## 本机运行配置

| 项目 | 配置 |
| --- | --- |
| Python 环境 | `local-models/qwen3-tts-venv`，独立环境，不继承系统包 |
| Python | 3.10.20 |
| qwen-tts | 0.1.1 |
| PyTorch / torchaudio | 2.9.1+cu128 |
| Transformers | 4.57.3 |
| GPU | NVIDIA GeForce RTX 5070，12 GB |
| 精度 | BF16，没有量化或后期变调 |
| 注意力 | PyTorch SDPA |
| 模型目录 | `local-models/qwen3-tts-1.7b-customvoice` |
| 服务地址 | `http://127.0.0.1:18084/v1/` |
| AIRI Model | `qwen3-tts-1.7b-customvoice` |
| AIRI Voice | `serena` 或 `vivian` |
| 格式 | 24 kHz，单声道 PCM16 WAV |

官方文档推荐独立环境，并演示 Python 3.12。
本机 Python 3.10 环境已完成真实合成。
FlashAttention 不是本次运行依赖。
导入阶段会提示缺少可选 SoX 和 FlashAttention，实际预置音色合成通过。

模型固定在发布者 revision `0c0e3051f131929182e2c023b9537f8b1c68adfe`。
两项主要文件合计 4,515,695,644 字节。
下载完成后按发布者清单执行 SHA256 检查。

| 文件 | SHA256 |
| --- | --- |
| `model.safetensors` | `38b1d5971bdbd982b561cccec982669a53b0537c3cf5e9bd4778ed07bb2f5137` |
| `speech_tokenizer/model.safetensors` | `836b7b357f5ea43e889936a3709af68dfe3751881acefe4ecf0dbd30ba571258` |

Hugging Face 完整下载在本机速度较慢。
本次使用官方 ModelScope 文件地址分段下载，再检查相同 SHA256。
服务启动后强制使用本地文件和离线模式，不会临时下载权重。

## 启动与切换

新电脑需要先准备独立环境。以下命令使用官方 Python 包源和 CUDA 12.8 轮子：

```powershell
python -m venv local-models/qwen3-tts-venv
.\local-models\qwen3-tts-venv\Scripts\python.exe -m pip install torch==2.9.1+cu128 torchaudio==2.9.1+cu128 --index-url https://download.pytorch.org/whl/cu128
.\local-models\qwen3-tts-venv\Scripts\python.exe -m pip install qwen-tts==0.1.1 transformers==4.57.3
.\local-models\qwen3-tts-venv\Scripts\python.exe -c "from huggingface_hub import snapshot_download; snapshot_download('Qwen/Qwen3-TTS-12Hz-1.7B-CustomVoice', revision='0c0e3051f131929182e2c023b9537f8b1c68adfe', local_dir='local-models/qwen3-tts-1.7b-customvoice')"
```

下载需要约 4.52 GB 权重空间，另有运行环境和缓存。
上面是按本机版本整理的复现命令。当前安装与下载已经完成，无需再次执行。

在仓库根目录检查文件和权重：

```powershell
.\local-models\qwen3-tts-venv\Scripts\python.exe .\scripts\serve-local-qwen-tts.py --check
```

启动服务：

```powershell
.\local-models\qwen3-tts-venv\Scripts\python.exe -u .\scripts\serve-local-qwen-tts.py
```

检查服务：

```powershell
Invoke-RestMethod http://127.0.0.1:18084/health
```

在 AIRI 的语音输出设置中选择 `OpenAI Compatible`。
Base URL 填写 `http://127.0.0.1:18084/v1/`。
API Key 填写本地占位值 `local`。
模型填写 `qwen3-tts-1.7b-customvoice`。
声音填写 `serena`，或者切换为更明亮的 `vivian`。
当前角色卡也必须使用同一语音输出配置。

Kokoro 服务入口保留，作为用户手动切换的备选配置。
它没有被伪装为新模型，也没有被后台静默选回。

## ACT 到语音的连接

```text
模型文字流
  → ACT 解析
  → 当前 intent 的情绪状态
  → 每个 TtsRequest 的不可变 emotion 快照
  → Qwen 单次 instructions 与 emotion
  → 本地 BF16 合成
  → 语音队列播放与口型
```

前置 ACT 影响后续文字。
尾部 ACT 不会回写已经进入合成的前一段文字。
纯动作标记、非法标记和其他控制指令不会清空当前情绪。
`neutral` 可以显式恢复平和语气。
新 intent 不继承上一回合的情绪。

原先的 `onSpecial` 在播放顺序上触发。
提前合成的语音不能依赖这个较晚的事件。
本次保留了表情播放事件，另在合成前建立情绪快照。

语气映射覆盖 `happy`、`sad`、`angry`、`think`、`surprised`、`awkward`、`question`、`curious` 和 `neutral`。
强度进入当次指令，固定基调是自然、温柔的年轻成年女大学生。
问句保留自然疑问起伏，不使用后期抬高音调代替模型升级。

新 Qwen 配置禁用开头软标点加速分段，最长分段目标为 40 个词。
句末标点、控制标记和显式 flush 仍会建立边界。
它按一个并发任务合成，避免多个请求挤占同一张 GPU。
其他语音 Provider 保留原有分段与并发设置。

另一个实际问题是 OpenAI speech Provider 丢弃了额外请求选项。
这会让调用方的 `instructions` 和 `responseFormat` 无法进入 HTTP 请求。
本次修复了这个 Provider 边界，并增加跨请求隔离回归。
连接地址、API Key 和角色卡选中的模型由 Provider 拥有。
它们不会被额外语气参数覆盖，也不会作为 `base_url` 写入 API 正文。

## 原始接口示例

`POST http://127.0.0.1:18084/v1/audio/speech`：

```json
{
  "model": "qwen3-tts-1.7b-customvoice",
  "input": "诶，真的吗？这个想法好有意思！",
  "voice": "serena",
  "response_format": "wav",
  "emotion": "curious",
  "instructions": "轻快地发问，带自然的好奇语气。"
}
```

输入上限为 400 个字符。
服务只返回 WAV，语速用自然语言指令控制。
HTTP 服务串行运行合成，排队等待上限为 120 秒。
不支持的模型、音色和格式返回明确错误。

服务只监听回环地址。
浏览器跨域只允许回环 HTTP(S) Origin。
它不记录请求正文、用户录音或合成音频。
控制台只输出字数、音色、情绪、耗时和音频时长。
合成试听文件由独立验收脚本显式保存。

## 真实测量与限制

以下三个中文片段由本机真实模型生成，没有使用样本音频替代请求结果。
试听文件在父目录 `.tools/modern-voice-2026-10-17`。

| 片段 | 音色 / 情绪 | 音频时长 | 完整合成耗时 |
| --- | --- | --- | --- |
| `serena-friendly.wav` | Serena / happy | 5.28 秒 | 15.61 秒 |
| `serena-gentle.wav` | Serena / sad | 6.56 秒 | 17.64 秒 |
| `vivian-curious.wav` | Vivian / curious | 5.60 秒 | 15.56 秒 |

上述三个原创片段另经本地 Whisper small 回读，固定 `language=zh`。
没有读取用户录音或真实对话。

| 片段 | 回读与原句差异 | ASR 耗时 |
| --- | --- | --- |
| Serena / happy | “你回来啦”识别为“你回来了”，其余文字一致 | 3.047 秒 |
| Serena / sad | “我在听”识别为“我再听”，其余文字一致 | 1.656 秒 |
| Vivian / curious | 开头“诶”识别为“哎”，其余文字一致 | 2.063 秒 |

该比较忽略标点差异。
三个样本均没有出现整句丢失或大段吞字。
这些单字差异仍可能来自 ASR 选择，不能单凭回读定位到 TTS 发音。
完整结果保存在同目录的 `tts-asr-readback.json`。
ASR 可懂度检查不能替代用户对音色、情绪和语调的现场试听。

这些测量没有包括大模型回复耗时。
当前适配使用完整 WAV，不能把官方极低首包延迟宣传作为本机已实现的性能。
本次实测实时率约 2.7 到 3.0，合成速度慢于播放速度。
新的表现力能力有明确的延迟代价。
语音是否符合用户偏好，需要通过上述试听文件和现场对话判断。

插话可以停止本地播放，并丢弃已经过时的音频结果。
当前正在 GPU 上合成的旧请求仍会运行至结束。
它结束后才释放合成锁，因此新回合仍能受到旧请求计算的影响。
当前接口没有实现服务端实时取消，不能承诺插话立即释放 GPU。

加载耗时为 3.421 秒，TTS 活跃显存约 4.18 GB。
与 2B BF16 对话模型同时驻留时，首轮合成没有出现显存不足。
长上下文预填充和完整对话的共存验收由统一启动链路记录。

性能瓶颈包括自回归 Talker、逐帧残差码本预测和完整音频返回。
当前 SDPA 路径没有 CUDA Graph，也没有 `torch.compile` 编译。
独立环境未安装 Triton，不能声称已经得到编译加速。

[vLLM-Omni 官方性能文档](https://docs.vllm.ai/projects/vllm-omni/en/latest/design/qwen3_omni_tts_performance_optimization/)提供 CUDA Graph、批处理和异步音频分块路径。
其[官方安装要求](https://docs.vllm.ai/projects/vllm-omni/en/stable/getting_started/installation/gpu/)明确要求 Linux，当前不原生支持 Windows。
本次没有把 Linux 基准成绩或 H200 延迟填入本机报告。
迁移 Linux 推理后端需要重新测量显存、首包、连续播放和打断行为。

## 已运行检查

| 命令 | 结果 |
| --- | --- |
| `python -m py_compile scripts/serve-local-qwen-tts.py scripts/test-local-qwen-tts.py` | 通过 |
| `python scripts/test-local-qwen-tts.py` | 6 项通过 |
| `python scripts/serve-local-qwen-tts.py --check` | 两项权重 SHA256 通过 |
| `pnpm -F @proj-airi/provider-inference exec vitest run --project node src/providers/cloud/openai-audio/index.test.ts` | 2 项通过 |
| `pnpm -F @proj-airi/provider-inference build` | 通过 |
| `VITEST_BROWSER_CHANNEL=msedge pnpm -F @proj-airi/stage-ui exec vitest run --project browser src/stores/modules/speech.browser.test.ts` | 20 项通过 |

完整仓库检查与实际桌面闭环结果见对应验收记录。
