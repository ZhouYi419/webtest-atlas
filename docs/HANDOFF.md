# 开发交接

项目目录为 `E:\PythonFiles\webtest-atlas`。T001 已通过实际 DSH Web 模型对话、宿主重启后的新会话回查，以及本机异常分支验收。项目以本地单用户、单环境、Chromium 与一个测试账号为 MVP 范围。

## 先读这些文件

1. [AGENTS.md](../AGENTS.md) 了解本项目的工作流、开工说明和本机验收规范。
2. [PRD.md](PRD.md) 核对确认的 F001–F008 及对应产品验收。
3. [ARCHITECTURE.md](ARCHITECTURE.md) 核对选型建议、运行和数据边界。
4. [TASKS.md](TASKS.md) 记录切片状态及实际验收入口，后续卡片随选定任务细化。

## 第一个切片

T001 用实际 DSH Web 对话驱动 Chromium 读取本地示例后台页面，回传运行 ID、可核对的页面结果和证据。在新对话中能查到同一运行。这个入口留下后续接入、探索、测试和脚本功能所需的宿主集成与运行记录基础。

本机通过 npx 缓存复用 DSH `0.2.2-rc.5`。Atlas 使用项目级预设，复用本机模型时，按本次确认选用官方 DeepSeek-V4.1-Flash 接替旧 Flash。Compose 提供示例页，Python MCP 子进程运行 Windows Chromium，SQLite 保存两步证据。启动命令见 [README](../README.md)，模型与 profile 配置见 [DSH 运行说明](DSH.md)。项目的运行数据和凭据位于 Git 忽略范围。

T001 沿用 DSH 原生对话。首次接入需要登录的环境时，在 T002 确认站点、凭据录入、登录恢复和授权操作范围。

## 新会话可用的开场指令

> 在 E:\PythonFiles\webtest-atlas 继续开发。先读 AGENTS.md 和 docs/TASKS.md 的当前验收记录，核对工作区。T001 已完成，从 T002 收敛站点配置和登录恢复的规则。
