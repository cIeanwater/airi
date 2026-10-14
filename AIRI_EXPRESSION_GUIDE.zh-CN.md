# AIRI 表情、状态与动作指南

更新日期：2026-10-14。

本指南以本仓库的实际代码和模型资源为依据。模型切换后，可用参数和动作会变化。

## 1. 情绪如何变成人物反应

当前人物支持九种情绪：`happy`、`sad`、`angry`、`think`、`surprised`、`awkward`、`question`、`curious`、`neutral`。

聊天模型通过 ACT 控制标记发出情绪。例如：

```text
<|ACT {"emotion":{"name":"happy","intensity":0.7}}|>
今天见到你很开心。
```

`intensity` 表示强度，范围是 `0` 到 `1`。省略强度时使用 `1`。情绪名会转换为小写。

Live2D 还接受模型内的动作组名称。例如，内置 Hiyori Pro 有 `Tap` 动作组：

```text
<|ACT {"motion":"Tap","emotion":{"name":"happy","intensity":0.7}}|>
```

这里的 `Tap` 必须存在于当前模型。任意写入 `wave` 或 `dance` 不会生成对应动作。

链路如下：

```text
聊天输出 ACT
  → streaming-control 解析和归一化
  → Stage 分发情绪和动作
  → Live2D / VRM / 其他渲染器
  → 当前模型已有参数、表情或动作资产
```

“思考中”气泡属于界面状态。它不是模型表情。聊天开始时，Stage 还会给 Live2D 设置 `think` 表情。

关键代码：

- [ACT 解析](packages/pipelines-audio/src/llm-streaming-control/parsers/act.ts)
- [情绪名、强度与动作归一化](packages/pipelines-audio/src/llm-streaming-control/payloads.ts)
- [Stage 分发](packages/stage-ui/src/components/scenes/Stage.vue)
- [聊天控制插件](packages/stage-ui/src/stores/ai/chat-llm/streaming-control.ts)
- [界面状态气泡](packages/stage-ui/src/stores/presence-bubble.ts)

## 2. 当前逻辑映射

下表中的名称属于 AIRI 约定，并不表示每个模型都包含这些动作。显式 ACT 动作必须使用模型中的实际组名。

| 情绪 | 中文含义 | Live2D 约定动作组 | VRM 表情状态 |
| --- | --- | --- | --- |
| `happy` | 开心 | `Happy` | `happy` |
| `sad` | 难过 | `Sad` | `sad` |
| `angry` | 生气 | `Angry` | `angry` |
| `think` | 思考 | `Think` | `think`，缺失时使用 `relaxed` |
| `surprised` | 惊讶 | `Surprise` | `surprised` |
| `awkward` | 尴尬 | `Awkward` | `neutral` |
| `question` | 疑惑 | `Question` | `think`，缺失时使用 `relaxed` |
| `curious` | 好奇 | `Curious` | `think`，缺失时使用 `relaxed` |
| `neutral` | 平静 | `Idle` | `neutral` |

映射的入口是 [stage-ui 的 emotions.ts](packages/stage-ui/src/constants/emotions.ts)。

### Live2D 的表情与动作选择

聊天情绪会优先使用模型的同名 `.exp3.json` 表情组。匹配忽略大小写。例如，`happy` 匹配 `Happy`，`surprised` 也匹配 `Surprise`。

如果模型没有对应表情组，渲染器组合实际存在的 Cubism 参数。它不会给模型中不存在的参数制造虚拟表情。

合成表情的满强度目标如下。未列出的参数保持自身控制逻辑。

| 情绪 | Cubism 参数目标 |
| --- | --- |
| `happy` | 嘴形 `1`，左右笑眼 `1`，左右睁眼 `0.65`，脸红意图 `0.4` |
| `sad` | 嘴形 `-0.7`，左右眉高 `-0.4`，左右眉形 `0.8`，左右睁眼 `0.65` |
| `angry` | 嘴形 `-1`，左右眉角 `-0.7`，左右眉形 `-1` |
| `think` | 嘴形 `-0.15`，左眉高 `0.2`，右眉高 `-0.2`，左右睁眼 `0.75` |
| `surprised` | 嘴形 `-0.2`，左右眉高 `0.6`，左右睁眼 `1` |
| `awkward` | 嘴形 `0.2`，脸红意图 `0.6`，左眉高 `0.3`，右眉高 `-0.3` |
| `question` | 嘴形 `-0.2`，左眉高 `0.6`，右眉高 `-0.2` |
| `curious` | 嘴形 `0.3`，左右眉高 `0.4` |
| `neutral` | 释放聊天表情，恢复模型默认值和原有控制器 |

