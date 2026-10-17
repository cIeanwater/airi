# 本地对话持久化与后续数据沉淀

状态：已采用

## 结论与范围

AIRI 已经自动保存本地对话。离线运行和本地模型推理也使用这条保存路径，不需要登录或启用长期记忆。

现有会话记录是原始对话的主数据。后续整理、分析或训练数据抽取复用会话导出，不再创建一套重复的聊天档案库。

本文根据源码和合成数据回归说明行为。此次检查没有读取个人对话正文。

## 数据分层

| 数据 | 保存位置 | 生命周期 |
| --- | --- | --- |
| 会话索引和完整消息 | 当前浏览器来源的 IndexedDB | 跨刷新、重启保留，直到删除或清除该浏览器配置 |
| 图片与录音 | 同一来源的独立 IndexedDB 附件库 | 至少一个会话引用时保留，最后一个引用删除后回收 |
| 尚未结束的模型输出 | 当前渲染器内存中的流状态 | 正常完成、取消或传输失败时转成会话消息，进程突然退出前的未完成部分没有保存保证 |
| 长期记忆证据 | 独立的本地记忆快照 | 按用户和角色授权、编辑、清空，与完整对话记录分开管理 |
| 已登录用户的云同步副本 | 已连接的 Hosted API / PostgreSQL | 使用持久化 outbox 重试，不能代替本地完整备份 |

## 本地数据库与目录

`packages/stage-ui/src/database/storage.ts` 使用 unstorage 的 IndexedDB 驱动。
当前驱动没有指定 `dbName` 和 `storeName`，因此使用 idb-keyval 的默认值：

| 项目 | 当前值 |
| --- | --- |
| 数据库 | `keyval-store` |
| 对象仓库 | `keyval` |
| 本地数据前缀 | `airi-local:` |
| 会话索引键 | `airi-local:chat:index:<userId>` |
| 会话记录键 | `airi-local:chat:sessions:<sessionId>` |
| 云同步 outbox 键 | `airi-local:chat:outbox:<userId>` |
| 云删除标记键 | `airi-local:chat:tombstones:<userId>` |
| 附件数据库 / 对象仓库 | `airi-chat-assets` / `assets` |

表中是驱动规范化后的物理键。仓库源码中的 `local:chat/index/...` 路径会把斜杠转换成冒号，并去掉挂载前缀。

索引按 `userId` 和 `characterId` 分组。未登录用户使用 `local`，没有显式角色 ID 时使用 `default`。
切换角色或创建新会话不会删除旧会话。历史列表可以重新加载同一用户和角色的旧会话。

桌面版把这些数据库放在 Electron 的 `userData` 配置目录中。主进程支持通过 `APP_USER_DATA_PATH` 指定目录。
当前仓库的 `scripts/start-local-airi.ps1` 使用：

```text
D:\coding\humanning\.tools\airi-stage1-desktop-profile
```

Chromium 在该配置目录中管理 IndexedDB 文件。这些文件不是独立的聊天 JSON 文件，不应逐条直接修改。
没有使用该脚本时，实际目录以 Electron 的 `app.getPath('userData')` 为准。

数据库还按页面来源隔离。开发版使用 `ELECTRON_RENDERER_URL`，当前本地部署说明中的来源是 `http://localhost:5173`。
`localhost`、`127.0.0.1`、不同端口、HTTP/HTTPS、开发版与打包版的 `file:` 页面不能视为同一份浏览器数据。
同一个 profile 下更换来源也能出现空历史。部署时固定 profile 和来源，迁移时使用显式导出和导入。

## 消息结构与保存时机

会话记录由 `meta` 和 `messages` 组成。`meta` 保留会话 ID、用户 ID、角色 ID、标题、创建时间和更新时间。
还可以包含云会话 ID、云同步序号和语音打断控制事件。

每条消息保留角色、内容、消息 ID、创建时间，以及可用时的回复目标、完成时间和工具选择。
助手消息还可以保留文本与工具切片、工具结果、引用、推理分类和生成过程记录。
录音转写与图片描述附在对应消息上，后续轮次可以复用。

图片和录音在消息中使用附件引用，实际二进制数据存入附件库。分叉和导入的会话共享附件所有权，删除一个会话不会删除另一个会话仍引用的附件。

保存流程如下：

1. 用户发送消息后，`commitUserMessage` 先保存消息和会话索引，再返回用户消息接收回执。
2. 模型生成期间，界面显示内存中的流输出。
3. 正常结束后，运行时等待助手消息的持久化完成，再调用完成钩子并报告整轮成功。
4. 取消或传输失败时，已经收到的助手输出以 `interrupted: true` 保存。存储失败会报告错误。
5. 存储失败不会删除此前已保存的用户原文，也不会把本轮报告成成功。

用户消息回执只证明用户输入已落入本地存储，不代表助手已经生成或保存完成。
应用强制退出、操作系统终止进程或磁盘故障时，仍不能保证保留正在生成的所有字符。

本次修复消除了两个真实缺口：助手消息原来以异步后台任务保存，整轮完成信号能早于存储完成；导出原来能优先读取旧的磁盘快照。
现在助手追加返回存储 Promise，运行时等待该 Promise。导出等待已有保存任务，并优先使用当前已加载的完整历史。
导出还在异步读取前后检查账号代次，账号切换会终止本次导出。
助手追加保留即时显示，但回执还必须通过账号代次、用户和角色、会话代次及消息身份检查。排队期间切换账号不能让被跳过的保存任务报告成功。

## 导出、导入与备份

