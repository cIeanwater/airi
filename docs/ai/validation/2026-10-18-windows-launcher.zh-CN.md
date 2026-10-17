# Windows 双击启动验收

日期：2026-10-18。

## 原因与修复

四个模型服务均已就绪，但桌面启动失败。桌面错误日志报告找不到 `node`。
旧入口运行 `electron-vite.cmd`，该脚本依赖 PATH 中的 Node。
调试终端包含 Codex 的 Node 路径，Windows 资源管理器启动的进程没有该路径。

入口现在直接用已准备的 Node 执行 `electron-vite.js`。
Node 路径依次取显式配置、父目录便携运行时、系统 PATH。
入口在启动服务前检查 Node 版本，并为桌面子进程设置 PATH。
这些设置仅影响子进程，不修改 Windows 全局环境。

批处理成功时明确返回 `0`，避免重定向输入导致末尾的 `timeout` 返回失败。
真实启动错误仍返回 `1`。

## 本机复测

测试从注册表读取 Machine 与 User PATH，移除调试终端注入的 Node 路径。
测试还移除了 `NODE_PATH` 和 `ELECTRON_RUN_AS_NODE`，然后运行原 `Start-Humanning.cmd`。

| 检查 | 结果 |
| --- | --- |
| 修复前复现 | 四个模型健康，桌面退出码为 1，日志提示找不到 `node` |
| 修复后启动 | 便携 Node 26.7.0 运行 Electron Vite，桌面服务约 7.3 秒就绪 |
| 桌面加载 | Vue 应用已挂载，Live2D 模型已选中，画布为 900 × 1084 |
| 原会话 | 活动会话标识保持不变 |
| 开发工具浮标 | 未出现 |
| 再次运行入口 | 约 0.53 秒返回成功，五个服务均报告 `already_ready` |
| 批处理退出状态 | `0`，标准错误为空 |
| 模型与消息记录 | 四个已有服务保持运行，消息 JSON 记录仍开启 |

7.3 秒只表示已有模型服务就绪时的桌面启动耗时，不包含模型冷加载。
本次没有录音，也没有读取或上传聊天正文。

原始复测元数据保存在本机父目录 `.tools/launcher-node-path-2026-10-18/`。
该目录不提交到仓库。

## 回归命令

```powershell
python -B scripts/test-local-voice-launcher.py -v
python -m py_compile scripts/start-local-voice.py scripts/test-local-voice-launcher.py
python scripts/start-local-voice.py --check
pnpm --config.verify-deps-before-run=false lint
```

回归使用临时准备目录和真实 Node，不启动模型或 Electron。
它覆盖空 PATH、带空格路径、Node 子进程、无效运行时提前失败、仅启动服务及批处理退出状态。
Windows 若无可用 Node，请先用 `HUMANNING_TEST_NODE` 指定测试运行时。
Windows CI 使用固定 Node 26.7.0 和 Python 3.12，无需安装模型或应用依赖。

本机使用入口的 Python 3.10 与开发环境 Python 3.13 运行，13 项回归均通过。
语法检查和 `--check` 通过。完整 lint 返回 `0`，保留 655 项既有警告。
新增工作流通过 actionlint 1.7.12 与显式 YAML ESLint 检查。