满强度目标定义在 [Live2D emotion.ts](packages/stage-ui-live2d/src/composables/live2d/emotion.ts)。强度从模型默认值向目标值插值，结果限制在真实参数范围内。

脸红参数会适配模型的方向。Hiyori Free 的 `ParamCheek` 范围是 `[-1, 0]`，默认值是 `0`。因此，`happy` 的脸红目标是 `-0.4`，强度 `0.3` 时为 `-0.12`。Hiyori Pro 则采用正向目标。模型自带 `.exp3.json` 的数值不会做这个方向转换。

聊天表情持续约三秒。收到 `neutral` 或强度 `0` 时立即释放。它不改写用户保存的默认值。

释放时恢复的是模型源文件的默认参数。这些值可以与应用启动时的手动参数不同。恢复不表示画面与首次截图逐像素一致。

聊天表情独立于设置中的“表情覆盖”开关。关闭手动覆盖时，聊天情绪仍会显示。

动作选择先检查显式文件映射，再检查模型中同名动作组。这两种自动匹配忽略大小写。显式 ACT 的 `motion` 仍直接使用实际组名。

找不到动作时，渲染器仍显示表情，不请求不存在的组。

ACT 同时给出 `motion` 和 `emotion` 时，两者都执行。显式动作优先控制身体，情绪叠加到脸部。

VRM 表情状态会组合模型的 morph 权重。例如，`happy` 使用 `happy=0.7` 和 `aa=0.2`。实际权重还要乘以 `intensity`。

VRM 会渐变到目标表情，并在约三秒后回到 `neutral`。说话时，口型控制优先使用元音 morph。眨眼和动画也有独立控制器。

VRM 组合定义在 [VRM expression.ts](packages/stage-ui-three/src/composables/vrm/expression.ts)。VRM 调用入口在 [VRMModel.vue](packages/stage-ui-three/src/components/Model/VRMModel.vue)。

## 3. 内置模型实际有什么

默认人物模型是 `preset-live2d-1`，也就是 `Hiyori (Pro)`。模型清单在 [display-models.ts](packages/stage-ui/src/stores/display-models.ts)，默认选择在 [stage-model.ts](packages/stage-ui/src/stores/settings/stage-model.ts)。

### Live2D：Hiyori

以下内容来自本机实际 ZIP 中的 `.model3.json`，不是根据名称推测的。

| 模型 | 实际动作组 | 动作文件总数 | `FileReferences.Expressions` |
| --- | --- | --- | --- |
| Hiyori Pro | `Idle` × 3、`Flick`、`FlickDown`、`FlickUp`、`Tap` × 2、`Tap@Body`、`Flick@Body` | 10 | 未配置 |
| Hiyori Free | `Idle` × 3、`Flick`、`FlickDown`、`Tap`、`Tap@Body`、`Flick@Body` | 8 | 未配置 |

内置 Hiyori 没有 `Happy`、`Sad`、`Angry`、`Think` 这些动作组。它也没有预先注册的 `.exp3.json` 表情。

当前修复通过现有参数生成聊天表情，所以它不再依赖这些缺失的动作组。它仍不能凭空增加挥手等肢体动作。

Hiyori 的 `.cdi3.json` 已列出这些可用参数：

| 用途 | 参数 ID |
| --- | --- |
| 笑眼 | `ParamEyeLSmile`、`ParamEyeRSmile` |
| 睁眼、闭眼 | `ParamEyeLOpen`、`ParamEyeROpen` |
| 眼睛看向 | `ParamEyeBallX`、`ParamEyeBallY` |
| 嘴部形状与开合 | `ParamMouthForm`、`ParamMouthOpenY` |
| 脸红 | `ParamCheek` |
| 眉毛形状 | `ParamBrowLForm`、`ParamBrowRForm` |
| 头部角度 | `ParamAngleX`、`ParamAngleY`、`ParamAngleZ` |
| 身体角度 | `ParamBodyAngleX`、`ParamBodyAngleY`、`ParamBodyAngleZ` |
| 呼吸 | `ParamBreath` |

