# 本地轻量语音与免登录验收

日期：2026-10-17。Windows，本地 Electron、Edge、RTX 5070 12 GB。

## 当前配置

| 边界 | 实际配置 |
| --- | --- |
| 输入 | CPU Zipformer small 中英双语，INT8 encoder/joiner、FP32 decoder，60.14 MB |
| 输入协议 | `18086` 文件兼容接口，`18087/v1/stream` 原生 PCM16 流 |
| 结束判断 | Smart Turn v3.2 CPU，600 ms 停顿后判断；本机最多等 2 秒静音 |
| 转写改写 | 关闭，避免每轮额外调用聊天模型 |
| 对话 | 原 Qwen3.5-2B BF16，`18083`，未改成量化参数 |
| 输出 | MOSS-TTS-Nano ONNX CPU，`18084`，`xiaoyu`，48 kHz 双声道 PCM16 流 |
| 身份 | `local`；默认不登录、不访问账号同步与官方托管 Provider |

模型来源、固定 SHA、质量样本与取舍见[研究记录](../research/2026-10-17-fast-local-voice.zh-CN.md)。

## 完整自动对话

测试为 `packages/testing-audio/cases/local-conversation/case.audio.web.test.ts`。
只有麦克风替换为仓库已知合成 WAV，其余使用实际 VAD、Smart Turn、原生 WS ASR、2B BF16、TTS 与浏览器播放。
测试没有点击发送，保留完整 PCM、最终转写、增量文字、用户/助手消息、非零语音时长和正常播放结束的断言。

最终一项用例通过：测试主体 33.26 秒，总时长 34.30 秒。
此前 1.7B 语音方案的对应测试为主体 66.02 秒、总时长 67.82 秒。
输入包含 14 秒预热静音，测试还等待整段语音播放完毕；总时长不能作为用户说完后的响应延迟。

最终合成会话的真实模型服务元数据：

| 项目 | 此前方案 | 本次 |
| --- | --- | --- |
| 输入 token | 4,050 | 1,566 |
| 输出 token | 128 | 34 |
| 模型请求计算时间 | 9.798 秒 | 2.036 秒 |

回复内容和长度不同，因此不能把时间差当作相同 token 数的吞吐提升。
改进来自默认角色提示精简、去掉重复 ACT 长说明、减少无用工具 schema、默认短句和不再运行转写改写。
本地模式保留显式工具、已注册工具、自定义工具和已配置搜索。自定义与导入角色卡不使用默认角色的精简投影。

原测试探针只识别 HTTP 上传和 Aliyun NLS。
现已补齐对严格回环 `/v1/stream` 的完整 PCM、partial、final 观测，使用真实 Edge/ASR 验证采集字节与全部发送内容相等。
没有通过删除录音或转写断言来绕过观测差异。

复跑前构建 Web，再设置：

```powershell
$env:TESTING_AUDIO_LOCAL_CONVERSATION = '1'
$env:TESTING_AUDIO_LOCAL_ASR_BASE_URL = 'http://127.0.0.1:18086/v1/'
$env:TESTING_AUDIO_LOCAL_ASR_API_KEY = 'local'
$env:TESTING_AUDIO_LOCAL_ASR_MODEL = 'zipformer-small-zh-en-int8'
$env:TESTING_AUDIO_LOCAL_ASR_STREAM_URL = 'ws://127.0.0.1:18087/v1/stream'
$env:TESTING_AUDIO_LOCAL_LLM_BASE_URL = 'http://127.0.0.1:18083/v1/'
$env:TESTING_AUDIO_LOCAL_TTS_BASE_URL = 'http://127.0.0.1:18084/v1/'
$env:TESTING_AUDIO_LOCAL_TTS_MODEL = 'moss-tts-nano-100m'
$env:TESTING_AUDIO_LOCAL_TTS_VOICE = 'xiaoyu'
$env:TESTING_AUDIO_LOCAL_TURN_ENDPOINT = 'http://127.0.0.1:18085/v1/turn'
pnpm --config.verify-deps-before-run=false -F @proj-airi/testing-audio exec vitest run --project audio-web cases/local-conversation/case.audio.web.test.ts
```

