# Gemma、流式语音与消息上下文验收

日期：2026-10-18。环境：Windows、RTX 5070 12GB、本地 Electron 源码桌面。

本文为本轮初始验收。后续已调整为 64K，并增加声音选择与精简消息视图，见[后续验收](2026-10-18-voice-context-tuning.zh-CN.md)。

## 当前配置

| 作用 | 模型与运行方式 |
| --- | --- |
| 转写 | Zipformer small 中英双语 INT8，CPU，HTTP 18086、WebSocket 18087 |
| 说话结束 | Smart Turn v3.2，CPU，18085 |
| 对话 | Gemma 4 E2B 官方 QAT Q4_0，Vulkan GPU，16K 上下文、Q8 K/V 缓存 |
| 模型接口 | 消息记录网关 18083，原生 llama.cpp 18088 |
| 语音 | GPT-SoVITS v2ProPlus，`student-bright`，18084，32kHz 单声道 PCM |

GPT 与 SoVITS 合成核心共有 175,944,834 个参数，使用 FP16 CUDA。
BERT、HuBERT、多音字与说话人特征使用 CPU，固定参考特征缓存复用。
CPU 辅助模型仍占内存，服务工作集约 5.3GiB。
Gemma 与语音模型均已保存到全局设置及当前默认角色卡。
Gemma 当前只接收文本，音频由独立转写服务处理。

## 正式入口与实际桌面

从 Windows Machine 与 User PATH 启动 `Start-Humanning.cmd`，移除调试终端的 `NODE_PATH` 与 `ELECTRON_RUN_AS_NODE`。
入口依次启动全部服务与原桌面 profile，退出码为 0，约 54.10 秒完成。
语音模型加载与公开测试句预热约 22.14 秒，完成后才报告 ready。
网关预热约 0.118 秒，随后单独重启为 0.036 秒。
本机已有模型、磁盘和驱动缓存，这不是全新机器的安装耗时。

在独立合成会话发送“用一句话邀请我放学后一起去逛书店。”，未使用真实录音或私人历史。

| 观测 | 结果 |
| --- | --- |
| 首段正文 | 1.056 秒 |
| 文本生成结束 | 1.135 秒 |
| 实际播放开始 | 2.091 秒 |
| 播放结束 | 5.292 秒，状态 `ended` |
| 原始状态词 | `<\|ACT {"emotion":"happy"}\|>` |
| 语音结束后 | 无活跃回合，AudioContext 为 `running`，无语音错误 |

结果来自真实聊天入口、模型网关、语音服务及桌面 WebAudio 播放链路。
时间从文字提交开始，不包含用户说话、停顿判断和 ASR，也不保证所有问题都相同。
播放器回调证明浏览器音频播放正常。扬声器现场听感仍需用户试听。

语音独立热请求首 PCM 为 0.520–1.042 秒，RTF 为 0.265–0.335。
这些数字排除文本生成与成句等待，详见[声音研究](../research/2026-10-18-compact-student-voice.zh-CN.md)。
本轮实际整卡占用观测为 5477MiB / 12227MiB，包含桌面及其他负载。
该快照不表示语音进程的独立显存或更长上下文的峰值。

## 说话打断与取消

持续语音默认确认 96ms，再发出打断信号。
此确认不再要求浏览器报告 AEC 已启用，输入预滚动保留首字音频。
浏览器集成测试使用真实 Vue、Pinia、VoiceController 与 WebAudio，向 VAD 输入合成 PCM。
测试覆盖生成期和播放期：取消模型、TTS、排队 PCM，拒绝迟到 token，下一段语音继续自动提交。

真实桌面另验证同一 `speech-start` 信号的下游路径：

- 播放中打断，播放器约 102ms 后返回 `stopped`，无残留活跃回合。
- 生成中收到首段正文后打断，约 16ms 完成控制流程，网关记录为 `canceled`。
- 取消后没有继续恢复旧输出，后续新请求成功。
- 独立 TTS 首包前、首包后取消，锁释放约 96ms 与 24ms。

桌面这两次使用程序触发信号，VAD 检测由浏览器合成 PCM 测试覆盖。
本轮没有采集现场麦克风，也没有验收扬声器回声环境。
检测到三个输入设备，用户原有麦克风开关为关闭，本轮保留该设置。
使用时点击桌面麦克风按钮开启监听，自动发送与 `always-on` 模式已保留。

## 重复角色错例

原 Qwen 检查点结束符与聊天结束符不同，已同时配置并添加跨块协议边界检测。
不会按普通 `user`、`assistant` 单词或时间引用截断内容。
原始解码文本与处理后的输出分别保存，旧历史不自动删除。

新 Gemma 桌面使用截图中的同一句输入“你妈的”复测。
实际回复：“咦？刚才那句话是想跟我说点什么吗？是不是有什么事不开心呀？”
模型正常以 `stop` 结束，没有追加伪造角色或时间戳。
这是一条具体回归样例，不代表所有人格表达都已达到主观要求。

## 完整上下文菜单

实际浮动聊天窗口右键菜单包含“查看完整上下文”。
点击后显示约 31KB 的合成记录 JSON，包含实际请求、真实渲染提示与原始模型输出。
记录的会话、回合、助手消息 ID 与所选消息一致，匹配方式为 `id`。
已检查滚动区、复制和下载入口的布局，切换会话后窗口关闭。
旧记录仅能按正文和时间唯一匹配时，会明确标注候选匹配。
没有记录时返回不可用，不重建假的历史请求。

人格与记忆使用规则已在真实请求中确认生效。
直接修改入口见[上下文与人格说明](../companion-context-editing.zh-CN.md)。
所有测试会话结束后删除，原会话恢复，原聊天与账号设置保留。
JSON 与试听仅存于 Git 外的父目录 `.tools`，没有上传私人消息、模型或声音文件。

## 自动检查

| 命令或范围 | 结果 |
| --- | --- |
| `pnpm typecheck` | 55/55 工作区任务通过 |
| `pnpm lint` | 0 错误，655 项现有警告 |
| 定向 stage-ui Node 回归 | 15 文件，168/168 |
| 定向 stage-ui Edge 浏览器回归 | 4 文件，66/66 |
| core-agent 对话运行时测试 | 50/50 |
| `python -B scripts/test-local-voice-launcher.py -v` | 18/18 |
| `python -B scripts/test-local-chat-output.py -v` | 12/12 |
| `python -B scripts/test-local-message-context.py -v` | 14/14 |
| `python -B scripts/test-local-gemma-gateway.py -v` | 16/16 |
| `python -B scripts/test-local-gpt-sovits.py -v` | 5/5 |
| 启动器 `--check` | 38 个本机路径通过 |

pnpm 命令带有 `--config.verify-deps-before-run=false`，避免测试时触发重新安装。
浏览器测试使用 `VITEST_BROWSER_CHANNEL=msedge`。
范围包括上下文查询、对话拼接、角色设置、语音状态、PCM 协议和历史消息显示。
日志中的已有 ResizeObserver 循环诊断没有造成断言失败。
原消息记录器另有 6 项回归通过，相关 Python 文件编译检查通过。

本机元数据与试听位于父目录 `.tools/expressive-voice-2026-10-18`。
关键报告为 `desktop-streaming-eval.json`、`desktop-generation-cancel.json`、`desktop-interruption-eval.json` 与 `desktop-context-dialog.json`。