Pro 还有更多眉毛、肩、腿、手臂和手部参数。Free 的参数较少。数值范围以实际模型为准。

资源位置：

```text
packages/stage-ui/src/assets/live2d/models/hiyori_pro_zh.zip
  hiyori_pro_zh/runtime/hiyori_pro_t11.model3.json
  hiyori_pro_zh/runtime/hiyori_pro_t11.cdi3.json

packages/stage-ui/src/assets/live2d/models/hiyori_free_zh.zip
  hiyori_free_zh/runtime/hiyori_free_t08.model3.json
  hiyori_free_zh/runtime/hiyori_free_t08.cdi3.json
```

### VRM：AvatarSample_A / AvatarSample_B

两份内置资源都是 VRM 0.x。原始 blend shape 名称如下：

```text
Neutral, A, I, U, E, O,
Blink, Blink_L, Blink_R,
Angry, Fun, Joy, Sorrow, Surprised
```

three-vrm 加载 VRM 0.x 时会转换标准预设名称。自定义名称仍取决于模型。因此，运行时清单比原始文件名更准确。

原始资源没有 `Think` blend shape。当前代码会把缺失的 `think` 回退到已有的 `relaxed`。这是一种替代表情。

## 4. 如何增加 Live2D 表情

### 方法 A：组合已有参数

这个方法适合微笑、脸红、闭眼或眉毛变化。它不需要重新绘制模型。

1. 打开“设置 → 模型”。
2. 选择 Live2D 模型。
3. 检查模型参数和已有表情组。
4. 记录目标表情的参数 ID、数值和范围。
5. 为这些数值建立表情预设。

参数控制位于 [Live2D 模型设置](packages/stage-ui/src/components/scenarios/settings/model-settings/live2d.vue)。

例如，可以为 Hiyori Free 增加 `Happy.exp3.json`：

```json
{
  "Type": "Live2D Expression",
  "FadeInTime": 0.3,
  "FadeOutTime": 0.3,
  "Parameters": [
    { "Id": "ParamEyeLSmile", "Value": 0.8, "Blend": "Overwrite" },
    { "Id": "ParamEyeRSmile", "Value": 0.8, "Blend": "Overwrite" },
    { "Id": "ParamMouthForm", "Value": 0.6, "Blend": "Overwrite" },
    { "Id": "ParamCheek", "Value": -0.3, "Blend": "Overwrite" }
  ]
}
```

这是 Hiyori Free 的初始数值。它需要在实际模型上预览和调整。Hiyori Pro 的脸红值采用正方向。

随后，在原 `.model3.json` 的 `FileReferences` 中增加表情引用：

```json
{
  "Expressions": [
    { "Name": "Happy", "File": "expressions/Happy.exp3.json" }
  ]
}
```

保留原模型的其他 `FileReferences`。将新表情文件放在对应相对路径。重新打包模型 ZIP 后，通过模型选择器导入。

只把文件放进 ZIP 不够。模型清单必须引用它。

加载逻辑在 [expression-controller.ts](packages/stage-ui-live2d/src/composables/live2d/expression-controller.ts)。预览、默认值和恢复逻辑在 [expression-store.ts](packages/stage-ui-live2d/src/stores/expression-store.ts)。

### 方法 B：使用 Cubism 制作表情资产

1. 打开 Live2D Cubism 的模型源文件。
2. 在 Animation View 中组合参数，制作表情。
3. 导出表情用 `.motion3.json`。
4. 在 Cubism Viewer 中导入这些文件。
5. 调整表情参数和混合方式。
6. 导出 `.exp3.json`。
7. 将表情文件加入模型包和 `FileReferences.Expressions`。

