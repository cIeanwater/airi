# 本地运行与 GitHub 部署任务

## 本次失败的实际原因

2026-10-17，默认分支 `4026e33e38bc7d5257c955e2ea64fb8f93388e78` 触发了上游保留的生产部署任务。
GitHub 插件读取到以下结果：

| 任务 | 实际结果与原因 | 原始记录 |
| --- | --- | --- |
| `Production` | 文档、Web、组件展示均构建成功，最后 Wrangler 缺少 `CLOUDFLARE_API_TOKEN` | [Cloudflare Workers](https://github.com/cIeanwater/airi/actions/runs/39008851569/job/117327063258) |
| `Auth Production` | 账号界面构建成功，最后 Wrangler 缺少同一 Cloudflare Token | [Cloudflare Pages Auth UI](https://github.com/cIeanwater/airi/actions/runs/39008851592/job/117326822460) |
| Crowdin | 翻译上传没有配置 API Token | [翻译上传](https://github.com/cIeanwater/airi/actions/runs/39008851608/job/117326822580) |
| CI Unit Test | 一条 RevenueCat 测试使用真实时钟，当前时间超过了固定购买日期 | [单元测试](https://github.com/cIeanwater/airi/actions/runs/39008851605/job/117326822401) |

Cloudflare 的两个失败发生在云端发布步骤，不是前端构建失败。
这些任务原先还使用上游的 Worker、Pages 项目与生产域名。
连接 GitHub 插件不会提供 Cloudflare 或 Crowdin 凭据。

GitHub 的部署记录也不是本地服务运行状态。
本地麦克风、转写、对话模型、语音输出和消息记录不依赖这两个生产环境。
历史失败记录保留，不会被删除或改成成功。

## 当前默认行为

两个 Cloudflare 生产任务只提供 `workflow_dispatch`。
提交到 `main` 不再自动尝试生产部署。
运行云端任务还要求仓库变量 `ENABLE_HOSTED_DEPLOYMENTS=true`，并勾选手动输入 `confirm`。
缺少这些开关时，生产任务不会开始部署。

正常 CI 的构建、类型检查、lint 和单元测试继续运行。
它们没有被改成跳过，以免掩盖代码错误。

## 后续完整 CI 检查

首个修复提交 `832bec1c501e72dbbc75db078bb71e4239fc5b16` 的[完整 CI](https://github.com/cIeanwater/airi/actions/runs/39039327880)中：

- Web、Electron 等全部构建、类型检查、lint 与来源检查通过。
- 独立的 [Linux 桌面端到端测试](https://github.com/cIeanwater/airi/actions/runs/39039328034)通过。
- 根单元测试 2,928 项通过，原来的账务时间测试已恢复；桌面浏览器 165 项与 Web 浏览器 1 项通过。
- 设置页七个文件因缺少 `virtual:airi-transformers` 资源解析插件而未能加载，后续阶段因此尚未运行。

已为独立设置页 Vitest 入口接入与正式应用相同的 Transformers.js 资源插件。
完整设置页 12 文件、37 项测试在真实 Edge 中通过，没有模拟 VAD 或跳过断言。

继续执行此前未到达的共享 UI 测试时，一并补齐默认角色新接口的旧测试 fixture。
Provider 缓存回归使用真实回环 HTTP 服务验证新旧凭证，避免访问外网；语言默认值改在真实浏览器存储中分别验证中文和英文。
Windows 下 UnoCSS 的文件 glob 归一化为正斜杠，恢复测试样式采集。
触控目标最少 44 px 和长列表滚动的断言均保留，组件布局没有为通过测试而修改。

完整验证记录见[本地轻量伴侣验收](validation/2026-10-17-fast-local-companion.zh-CN.md)。

Fork 中的四个 Crowdin 任务默认跳过。
只有 `ENABLE_CROWDIN_SYNC=true` 才会启用 Fork 的翻译同步。
上游仓库保留其原有自动翻译行为。

## 本地使用

本地模式默认为开启，不需要部署 Auth Production。
可用服务通过本机启动器启动，配置和检查见[本地语音对话](local-voice-conversation.zh-CN.md)。
本地模式使用 `VITE_AIRI_LOCAL_MODE=true`。
只有明确设为字符串 `false`，应用才启用连接账号服务的入口。

仅运行 Web 开发界面时，在仓库根目录执行：

```powershell
pnpm --config.verify-deps-before-run=false -F @proj-airi/stage-web dev --host 127.0.0.1
```

预览已有构建时，执行：

```powershell
pnpm --config.verify-deps-before-run=false -F @proj-airi/stage-web preview --host 127.0.0.1
```

预览不会启动本地模型服务。
页面中的本地 Provider 仍需连接自己的模型、ASR 和 TTS 服务。

## 可选 Cloudflare 部署

以下配置只用于自己明确需要的云端站点。
本次没有执行新的云端发布，也没有检查用户的 Cloudflare 账号权限。

在仓库的 Actions Secrets 中配置：

| Secret | 用途 |
| --- | --- |
| `CLOUDFLARE_API_TOKEN` | 对自己的目标 Worker 或 Pages 项目执行发布 |
| `CLOUDFLARE_ACCOUNT_ID` | 自己的 Cloudflare 账号标识 |

在仓库的 Actions Variables 中配置：

| Variable | 用途 |
| --- | --- |
| `ENABLE_HOSTED_DEPLOYMENTS` | 明确设为 `true`，允许手动生产发布 |
| `CLOUDFLARE_WORKER_NAME` | Web 站点使用的自有 Worker 名称 |
| `VITE_WEB_APP_URL` | 自有 Web 站点的 HTTPS 地址 |
| `CLOUDFLARE_AUTH_PROJECT` | 可选账号界面的自有 Pages 项目 |
| `AUTH_UI_URL` | 可选账号界面的 HTTPS 地址 |
| `VITE_SERVER_URL` | 账号界面连接的自有 API HTTPS 地址 |

Web 发布通过 `--name` 显式选择自己的 Worker，不再把配置中的上游名称当成目标。
Auth UI 发布通过 `CLOUDFLARE_AUTH_PROJECT` 选择自己的 Pages 项目。
任务在安装依赖前检查必要凭据、项目名称和地址。
预检只检查配置是否存在和格式是否有效，实际账号授权仍由 Cloudflare 校验。

Web 生产构建默认继续采用本地模式，且关闭分析上报。
Auth UI 是单独的可选服务，构建时使用连接账号服务的配置。
部署 Auth UI 不能代替部署 Auth、API、数据库或配置回调域名。
本机模型服务也不会因为静态站点上线而自动搬到云端。

## 可选 Crowdin 同步

Fork 的翻译同步需要：

| 配置 | 用途 |
| --- | --- |
| Variable `ENABLE_CROWDIN_SYNC=true` | 开启 Fork 的 Crowdin 任务 |
| Variable `CROWDIN_PROJECT_ID` | 自己的 Crowdin 项目 ID |
| Secret `CROWDIN_PERSONAL_TOKEN` | 访问自己项目的 Token |
| Variable `CROWDIN_GLOSSARY_ID` | 仅词汇表上传需要 |

Fork 启用同步后，任务会检查自己的项目 ID 与 Token。
显式 `project_id` 会覆盖仓库中保留的上游 Crowdin 项目配置。
这避免误向上游翻译项目写入本地修改。
修改本地中文或英文文件本身不需要 Crowdin 账号。

## CI 时钟回归

失败测试先用实际当前时间完成首次充值，却把升级购买日期固定为 `2026-10-15`。
当实际时间超过这个日期，首次充值就晚于升级周期边界。
这与测试原本描述的“先购买、后升级”顺序不同。

既有账务规则要求新边界晚于最后一次充值，才触发再次充值。
规则见[订阅额度 ADR](../../server/docs/ai/adr/2026-10-04-subscription-credits.md)。
本次只固定该测试的 Date 时钟，并在升级前推进到购买日。
结束时恢复真实时钟。
测试仍要求新额度为 5,000，没有修改账务实现或降低断言。

检查结果：

- 修改前复现云端同一失败，14 项通过，1 项失败。
- 修改后完整 RevenueCat 路由文件 15 项通过。
- `@proj-airi/api-server typecheck` 通过。
- 修改的六个工作流通过显式 YAML lint，相关检查不执行云端发布。
