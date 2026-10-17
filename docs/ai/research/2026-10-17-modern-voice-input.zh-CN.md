# 本地现代语音输入：音频语义说完判定

日期：2026-10-17。

## 已选方案

输入改为 **Silero VAD + Smart Turn v3.2 CPU + 本地 Whisper small**。

三个模型分别解决“有人在讲话”“这句话说完了吗”“说了什么”。
Smart Turn 直接读取原始声音，利用语言及韵律线索判断回合结束。它无需等待 ASR 出字。
当前批量 Whisper 直到录音结束才返回文本，因此此前的文本判定实际无法在录音中工作。

这次更换的是回合结束模型。Whisper 继续负责转写，不再负责决定用户何时说完。
所有输入模型均在本机运行，不调用云端接口，也不上传或落盘麦克风录音。

## 官方方案对比

| 方案 | 能力 | 本机选择 |
| --- | --- | --- |
| Smart Turn v3.2 | 直接听 PCM 判断回合结束，支持中文。8M 参数，CPU int8 模型约 8.7 MB。 | 已接入。无需 GPU。 |
| Qwen3-ASR 0.6B / 1.7B | 多语言及中文方言转写。官方流式推理由 vLLM 提供。 | 可用于后续转写升级。ASR 输出结束符不等于用户说完。 |
| SenseVoiceSmall / FunASR | 中文转写、语言、情绪和声音事件识别。可组合 VAD。 | 更换转写器时的候选。声音事件标签不是回合结束判断。 |
| Fun-ASR-Nano | SenseVoice 编码器与 Qwen3 解码器，提供独立 CPU 和流式部署路线。 | 需独立评测识别率和资源，不替代专用回合判定。 |
| FireRedVAD | 人声活动与声音事件检测。 | 可改进噪声环境中的人声分段，仍需语义回合模型。 |
| Qwen3-Omni | 统一声音理解、文本推理和声音生成。 | 官方 30B BF16 部署超出本机 12 GB 显存，不替换现有 2B BF16 对话模型。 |

官方依据：

