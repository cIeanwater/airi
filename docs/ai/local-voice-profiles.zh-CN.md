# 本地声音选择与参考配置

## 在界面选择

打开「设置 → 语音合成」。
保留 `gpt-sovits-v2proplus` 模型，在声音卡片中选择。
在下方测试文本框输入中文，再点击「测试声音」试听。
选择会写入当前角色卡，并同步到桌面其他窗口。
切换声音复用同一套合成模型，不加载另一个大模型。

| 声音 ID | 界面名称 | 参考与语速 |
| --- | --- | --- |
| `anime-playful` | 动漫 · 轻快俏皮 | Vivian 合成参考升调 4 半音，1.10 倍语速 |
| `anime-soft` | 动漫 · 轻柔明亮 | Serena 合成参考升调 3 半音，1.04 倍语速 |
| `student-bright` | Vivian · 好奇轻快 | 原始 Vivian 合成参考，正常语速 |
| `serena-friendly` | Serena · 明快亲切 | 原始 Serena 合成参考，1.06 倍语速 |
| `serena-gentle` | Serena · 轻柔陪伴 | 原始 Serena 轻柔合成参考，正常语速 |

默认配置为 `anime-playful`。
已有角色卡的明确选择优先，不会被默认值覆盖。
升调版本是可供比较的参考变体，不能通过音高数值证明自然度或少女感。
实际音色和韵律必须试听。
本机五种声音均完成真实合成与切换测试，取消后没有重载模型。
升调版本的部分句子出现 ASR 回读错误，原始版本同句回读更准确。
这说明升调参考存在清晰度折衷，不能把它当作质量无损的声线升级。

## 更换自己的参考

编辑仓库中的 [`configs/local-voice-profiles.json`](../../configs/local-voice-profiles.json)。
需要保留个人配置时，复制此文件到仓库外。
用启动器的 `--voice-profiles` 参数指定副本。

```json
{
  "version": 1,
  "default_voice": "my-character",
  "voices": [
    {
      "id": "my-character",
      "name": "我的角色声音",
      "description": "清亮、轻快的原创合成参考",
      "reference_audio": "./my-character.wav",
      "reference_text": "这里填写参考录音中实际说出的每一个字。",
      "speed_factor": 1.08
    }
  ]
}
```

- `id`：唯一的英文小写 ID，可含数字和连字符，最多 48 字符。
- `name`、`description`：声音卡片显示的名称与说明。
- `reference_audio`：本地 PCM16 WAV，路径相对配置文件所在目录。
- `reference_text`：参考音频的准确中文逐字稿，不是风格提示词。
- `speed_factor`：0.8–1.2，数字越大语速越快。
- `default_voice`：API 未指定 `voice` 时采用的声音 ID。

参考音频必须为 3–10 秒、16–48kHz、单声道或双声道。
使用干净的人声，避免背景音乐、长静音和多人说话。
当前语言固定为中文。
配置最多包含 12 个声音。

GPT-SoVITS 从参考中提取音色、说话方式和部分韵律。
仅把名称改成“少女”不会改变声音。
年轻、俏皮的参考比低沉、成熟的参考更符合当前角色目标。
速度参数进入上游流式解码，不通过修改播放器采样率加速。
来源：[固定官方版本的流式解码与 speed_factor](https://github.com/RVC-Boss/GPT-SoVITS/blob/1b47ca07ef43a284e0b8bd7f29d3e1e3ff76082a/GPT_SoVITS/TTS_infer_pack/TTS.py)。

本机附带参考均来自此前生成的原创合成语音。
升调参考通过 `scripts/prepare-local-voice-references.py` 生成，并记录来源哈希。
权重、参考音频和个人配置副本不随代码上传。

## 使配置生效

修改配置文件后，重启本地语音服务。
再打开语音设置，刷新声音列表。
在列表中选择新的声音。
修改文件不会覆盖当前正在生成的请求。
启动器会核对正在运行服务加载的配置哈希。配置改变后，它会明确要求重启语音服务，避免继续复用旧声线。
仅覆盖同一路径的 WAV 文件时，也需要主动重启语音服务。

在仓库根目录检查配置：

```powershell
..\.tools\gpt-sovits-venv\Scripts\python.exe scripts\serve-local-gpt-sovits.py --voice-profiles D:\my-voices\profiles.json --check
```

使用个人配置启动：

```powershell
python scripts\start-local-voice.py --voice-profiles D:\my-voices\profiles.json
```

## 接口和资源边界

`GET /v1/audio/voices` 只返回声音 ID、名称、说明和语速。
它不返回参考文件路径、逐字稿或原始音频。
`POST /v1/audio/speech` 通过 `voice` 选择已加载的 ID。
HTTP 请求不能上传参考、指定权重或加载任意本地路径。

启动时会检查全部参考和配置，再提取特征。
各声音的参考特征与说话人嵌入缓存在 CPU。
所有声音共用一套 GPU 合成模型，每次只处理一个请求。
HTTP 断开仍会取消当前合成并释放锁。

当前模型不接受自然语言 `instructions`。
ACT 控制表情，尚未转换为独立的 TTS 情绪条件。
声音设置页隐藏了这个模型没有接入的 Pitch 与 SSML 控件。