官方流程说明见 [Live2D：Expression Settings and Export](https://docs.live2d.com/en/cubism-editor-manual/setting-and-exporting-facial-expressions/)。眼睛开合使用乘法混合时，需要检查眨眼效果。

如果目标是增加眼泪、全新嘴形或新的手部姿势，而原模型没有对应参数，需要增加图层、网格和变形器。这个工作需要可编辑的模型源文件。

### 现有 LLM 表情工具的边界

[expression-tools.ts](packages/stage-ui-live2d/src/tools/expression-tools.ts) 已定义：

```text
expression_get
expression_set
expression_toggle
expression_save_defaults
expression_reset_all
```

这些函数可以控制已注册的 Live2D 表情或参数。当前聊天工具链没有注册这组工具。设置页也把“向 LLM 暴露表情”标为开发中。

因此，勾选“全部暴露”不等于聊天模型已经可以调用它们。接入时需要注册工具、传入实际表情清单，并执行暴露范围检查。

当前 `set(groupName, value)` 会给组内每个参数设置同一数值。它不是按 `.exp3.json` 每个参数的目标值缩放。需要按目标值启用整组时，可以使用 `toggle`，或逐个设置参数。

## 5. 如何增加 Live2D 动作

### 内置动作编辑器

桌面版入口是“设置 → 系统 → 开发者工具 → Live2D 动作”，路由是 `/devtools/live2d-motion`。

1. 选择 Live2D 模型。
2. 打开动作工具。
3. 用摇杆或手柄控制头部、身体、眼睛和嘴部。
4. 录制动作。
5. 在时间轴增加或调整关键帧。
6. 播放动作，检查模型反应。
7. 导出动作项目 JSON。

编辑器支持 `add` 和 `replace` 轨道叠加。它还支持裁剪、导入、导出和撤销。

动作项目格式是 `airi-live2d-motion-project/v1`。其中的录制数据格式是 `airi-live2d-motion/v6`。

这两种格式属于 AIRI。导出文件不能直接作为 Cubism `.motion3.json` 使用，也不会自动增加 ACT 动作组。

关键代码：

- [桌面动作工具入口](apps/stage-tamagotchi/src/renderer/pages/devtools/live2d-motion.vue)
- [动作录制与回放](packages/stage-ui/src/features/devtools/motion/live2d/composables/recording.ts)
- [动作项目与关键帧](packages/stage-ui/src/features/devtools/motion/live2d/composables/keyframes.ts)
- [编辑器导入、导出](packages/stage-ui/src/features/devtools/motion/live2d/components/keyframe-editor.vue)

### MAGIC 生成连续动作

模型设置中的动作驱动可以选择 `magic`。它从参考数据拟合 VAR 或 AR-HMM，再生成连续的姿态变化。

内置参考数据是 `idle-calm` 和 `speaking-excited`。定义在 [profiles.ts](packages/stage-ui/src/features/motions/live2d/profiles.ts)。

动作工具里的 Procedural Motion 可以用录制数据生成变化。这个能力适合自然晃动和持续姿态。它没有把“挥手”这样的文字变成新骨骼动作的语义接口。

### 增加聊天能触发的正式动作

1. 在 Cubism 中制作动作时间线。
2. 导出 `.motion3.json`。
3. 把文件加入模型包。
4. 在 `.model3.json` 的 `FileReferences.Motions` 中注册动作组。
5. 导入更新后的模型包。
6. 用 ACT 的 `motion` 指定该动作组。

例如，在保留原动作组的前提下增加：

```json
{
  "Motions": {
    "Wave": [
      { "File": "motions/Wave.motion3.json" }
    ]
  }
}
```

对应控制标记是：

```text
<|ACT {"motion":"Wave"}|>
```

如果要让 `happy` 自动播放这个动作，需要把 `happy` 的映射改为 `Wave`，或提供 `Happy` 动作组。

原设置页的“动作映射编辑”整块界面处于注释中。不能把它当成已发布的图形映射编辑器。

开发代码可以把模型的现有动作文件绑定到情绪。例如，Hiyori Pro 的 `Tap` 第一个动作文件可以绑定到 `happy`：

```ts
import { useLive2dParams } from '@proj-airi/stage-ui-live2d/stores'

const live2d = useLive2dParams()
live2d.motionMap['motion/hiyori_m07.motion3.json'] = 'happy'
```

文件路径必须来自当前模型的 `availableMotions`。模型自己的 `.model3.json` 动作组也可以直接采用 `Happy` 等约定名称。

## 6. 如何增加 VRM 表情和动作

VRM 表情使用模型的 blend shape / morph。`useVRMEmote` 的 `addEmotionState()` 可以组合已有 morph，`removeEmotionState()` 可以删除组合。

例如，模型已有 `relaxed` 和 `happy` 时，可以组合一个柔和表情：

```ts
emote.addEmotionState('gentle', {
  expression: [
    { name: 'relaxed', value: 0.3 },
    { name: 'happy', value: 0.4 },
  ],
  blendDuration: 0.5,
})
```

这是开发接口示例。它不会自动让 ACT 接受新的 `gentle` 情绪。

如果目标 morph 不存在，先在模型制作工具中增加并导出 VRM。可以用 Blender 的 VRM Add-on 或 Unity 的 UniVRM 完成资产工作。

骨骼动作使用 `.vrma`。当前 Stage 的 VRM 使用内置 `idle_loop.vrma`，ACT 的 `motion` 只驱动 Live2D。

因此，VRM 新动作需要动作资产，也需要动作选择、播放和切换代码。只导入新 `.vrm` 不会增加聊天动作。

当前加载代码在 [VRM animation.ts](packages/stage-ui-three/src/composables/vrm/animation.ts)。内置动作入口在 [Stage.vue](packages/stage-ui/src/components/scenes/Stage.vue)。

官方格式说明见 [VRM Animation](https://vrm.dev/en/vrma/)。Blender 导出流程见 [Export VRM Animation](https://vrm-addon-for-blender.info/en-us/ui/export_scene.vrma/)。目标模型必须具备动画引用的自定义表情。

## 7. 我们能生成哪些内容

| 目标 | 可实施方式 | 交付内容 |
| --- | --- | --- |
| 用现有模型做微笑、脸红、闭眼 | 调整已有参数和强度 | 参数预设、`.exp3.json`、映射代码 |
| 做点头、摇头、自然晃动 | 录制姿态或编写关键帧 | AIRI 动作项目 JSON，或正式 `.motion3.json` |
| 做 VRM 的新表情组合 | 组合现有 morph | `EmotionState` 和映射代码 |
| 做挥手或新的肢体姿势 | 制作动作资产和播放接口 | `.motion3.json` / `.vrma` 与接入代码 |
| 做全新眼泪、嘴形或身体结构 | 修改模型源文件 | 新图层、变形器或 morph，加更新后的模型包 |
| 根据图片生成另一套静态表情 | 生成同尺寸角色图片 | `.tachie.zip` |

图片生成可以提供表情参考、纹理或静态立绘。单张 PNG 不能代替 Live2D 的 `.moc3`，也不能代替 VRM 骨架或动画。

如果只需要图片切换，本项目已经支持 Tachie。把 `neutral.png` 和可选的 `happy.png`、`sad.png` 等同尺寸图片打成 `character.tachie.zip` 即可。必须包含 `neutral`。

Tachie 缺少的情绪会回到 `neutral`。它适合静态表情，不提供骨骼动作或口型。格式见 [Tachie README](packages/stage-ui-tachie/README.md)。

## 8. 增加新的情绪类别时改哪里

新增资产不一定需要新增情绪类别。例如，“害羞”可以先作为 `awkward` 的表情预设。

如果要让聊天直接输出一个新类别，需要同步这些位置：

1. 在 [stage-ui 情绪枚举](packages/stage-ui/src/constants/emotions.ts) 增加名称和映射。
2. 在 [ACT 归一化](packages/pipelines-audio/src/llm-streaming-control/payloads.ts) 增加名称。
3. 在 [Stage 分发](packages/stage-ui/src/components/scenes/Stage.vue) 接收这个名称。
4. 在使用的渲染器中增加对应表情或动作。
5. 给聊天提示词提供这个名称和实际资产清单。
6. 检查流式输出、静音聊天、模型切换和自动恢复。

各渲染器还保留自己的情绪类型。需要支持其他渲染器时，同步它们的 `constants/emotions.ts`。

不要只改枚举。ACT 归一化会丢弃未知情绪，模型也不会生成未制作的资产。

## 9. 检查新增效果

1. 导入模型，检查模型报告中的表情和动作引用。
2. 在模型设置中手动预览目标表情或动作。
3. 发送强度为 `0.3`、`0.7`、`1` 的 ACT。
4. 检查人物反应是否随强度变化。
5. 检查说话口型和眨眼是否正常。
6. 检查 `neutral` 或自动恢复是否清除上一次表情。
7. 检查静音或未配置 TTS 时是否仍有情绪反应。

浏览器控制台的 `emotion detected` 只说明 Stage 收到了情绪。检查画面和模型参数，才能确定人物执行了反应。