本机临时配置只把测试浏览器选择为已安装的 Edge。标准命令需要已安装的 Playwright Chromium。

## 原桌面与启动入口

最终桌面使用 `voice.startResponse → write/end → finishResponse` 播放已知原创中文。
首部采用小 PCM 块建立余量，随后恢复较大块，避免首包后等待太久而断续。

| 实际播放观测 | 结果 |
| --- | --- |
| 从提交文本到首个播放节点 | 273 ms |
| PCM 节点 / 正常 ended | 22 / 22 |
| 音频时长 | 5.36 秒 |
| 相邻节点最大调度间隙 | 浮点误差范围内为 0 ms |
| 结果 | `finished`，AudioContext `running` |

音频包含有声样本和正常静音块。原会话保持不变，Live2D 画布保持有效尺寸。
上述首播时间不包含 ASR、停顿判断或聊天模型耗时，不是完整响应时间。

实际原 profile 检查：默认角色的公开历史模板得到精确匹配，运行时精简提示长 406 字符。
角色卡、用户消息、助手消息与原选中会话保持不变。
配置为新 ASR、新 TTS、自动发送开启、改写关闭、Smart Turn 地址正确、两秒最长静音等待。

两个白色 V 浮标均来自 Vue DevTools 插件，分别注入主窗口和 Inlay。
默认不注入该插件，两个窗口已实测不再含 DevTools 容器。
需要调试插件时显式设置 `AIRI_VUE_DEVTOOLS=true` 后重启开发服务器。

已实际执行 `Start-Humanning.cmd`。
统一入口从全部服务停止的状态启动 ASR、Smart Turn、BF16 对话与 Nano 服务；再次运行时复用健康服务和原 profile。
跨进程启动锁验证并发双击不会重复加载模型，路径包含空格时也能启动隐藏子进程。

消息 JSON 仍开启，位于父目录 `.tools/airi-message-records`，保留七天内最新 1,000 份。
没有将真实消息、profile、令牌、模型权重或私人录音提交到 GitHub。

本次恢复检查时，Windows `waveInGetNumDevs` 与 Electron 枚举均返回 0 个录音设备。
启用麦克风得到 `Requested device not found`，应用会关闭无效输入。
因此本次不宣称真实麦克风现场通过；完整自动对话使用已知合成音频，实际输出在桌面播放队列中验证。
重新接通系统输入设备后，再打开应用麦克风开关。

## 工程检查

| 检查 | 结果 |
| --- | --- |
| 全仓类型检查 | 55/55 任务成功；最后工具/探针/Pocket 改动另过对应类型检查 |
| 全仓 lint | 0 errors、655 条既有 warnings；最后增量文件定向 lint 通过 |
| Web 与 Electron 最终构建 | 均成功 |
| 本地模式真实浏览器 | 9 项通过，含保留数据、禁止账号请求、禁止遥测 |
| 原生输入真实 Edge | 8 项通过 |
| 原生输入采集探针 | 真实 Edge/ASR 1 项通过 |
| 输入策略 Node | 20 项通过；强化续说重置计时的音尾文件 8 项通过 |
| 默认角色/历史模板 | 18 项通过，拒绝精简用户改过的默认卡 |
| 流式 PCM 传输 | 5 项通过，含分包、首块、取消、错误格式与截断 |
| 输出追踪 | 3 项通过，逐块透传且保留真实合成结束时刻 |
| 本地默认工具与显式工具保留 | 12 项通过 |
| TTS HTTP 协议 | 6 项真实 TCP 回归通过 |
| RevenueCat 时钟回归 | 15/15 通过，未修改生产账务逻辑 |

## GitHub 部署边界

两项历史部署失败都发生在 Cloudflare 发布阶段，原因是缺少 Cloudflare Token。
现在改为手动确认并显式启用，默认本地使用不再自动尝试部署账号站点。
正常 CI 保留，历史失败记录保留，没有把未执行的云部署描述成成功。
原始运行链接与可选云配置见[部署说明](../local-deployment-and-github-actions.zh-CN.md)。
