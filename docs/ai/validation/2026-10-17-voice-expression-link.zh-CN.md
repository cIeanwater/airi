# 语音回复与表情联动验收

日期：2026-10-17（Asia/Shanghai）。

## 问题与修复

模型实际会输出状态标记。标记用于舞台控制，聊天正文会隐藏这些标记。
本地消息 JSON 的 `response.choices[].message.content` 保留这些原始标记。
完整目录与查看命令见[消息记录说明](../local-message-json.zh-CN.md)。

合成会话的首次回复包含 `happy`、`curious`，以及不存在的动作 `widen eyes`、`peek`。
旧逻辑将任意 `motion` 字符串当作模型动作组，导致身体动作请求无效。

现在只有当前模型已加载的动作组才能控制身体。
不存在的动作不再阻止情绪映射。
每次请求的运行时提示词也会提供已加载的动作组，要求模型省略不匹配的动作。
表情强度、语音播放与口型仍由各自的正常链路控制。

## 真实桌面应用验收

使用当前桌面应用、当前 Hiyori Pro 模型和本地 Qwen3.5-2B BF16。
测试建立独立合成会话，没有读取私人历史，当前会话选择保持原值。
语音使用已配置的本地语音服务。

应用最初处于隐藏状态，尺寸观察器没有投递尺寸，渲染缓冲区为零。
通过应用既有的主窗口显示入口恢复窗口，并完整重载同一路由后，舞台自然运行。
CSS 尺寸为约 `427.3 × 522`，画布缓冲区为 `855 × 1044`。
没有手动驱动 ticker，也没有直接写入 Cubism 参数。

真实模型输出的状态标记示例：

```text
<|ACT {"emotion":"happy","motion":"Flick"}|>
```

本次使用的 `Flick` 和 `Idle` 都属于已加载的动作组。
正常帧循环中的 216 个采样具有不同的帧时间。
情绪标记执行后，实际 Cubism 参数如下：

| 参数 | 观测值 |
| --- | --- |
| `ParamEyeLSmile` | `1` |
| `ParamEyeRSmile` | `1` |
| `ParamEyeLOpen` | 约 `0.65` |
| `ParamCheek` | 约 `0.4` |
| `ParamMouthForm` | `1` |

语音播放期间有 85 个采样处于发声状态，`ParamMouthOpenY` 最高约为 `0.697`。
回复的语音队列最终完全排空。

另一个回归直接发送带未知动作的合成 ACT 事件。
身体动作保持为有效的 `Idle`，`happy` 强度 `0.8` 仍使笑眼为约 `0.8`、腮红为约 `0.32`。
结束后发送 `neutral` 释放测试表情。

完整消息、采样数组和实际用户配置保留在本地，不提交到仓库。

## 自动检查

```powershell
pnpm --config.verify-deps-before-run=false -F @proj-airi/stage-ui exec vitest run --project node src/composables/use-airi-runtime-prompt.test.ts
pnpm --config.verify-deps-before-run=false -F @proj-airi/stage-ui typecheck
pnpm --config.verify-deps-before-run=false exec eslint packages/stage-ui/src/components/scenes/Stage.vue packages/stage-ui/src/composables/use-airi-runtime-prompt.ts packages/stage-ui/src/composables/use-airi-runtime-prompt.test.ts
```

结果：单测 `3/3` 通过，工作区类型检查通过，定向 lint 通过。
