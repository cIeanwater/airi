# 2026-10-18 音频停顿检测 HTTP 400 修复

## 复现与原因

截图错误来自 Smart Turn 停顿检测接口，并非语音转写模型。

使用真实 Edge `OfflineAudioContext` 生成 48 kHz、单声道的纯合成音频。
将增益设为 `1.6`，Web Audio 会输出绝对值大于 `1` 的有效浮点采样。
原请求直接发送这些采样。当前 Smart Turn 接口要求归一化 PCM，因此返回：

```json
{
  "error": "Audio must contain finite normalized float32 samples"
}
```

使用峰值约 `1.4` 的独立合成音频，也在本地 `18085` 接口复现了相同的 HTTP 400。
此次验证未读取或保存麦克风音频。合成复现证明这条失败路径，未回读用户当时的音频。

## 修复

`packages/stage-ui/src/composables/voice-audio-end.ts` 在请求边界处理采样：

- 检查全部采样均为有限数值，拒绝 `NaN` 和无穷大。
- 当峰值超过 `1`，按峰值缩放请求副本，保留采样比例，避免削波。
- 保留原始采集缓冲区，不影响转写输入或后续处理。
- 请求不携带浏览器凭据。
- HTTP 失败时显示有长度限制的服务端错误原因，避免只剩状态码。

Smart Turn 模型和服务无需更换或重启。前端刷新后使用新请求逻辑。
接口失败时，现有 VAD 静音上限仍可结束输入，不会无限等待语义判断。

## 验证

修复前，浏览器回归确认高增益采样触发 HTTP 400。
修复后，6 项浏览器用例通过，包含真实 Web Audio 到当前本地 HTTP 服务的请求。
另外 8 项停顿检测插件回归通过，覆盖取消、八秒截断和静音上限。

```powershell
$env:VITEST_BROWSER_CHANNEL='msedge'
$env:VITE_SMART_TURN_TEST_ENDPOINT='http://127.0.0.1:18085/v1/turn'
pnpm --config.verify-deps-before-run=false exec vitest run --config packages/stage-ui/vitest.config.ts --project browser packages/stage-ui/src/composables/voice-audio-end.browser.test.ts
pnpm --config.verify-deps-before-run=false exec vitest run --config packages/stage-ui/vitest.config.ts --project node packages/stage-ui/src/libs/voice/voice-audio-end-plugin.test.ts
```

真实 HTTP 用例依赖运行中的本地 Smart Turn 服务。未设置测试地址时，该项明确跳过。
纯合成音频用于验证数据传输边界，不代表真实中文语音的语义停顿准确率。
