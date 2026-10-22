# 2026-10-23：个人仓库定时工作流修复

这些邮件对应 GitHub Actions 的初始化或账号配置失败，不能据此判断业务代码编译失败。

| 工作流 | 日志证据 | 修复 |
| --- | --- | --- |
| Sponsors SVG | [run 39749798266](https://github.com/cIeanwater/airi/actions/runs/39749798266) 缺少 Patreon token | 个人 fork 默认不运行上游赞助账号任务。配置 `ENABLE_SPONSORS_WORKFLOW=true` 后可启用。 |
| Release Pocket Android | [run 39729116634](https://github.com/cIeanwater/airi/actions/runs/39729116634) 无法找到 SDK `tools` 包 | 明确安装 `platform-tools`。个人 fork 的每日发布需配置 `ENABLE_MOBILE_RELEASES=true`。 |
| Release Tamagotchi | [run 39712002433](https://github.com/cIeanwater/airi/actions/runs/39712002433) 下载独立 pnpm 可执行文件超时 | 所有平台先安装 Node，再经 npm 安装固定版本 pnpm，增加下载重试。个人 fork 每日发布需配置 `ENABLE_NIGHTLY_RELEASES=true`。 |

Android 和桌面的手动发布、预发布触发保留。普通测试、代码检查和依赖安全策略未关闭。

验证：使用项目已有的 `yaml` 解析器读取三个工作流，检查任务条件、SDK 包名、Node 初始化和 pnpm 安装步骤。没有触发真实签名或发布任务。

旧 run 固定使用旧提交。重新运行旧 run 不会验证新配置。每日任务在默认分支读取新配置后，未启用的个人 fork 发布任务会跳过。
