# 本地语音检测离线验收

用户报告：说话后，语音转写直接报错。

实际桌面错误是 `[voice] TypeError: Failed to fetch`。
错误发生在 Transformers.js 的 `AutoModel.from_pretrained`，本地 ASR 没有收到转写请求。
首次麦克风输入会从 Hugging Face 下载 Silero VAD，随后从 jsDelivr 加载 ONNX WASM。
这些下载依赖当前浏览器配置与缓存。上次独立测试浏览器通过，不能证明真实桌面首次使用也能通过。

本次修复把 2.24 MB 的 Silero 模型作为共享资产随应用打包。
模型版本、SHA256 和 MIT 许可保存在 `packages/stage-ui/src/assets/vad/`。
现用语音控制器与转写演示均使用同一加载入口。
加载入口只接受本地模型文件，没有模型仓库下载分支。

共享 Vite 插件从已安装的 `@huggingface/transformers` 发布目录获取匹配的 WASM 与模块。
插件用于 Web、桌面、移动端、组件预览及相关浏览器测试。
这避免了混用根目录中其他版本的 `onnxruntime-web`。
WASM 是构建产物，不复制到 Git 源码目录。

## 自动回归

测试文件：`packages/stage-ui/src/workers/vad/silero-vad.browser.test.ts`。
测试运行真实模型推理，关闭模型缓存，并通过 Playwright 拦截全部外网请求。

修复前，测试捕获到 `https://huggingface.co/onnx-community/silero-vad/resolve/main/onnx/model.onnx`，随后失败。
修复后，静音窗口产生有效语音概率，外网请求数为 0。
测试没有读取用户浏览器缓存，也没有读取或保存麦克风录音。

PowerShell 验收命令：

```powershell
$env:VITEST_BROWSER_CHANNEL = 'msedge'
pnpm --config.verify-deps-before-run=false -F @proj-airi/stage-ui exec vitest run src/workers/vad/silero-vad.browser.test.ts --project browser
```

结果：1 项通过，真实推理 394 ms。
默认测试浏览器仍为 Playwright Chromium。此环境使用已安装的 Edge。

```powershell
pnpm --config.verify-deps-before-run=false -F @proj-airi/stage-ui exec vitest run src/workers/vad/vad.test.ts src/stores/ai/models/vad.test.ts src/libs/voice/voice-activity-plugin.test.ts src/libs/voice/voice-rephrase-plugin.test.ts src/libs/voice/voice-end-judge-plugin.test.ts src/libs/audio/hearing-transcriber.test.ts src/stores/modules/hearing.test.ts --project node
```

结果：7 个文件、51 项测试通过。

本次修复不改变本地 ASR 模型、Qwen BF16 模型、语音改写或结束判断的选择。
它修复了转写之前的联网加载失败。

## 实际桌面与完整语音链路

已重启用户实际开发进程，保留原有 profile、会话选择与模型配置。
新增的 Vite 资源插件需要重启开发进程才能加载，单独刷新旧进程不足以启用它。

实际 Electron 中阻断 Hugging Face 与 jsDelivr 下载域名后，静音合成窗口推理通过，概率为 `0.0442626476`。
ONNX、MJS、WASM 三个请求均来自 `localhost:5173`，状态均为 200。
没有修改或清空用户的模型缓存。

正式产物另用全新 Electron profile 验证。
该测试关闭网络缓存并阻断全部 HTTP/HTTPS，三个资源均从 `file:` 页面加载，真实推理通过。
外网请求数为 0。分发目录中的 Silero 许可与源文件逐字节一致。
测试结束后，只关闭了该临时实例。

完整输入回归使用仓库已有合成 WAV：文件麦克风 → 真实 Silero VAD → 本地 Whisper → 语音草稿。
测试通过，耗时 26.51 秒。ASR 使用隔离的临时端口和凭据，结束后已清理。

```powershell
pnpm --config.verify-deps-before-run=false -F @proj-airi/testing-audio exec vitest run --config ../../.temp/audio-receive-check/vitest.edge.config.mjs --project audio-web cases/local-hearing/case.audio.web.test.ts
```

该命令沿用[音频验收](2026-10-16-audio-reception.zh-CN.md)中的本地 ASR 环境变量。
临时 Edge 配置只替换浏览器通道，不修改应用逻辑。

还用本机 Huihui 语音生成了中文合成 WAV，未录制真人声音。
在实际 AIRI 中使用原有 `8082`、`Systran/faster-whisper-small`，不覆盖语言设置，转写用时 3.40 秒。
结果命中“合成语音”“中文”“测试”。这验证中文请求能够完成，不代表真人识别准确率评测。

## 最终全仓检查

| 命令 | 结果 |
| --- | --- |
| `pnpm --config.verify-deps-before-run=false typecheck` | 55 个任务通过 |
| `pnpm --config.verify-deps-before-run=false lint` | 0 错误，655 条既有警告 |
| `pnpm --config.verify-deps-before-run=false -F @proj-airi/stage-tamagotchi build` | 通过，包含本地模型、运行库与 MIT 许可 |
| `pnpm --config.verify-deps-before-run=false -F @proj-airi/stage-web build` | 通过 |

完整模型消息的本地 JSON 查看入口见[消息记录说明](../local-message-json.zh-CN.md)。
