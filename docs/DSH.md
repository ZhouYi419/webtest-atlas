# 在 Windows 启动 Atlas 对话

项目使用 DeepSeek Harness（DSH）原生 Web、专用的 `atlas` profile 和本机 Chromium。启动器要求 `@deepseek-ai/dsh@0.2.2-rc.5`。该版本要求 Node.js 24.9.0 或更高，本机为 24.18.0。

## 启动

在项目根目录准备 [README](../README.md) 所述的 Python 环境和示例站，然后从 PowerShell 7 启动 DSH。

```powershell
.\scripts\start-dsh.ps1
```

启动器从 npm 全局安装目录和 npx 缓存定位匹配版本。本机已复用 npx 缓存。也可通过 `-DshCliPath` 指定该包的 `lib/bin.js`。首次使用且本机缺包时，启动器会显示带固定版本的安装命令。

首次访问或宿主重启后，使用启动终端打印的本机登录链接。也可以加 `-OpenBrowser`，由 DSH 在默认浏览器完成本机登录并跳转到普通地址。登录链接含本次进程的临时令牌，请留在本机。默认监听 `127.0.0.1:3081`。如端口已被使用，可用 `-Port 3082` 指定空闲端口。在启动终端按 Ctrl+C 关闭宿主。DSH 在 Windows 启动 Python stdio 子进程，Playwright 从宿主访问 Compose 映射的本机端口。

已在本机 DSH 配置模型时，可以用 `.\scripts\start-dsh.ps1 -UseLocalModels -OpenBrowser` 启动。Atlas 先加载宿主自带的模型设置，再依次应用原 DSH 主目录下的 `profiles/web/cordis.patch.yml` 和 `cordis.patch.yml`，仅读取其中的静态模型配置。凭据由 DSH 的官方提供器从原 `DSH_HOME`（默认 `~/.dsh`）解析。项目启动器将 Node 宿主的文件写权限限定到项目 `.local/` 和当前用户的临时目录。请从原 DSH 管理共用模型和凭据。模型行含手写动态 JavaScript 表达式时，可省略 `-UseLocalModels`，使用独立的项目配置。

固定版本默认使用官方 `deepseek-flash`（DeepSeek-V4.1-Flash）。当前补丁声明的提供方、模型 ID 和推理等级按配置生效。复用模式跳过初次录入凭据的弹窗。若实际调用提示缺少凭据，请在原 DSH 中检查配置并重新启动 Atlas。工作区、聊天历史和新会话的 Atlas 预设均由项目管理。

## 对话与历史

1. 在 Web 选择自动登记的 `workspace` 工作区并新建对话，预设为 **WebTest Atlas**。启动时会检查该预设的实际工具目录，终端显示 `mcp__atlas__get_run` 和 `mcp__atlas__read_demo_page`。使用项目独立模型存储时，可在原生「设置 → 模型」中配置模型。通过环境变量配置模型时，只向启动 DSH 的终端提供所需凭据。
2. 输入「读取本地示例后台首页，告诉我运行 ID、页面标题和步骤」。工具执行期间由宿主显示调用状态，工具结束后返回本次记录。
3. 新建对话，输入「查询运行 <上一步的完整 ID>，列出结果和步骤」。查询读取同一项目运行库。宿主重启后仍可使用该 ID。

如需观察 Chromium 窗口，在启动 DSH 的 PowerShell 中先设置 `$env:WEBTEST_ATLAS_HEADED = '1'`。Chrome 为默认通道，Edge 可设置 `$env:WEBTEST_ATLAS_BROWSER_CHANNEL = 'msedge'`。改变环境后重新启动宿主。

## 数据与诊断

| 位置 | 内容 |
| --- | --- |
| `.local/workspace/` | 作为 DSH 默认工作区的独立目录 |
| `.local/dsh/` | 项目 DSH 主页，包含 profile、会话、DSH 自身管理的模型设置和可能配置的本地凭据 |
| `.local/atlas.sqlite3` | Atlas 运行、脱敏结果和步骤 |
| `integrations/dsh/` | 固定的 Web 组合、预设和 MCP 工具定义 |

`.local/` 已加入 Git 忽略规则。项目预设显式禁用新版的 DeepSeek 会话日志上传和 OTel 遥测插件，启动脚本同时设置 `DSH_TELEMETRY_DISABLED=1`。本地会话可用于回查。使用云模型时，模型提供方会收到本次对话与工具结果。

如果新会话无法装载工具，先核对项目 `.venv/Scripts/python.exe` 及依赖，再查看 DSH 启动终端的 MCP 连接错误，修复后重新启动并新建对话。子进程断线时，DSH 的 MCP 插件默认自动重连；重试耗尽后可重启宿主。示例站不可达时检查 Compose 服务和端口后重新读取。查询历史只需要有效的运行 ID 和原项目运行库。

本地工具直接使用当前 SQLite 表结构。如果更早的测试库缺少 `runs.target_key`，可将 `WEBTEST_ATLAS_DB` 指向新的数据库文件并重启宿主。首次读取将初始化该文件，原库仍留在原处。

在复用本机模型的模式下，DSH 会向子进程传播其 Node 写权限参数。项目 stdio 适配器在启动 Playwright 的独立 Node 驱动之前恢复调用终端原有的 `NODE_OPTIONS`，以便读取本地示例站。调用方设置的端口、浏览器通道和可视模式由 Python 工具继承。