- [Smart Turn 官方代码及中文、CPU、8 秒输入说明](https://github.com/pipecat-ai/smart-turn)
- [Smart Turn 官方权重](https://huggingface.co/pipecat-ai/smart-turn-v3)
- [Pipecat CPU 推理参考](https://github.com/pipecat-ai/pipecat/blob/main/src/pipecat/audio/turn/smart_turn/local_smart_turn_v3.py)
- [Qwen3-ASR 官方部署与流式说明](https://github.com/QwenLM/Qwen3-ASR)
- [SenseVoice 官方仓库](https://github.com/FunAudioLLM/SenseVoice)
- [FunASR 官方模型选择](https://github.com/modelscope/FunASR/blob/main/docs/model_selection.md)
- [FireRedVAD 官方仓库](https://github.com/FireRedTeam/FireRedVAD)
- [Qwen3-Omni 官方部署与显存说明](https://github.com/QwenLM/Qwen3-Omni)

## 输入行为

1. Silero 检测人声后开始当前回合录音。
   累计有效人声达到 300 ms 后才允许语义结束。短促杂声不能绕过最短语音门槛。
2. 停顿 600 ms 后，将当前回合末尾最多 8 秒 PCM 送给本机 Smart Turn。
3. 完成概率大于 0.5 时结束录音，随后由 Whisper 转写并自动发送。
4. 未完句继续等候。继续讲话会立即取消旧判断，下一次停顿重新判断完整回合。
5. 连续静音达到 6 秒时结束录音，避免模型故障或一直判断未完成而无法提交。

600 ms 是开始判断的时间，不是固定提交时间。它后面还有模型判断、ASR、对话生成和语音合成延迟。
6 秒是安全上限，不是正常每句话都等待的时间。
静音和噪声不能单独开始输入，入口仍由 Silero 人声检测控制。
没有人声活动时不会调用说完模型。

旧文本判定保留为明确可选模式：音频服务地址留空时启用。
文本模式需要实时转写器，批量转写时只能依赖静音结束。
音频服务不可用时显示错误，并由 6 秒安全上限结束输入。

## 安装与启动

独立环境：`D:\coding\humanning\.tools\smart-turn-v3.2\venv`。
权重：`D:\coding\humanning\.tools\smart-turn-v3.2\smart-turn-v3.2-cpu.onnx`。

模型固定版本：`f766f81d3cfdf7737ac64aad813d91bbfd56bf93`。
模型大小：8,679,182 字节。
SHA-256：`2bb026316b14a660486a75b1733cd3fbab8c2fd0314dc9af7be49f8cca967e4f`。
该值已与官方 LFS 元数据逐字核对。
服务在创建 ONNX 会话之前验证 SHA-256，不匹配则拒绝启动。

依赖安装命令：

```powershell
python -m venv ..\.tools\smart-turn-v3.2\venv
..\.tools\smart-turn-v3.2\venv\Scripts\python.exe -m pip install numpy==2.2.6 scipy==1.15.3 onnxruntime==1.23.2
```

从 [固定版本官方地址](https://huggingface.co/pipecat-ai/smart-turn-v3/resolve/f766f81d3cfdf7737ac64aad813d91bbfd56bf93/smart-turn-v3.2-cpu.onnx) 下载权重。
网络受限时可使用同版本 HF 镜像，但下载后必须核对上述 SHA-256。

从仓库根目录启动：

```powershell
..\.tools\smart-turn-v3.2\venv\Scripts\python.exe -u scripts\serve-smart-turn.py --model ..\.tools\smart-turn-v3.2\smart-turn-v3.2-cpu.onnx --port 18085 --threads 2
```

健康检查：`http://127.0.0.1:18085/health`。
就绪响应必须包含 `provider: CPUExecutionProvider`。
服务只监听 `127.0.0.1`，只接受本机浏览器来源，不记录请求声音。

## AIRI 配置

在“设置 → 听觉”开启自动发送和说完判定。
在“本地音频说完判定模型”填入：`http://127.0.0.1:18085/v1/turn`。
此模式固定采用 600 ms 判断与 6 秒安全上限。
原“自动发送延迟”继续用于关闭语义判定时的静音模式。

对应本地设置：

```json
{
  "settings/hearing/auto-send-enabled": true,
  "settings/hearing/end-judge-enabled": true,
  "settings/hearing/end-judge-audio-endpoint": "http://127.0.0.1:18085/v1/turn"
}
```

服务协议为 `POST /v1/turn?sample_rate=16000`。
正文为 float32 little-endian 单声道 PCM，类型 `application/octet-stream`，最多 8 秒。
响应为 `complete`、`probability`、`inference_ms` 和 `model`。
它不接收文字上下文、不替代对话模型，也不生成语音回复。

## 验证范围

验证使用官方公开中文测试片段及人工生成的静音、噪声，没有读取或上传用户录音。
少量样本只验证部署与链路，不代表独立测试集上的中文准确率。
合成语音经常给未完句添加收尾语调，不能仅凭文字是否完整给模型判断定错。

本机隔离环境为 Python 3.10.20。依赖通过本机 wheel 缓存离线安装，没有修改 ASR 或对话模型环境。
验证工具额外使用 `soundfile==0.14.0` 读取公开 FLAC 文件，服务运行不依赖 SoundFile。

| 检查 | 结果 |
| --- | --- |
| 特征与官方 Pipecat NumPy 实现对齐 | 15 组，最大绝对误差 0.0，形状均为 80 × 800。 |
| 官方中文小样本 | 13 段，6 段完整、7 段未完，10/13 与标注一致，3 段错误。 |
| CPU 推理 | 13 段中位 27.04 ms，包含特征计算。 |
| HTTP 输入、静音及来源检查 | 8 项通过。外部来源 403，NaN、错误采样率和损坏正文 400。 |
| 输入插件行为 | 7 项通过：无文本判断、续说取消、8 秒上限、失败保留、6 秒上限、128 ms 拒绝、320 ms 通过。 |
| 核心 VAD 时长证据 | 3 项通过，累计有效人声帧不重复计算重叠区间，也不接受活动时间缺口。 |
| Edge 跨窗口设置同步 | 1 项通过，没有回传状态提案。 |
| Edge 真 Silero + 真 Smart Turn | 2 项通过。拒绝静音和固定随机噪声，并在批量转写出字前结束公开中文录音并提交。 |
| 相关类型检查 | stage-ui、stage-pages、testing-audio 均通过。 |

官方样本来自 `pipecat-ai/smart-turn-data-v3.2-test`，固定数据版本 `dfc179241fee65b0efe81ca37786f41ce26f38ec`。
使用前 500 行中的全部 13 个中文片段，不挑选正确样本。它们均标为合成语音。
错误行号为 186、452、473。完整结果保存在忽略目录 `.tools/smart-turn-v3.2/validation.json`。
这个小样本结果不能外推为真实麦克风准确率。未完句误结束仍然可能发生。

回归命令：

```powershell
pnpm --config.verify-deps-before-run=false -F @proj-airi/stage-ui exec vitest run src/libs/voice/voice-audio-end-plugin.test.ts --project node
$env:VITEST_BROWSER_CHANNEL='msedge'
pnpm --config.verify-deps-before-run=false -F @proj-airi/stage-ui exec vitest run src/stores/modules/hearing-endpoint.browser.test.ts --project browser
..\.tools\smart-turn-v3.2\venv\Scripts\python.exe scripts/test-smart-turn.py --model ..\.tools\smart-turn-v3.2\smart-turn-v3.2-cpu.onnx --fixtures ..\.tools\smart-turn-v3.2 --reference-features ..\.tools\smart-turn-v3.2\whisper-features.py --output ..\.tools\smart-turn-v3.2\validation.json
```

`--reference-features` 使用 [Pipecat 官方特征实现](https://github.com/pipecat-ai/pipecat/blob/main/src/pipecat/audio/turn/smart_turn/_whisper_features.py)。
本次参考文件 SHA-256：`139b047d10dee42ad5b4894489012e515a882d8f5e2bffe8ad27b30728fc9968`。
特征对齐同时覆盖静音、固定随机噪声和全部中文样本。
浏览器集成用例通过 `VITE_SMART_TURN_TEST_ENDPOINT` 与 `VITE_SMART_TURN_TEST_FIXTURE` 显式开启。
完整本地语音闭环可通过 `TESTING_AUDIO_LOCAL_TURN_ENDPOINT` 开启同一模型，不影响默认离线测试。
