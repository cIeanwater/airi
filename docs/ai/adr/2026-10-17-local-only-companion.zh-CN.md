# 默认本地模式与桌面调试浮标

状态：已采用。日期：2026-10-17。

## 本地身份与数据

Humanning 默认使用本地模式。统一开关为 `VITE_AIRI_LOCAL_MODE`。
只有显式设置字符串 `false` 才启用连接账号的模式。

本地模式使用现有匿名用户 `local`，继续读取原有角色、聊天与记忆。
启动时不恢复远程令牌，不请求账号会话，不刷新令牌，也不弹出登录。
之前保存的凭据和账号作用域数据保留在原处，不会自动合并进匿名历史。

本地模式同时关闭以下入口与调用：

- 桌面账号按钮、网页账号菜单和移动设置中的账号行。
- `/auth`、`/settings/account` 和 `/settings/flux` 路由。
- 官方托管模型 Provider，以及 Hosted API 请求。
- 本地优先数据请求中的远程同步，即使调用方允许远程读取。
- 产品遥测采集。

设置、角色管理、聊天导出、长期记忆、本地工具与模型 Provider 配置继续可用。
本地模型端点由启动器和各 Provider 配置决定。
首次启动不强制展示账号向导。主动打开向导时，只显示本地模型配置入口。

服务端源码保留，供明确选择连接模式的开发者使用。
这些改动不删除用户数据，也不修改应用 ID 或浏览器配置目录。

## 两个白色 V 浮标

它们是 `vite-plugin-vue-devtools` 注入的开发面板开关。
角色窗口与语音 Inlay 各有一个渲染器，因此各显示一个浮标。
实际检查在两个页面中都发现了 `#__vue-devtools-container__`。

桌面与网页现在默认不加载该插件。
需要调试时，在启动开发服务前设置 `AIRI_VUE_DEVTOOLS=true`。
插件设置变化需要重启开发服务。

修复后的真实桌面检查确认：主角色与 Inlay 均无该 DOM 节点。
菜单展开、聊天、麦克风与拖拽控件保持原有用途。

## 默认角色的简明提示

本地模式对未定制的内置 `default` 角色使用简明中文提示投影。
投影约束一到两句口语、一次前置 ACT、合法情绪枚举和实际加载的动作组。
记忆仍通过独立授权和检索流程进入上下文。
原始角色描述保留在存储中，历史消息保持完整。

识别条件包括角色 ID、名称、精确内置描述、空的自定义提示字段和默认美术指令。
导入角色、自定义系统提示、追加描述、自定义场景或启用美术能力都会保留原提示。

已识别的旧中文描述来自公开提交 `c595fd645c40280b87eec313774d4943b5acb011`。
它由当时的 `SystemPromptV2(prefix, emotions, suffix)` 生成，长度为 1,450 字符。
本机旧默认描述与该公开模板的 SHA-256 完全相等。
源码保存这个固定公开模板用于精确比较，不从用户提示生成识别常量。

运行时上下文按目标会话的角色选择提示，不依赖当前前台选中的角色。
简明 ACT 上下文仍会替换上一份运行期上下文，避免旧长说明残留。

## 本地对话的默认工具

本地模式不再自动注入通用 MCP 包装器、调试工具和 Spark 指令工具。
这些工具的 schema 会占用本地模型上下文，即使当前对话没有使用对应集成。

调用方显式提供的内置、调试和 Spark 工具继续保留。
自定义请求工具、已注册的 active 工具（包括唤醒词），以及已配置的网页搜索继续可用。
连接模式沿用原有默认工具列表。

## 验证

真实 Edge 的本地模式和向导回归共 9 项通过。
它们覆盖匿名启动、禁用账号请求、保留已有数据、禁用同步与遥测，以及本地配置入口。
账号与角色继承回归共 23 项通过，固定旧模板识别后的角色用例共 18 项通过。
核心聊天编排 49 项通过，核心构建通过。

```powershell
$env:VITEST_BROWSER_CHANNEL = 'msedge'
pnpm --config.verify-deps-before-run=false -F @proj-airi/stage-ui exec vitest run src/libs/deployment.browser.test.ts src/components/scenarios/dialogs/onboarding/step-welcome.browser.test.ts --project browser
pnpm --config.verify-deps-before-run=false -F @proj-airi/stage-ui exec vitest run src/stores/auth.test.ts src/stores/modules/airi-card-inheritance.test.ts --project node
pnpm --config.verify-deps-before-run=false -F @proj-airi/core-agent exec vitest run src/runtime/chat-orchestrator-runtime.test.ts
pnpm --config.verify-deps-before-run=false -F @proj-airi/stage-ui exec vitest run src/stores/ai/chat-llm/tool-resolver.test.ts --project node
```

既有账号测试在测试项目中显式设置 `VITE_AIRI_LOCAL_MODE=false`。
本地模式用例显式设为 `true`，两种模式分别覆盖。