入口是 **设置 → 数据 → 聊天记录**。现有导出格式为：

```json
{
  "format": "chat-sessions-index:v1",
  "index": {
    "userId": "local",
    "characters": {}
  },
  "sessions": {}
}
```

导出包含当前用户的各角色会话。图片和录音会内联成可搬移的数据，备份不依赖原设备上的附件引用。
导出不包含正在生成、尚未转成会话消息的流片段。导出前等待当前回复结束。

这份 JSON 已经能作为后续数据沉淀的输入。它保留会话边界、用户和角色、消息顺序、时间、原文及附件。
后续训练或评测抽取可以离线筛选角色、排除错误与中断回复，再按需要生成 JSONL。当前不增加新的 JSONL 导出入口。

导入保留现有 `chat-sessions-index:v1` 格式，替换导入索引及对应会话，不是自动合并两份档案。
当前导入入口只做有限结构检查。迁移前先备份目标数据，并保留导出的用户作用域。

备份步骤：

1. 等待当前回复结束，然后导出聊天 JSON。
2. 把导出文件复制到应用目录之外的备份位置。
3. 如果需要整个配置的副本，退出 AIRI 后再复制完整 `userData` 目录。
4. 不要把真实聊天导出、原始对话或浏览器 profile 作为源码提交。

导出文件与本地 IndexedDB 都没有应用层加密。删除应用中的数据不会删除已经复制出去的备份。

## 删除与长期记忆的关系

删除单条消息会移除对应会话中的消息，并回收失去引用的附件。删除会话会移除整个会话及其附件所有权。
已登录用户删除单个会话时，还会保存云删除标记并重试远程删除，防止离线删除后被重新同步回来。

当前“清空聊天记录”批量路径只清理当前用户的本地会话，不等于删除 Hosted API 上的全部对话。
该路径没有复用单会话的云删除标记流程，登录用户仍存在远程会话再次同步回来的边界。它不能作为云端数据清除承诺。
Hosted API 的会话删除是软删除，不能解释成数据库物理擦除。

聊天记录与长期记忆不是同一份数据。启用长期记忆后，成功轮次的用户原文可以进入独立的记忆库，但助手回答不会自动成为记忆事实。
删除聊天记录不会自动删除这些记忆证据。长期记忆页面提供单独清理；应用的“删除全部数据”先撤销记忆授权并清理记忆，再执行其他数据清理。

## 云同步的边界

匿名 `local` 用户不会启动聊天云同步。已经登录且连接 Hosted API 时，合适的用户消息和已完成助手文本通过 outbox 同步到 PostgreSQL。
服务端使用会话成员权限、客户端稳定消息 ID 和递增序号处理写入与拉取。

当前云协议不能完整往返本地结构。系统提示、工具交换、错误、中断助手消息、录音用户消息及其回复保留在本地。
因此，账号登录和云同步不能代替包含附件的 JSON 备份。调用本地模型接口也不等于把完整会话档案保存进模型文件或模型服务日志。

## 主要源码边界

- `packages/stage-ui/src/database/storage.ts`：本地 IndexedDB 挂载。
- `packages/stage-ui/src/database/repos/chat-sessions.repo.ts`：会话、索引、outbox 和删除标记。
- `packages/stage-ui/src/database/repos/chat-assets.repo.ts`：附件与引用生命周期。
- `packages/stage-ui/src/stores/chat/session-store.ts`：作用域、保存队列、导入导出和会话管理。
- `packages/core-agent/src/runtime/chat-orchestrator-runtime.ts`：用户接收回执、助手落盘与完成信号。
- `packages/stage-ui/src/stores/modules/memory.ts`：独立的长期记忆接入。
- `apps/stage-tamagotchi/src/main/index.ts`：桌面配置目录选择。
- `server/apps/api/src/services/domain/chats.ts`：云端会话与消息写入。

## 验证记录

助手存储等待回归在修复前失败：存储 Promise 仍未完成时，整轮 `done` 已经完成。
导出等待回归在修复前失败：助手保存任务仍未完成时，导出已经返回。
修复后，核心编排测试与浏览器会话测试通过。测试使用合成消息和存储边界，不读取用户历史。

账号生命周期回归也先复现了失败：旧账号的追加任务没有写入，Promise 却返回了成功。
修复后，排队期间和写入期间切换账号都会拒绝过期回执，同时消息在当前有效会话中仍然即时显示。

| 定向检查 | 结果 |
| --- | --- |
| 核心聊天编排 | 49 项通过 |
| Stage UI 聊天契约 | 58 项通过 |
| 浏览器会话生命周期与多窗口 | 36 项通过，其中包含两项账号切换追加回归 |

核心编排命令从 `packages/core-agent` 运行：

```sh
node ../../node_modules/vitest/vitest.mjs run src/runtime/chat-orchestrator-runtime.test.ts
```

客户端命令从仓库根目录运行：

```sh
node node_modules/vitest/vitest.mjs run --config packages/stage-ui/vitest.config.ts --project node packages/stage-ui/src/stores/chat.contract.test.ts
node node_modules/vitest/vitest.mjs run --config .temp/memory-vitest-edge.config.ts --project browser packages/stage-ui/src/stores/chat/session-store-lifecycle.browser.test.ts packages/stage-ui/src/stores/chat/session-store.browser.test.ts
pnpm --config.verify-deps-before-run=false -F @proj-airi/stage-ui typecheck
```

本机浏览器命令使用已忽略的临时配置，保留仓库项目设置，只把 Playwright 的启动通道设为已安装的 Microsoft Edge。
