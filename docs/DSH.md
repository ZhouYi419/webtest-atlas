# 在 Windows 启动 Atlas 对话

项目使用 DeepSeek Harness（DSH）原生 Web、专用的 `atlas` profile 和本机 Chromium。启动器要求 `@deepseek-ai/dsh@0.2.2-rc.5`。该版本要求 Node.js 24.9.0 或更高，本机为 24.18.0。

## 启动

在项目根目录准备 [README](../README.md) 所述的 Python 环境，需要合成站时启动示例容器，然后从 Windows PowerShell 5.1 或 PowerShell 7 启动 DSH。维护启动脚本时保留 UTF-8 BOM，以便 Windows PowerShell 5.1 正确读取中文注释。

```powershell
.\scripts\start-dsh.ps1
```

启动器从 npm 全局安装目录和 npx 缓存定位匹配版本。本机已复用 npx 缓存。也可通过 `-DshCliPath` 指定该包的 `lib/bin.js`。首次使用且本机缺包时，启动器会显示带固定版本的安装命令。

首次访问或宿主重启后，使用启动终端打印的本机登录链接。也可以加 `-OpenBrowser`，由 DSH 在默认浏览器完成本机登录并跳转到普通地址。登录链接含本次进程的临时令牌，请留在本机。默认监听 `127.0.0.1:3081`。如端口已被使用，可用 `-Port 3082` 指定空闲端口。在启动终端按 Ctrl+C 关闭宿主。DSH 在 Windows 启动 Python stdio 子进程，Playwright 从宿主访问 Compose 映射的本机端口。

已在本机 DSH 配置模型时，可以用 `.\scripts\start-dsh.ps1 -UseLocalModels -OpenBrowser` 启动。Atlas 先加载宿主自带的模型设置，再依次应用原 DSH 主目录下的 `profiles/web/cordis.patch.yml` 和 `cordis.patch.yml`，仅读取其中的静态模型配置。凭据由 DSH 的官方提供器从原 `DSH_HOME`（默认 `~/.dsh`）解析。项目启动器将 Node 宿主的文件写权限限定到项目 `.local/` 和当前用户的临时目录。请从原 DSH 管理共用模型和凭据。模型行含手写动态 JavaScript 表达式时，可省略 `-UseLocalModels`，使用独立的项目配置。

固定版本默认使用官方 `deepseek-flash`（DeepSeek-V4.1-Flash）。当前补丁声明的提供方、模型 ID 和推理等级按配置生效。复用模式跳过初次录入凭据的弹窗。若实际调用提示缺少凭据，请在原 DSH 中检查配置并重新启动 Atlas。工作区、聊天历史和新会话的 Atlas 预设均由项目管理。

## 对话与历史

1. 在 Web 选择自动登记的 `workspace` 工作区并新建对话，预设为 **WebTest Atlas**。启动时检查 Atlas 的网址读取、改址、运行查询、已配置站点登录、示例页读取工具，以及 DSH 原生环境选择工具。使用项目独立模型存储时，可在原生「设置 → 模型」中配置模型。通过环境变量配置模型时，只向启动 DSH 的终端提供所需凭据。
2. 发送要查看的网址和环境。比如运行了本项目 Compose 示例站时，输入「这是测试环境，http://127.0.0.1:18765/，看一下首页」。`open_site` 在临时匿名 Chromium 上下文中打开给定地址，只读取同源页面和渲染资源，返回所属部署、HTTP 结果、可公开的标题与运行 ID。省略环境会记为“未指定”。同一地址在同一环境中的连续两次读取分别保存各自的证据。接入时使用稳定入口，带查询参数或凭据的地址会被拒绝。
3. 新建对话，输入「查询运行 <上一步的完整 ID>，列出结果和步骤」。查询读取同一项目运行库。宿主重启后仍可使用该 ID。

希望以后按名称访问时，可以说「这是仓库的测试环境，地址是 qa.example.org」。Atlas 会一并登记名称、环境和入口，省略协议的域名按 HTTPS 处理。换一个对话或工作区说「打开仓库」即可重新访问。共用同一份 Atlas SQLite 数据库的工作区可查到同一配置；分别使用独立数据库的实例各自保存。

说「把仓库地址更新为 qa2.example.org」只改当前配置。唯一匹配时直接更新；同名站点有多个环境且无法确定时，在追问卡里选择环境，继续原操作。直接说「把仓库的测试环境地址改成 qa2.example.org」也可以。旧运行仍保留原地址、标题和步骤。

如需观察 Chromium 窗口，在启动 DSH 的 PowerShell 中先设置 `$env:WEBTEST_ATLAS_HEADED = '1'`。Chrome 为默认通道，Edge 可设置 `$env:WEBTEST_ATLAS_BROWSER_CHANNEL = 'msedge'`。改变环境后重新启动宿主。

首次读取保留可见一级标题与文档标题，缺少可公开标题时仍报告实际 HTTP 状态。可见密码框只作为页面观察。401 表明需要认证；403 表明匿名访问被拒，具体原因需另行核对。跨源主文档跳转会在访问下一站之前停止。全站探索、跨站 SSO 和对话内凭据接收尚待后续接入。

Chromium 接收主文档后就开始观察首屏，页面后部的慢脚本可以继续加载。主文档请求最多等待 20 秒，单个附属资源最多 6 秒，整次页面读取共用 30 秒预算。超时提示会区分“未取得主文档”与“已收到 HTTP 响应但首屏仍未就绪”；这两种情况均无法据此判定登录要求。

## 站点配置和登录

正式配置入口为 `python -m webtest_atlas.site_config`（使用项目虚拟环境），`--demo` 可以预填 Compose 示例站。命令在当前项目的本机目录保存一个环境和一个账号。Windows 当前用户通过 DPAPI 加密配置、凭据和浏览器状态。每次重新配置会换一个标识，先前执行中的任务无法向新配置保存会话。通过 `WEBTEST_ATLAS_SITE_HOME` 可以指定单独的站点数据目录；配置终端和启动 DSH 的终端应指向同一目录。

- 登录入口、受保护页和允许路径须同源。登录地址和受保护页须有独立路径。GET/HEAD 的额外路径可以精确指定，目录可写作 `/assets/*`。登录和人工验证 POST 只接受配置的精确路径。请求或跳转越界时，回到终端核对实际需要的授权。
- 在对话输入“登录测试后台”。DSH 使用无参数的 `login_site`，`authenticated` 表示受保护页面和指定账号均已核对。通过运行 ID 随时可以回查。缓慢的请求可先显示 `running`，人工验证显示 `waiting`，同一时间重复发起登录会返回已有运行。
- 登录默认打开可视 Chromium。遇到人工验证，在窗口中本人完成，原任务继续核对受保护页面。当前人工等待时间为 180 秒。关闭窗口或超时后重新发起登录。错误凭据需重新配置；账号缺少页面权限时需调整账号权限。终端设置 `WEBTEST_ATLAS_HEADED=0` 可用于无人工操作的自动化验收。
- 独立登录工作进程持有浏览器。进程提前退出后，查询原 ID 或重新登录会补记中断。已有 T001 结构的运行库首次打开时在事务中扩展约束，保留原运行 ID、时间和步骤。切换版本前可在工具退出后备份运行库。

### 重现示例站的不同结果

Compose 默认使用 `python:3.13-alpine`，也可以将 `WEBTEST_ATLAS_DEMO_IMAGE` 设为带 Python 3.13 的本机镜像标签。自定义 `WEBTEST_ATLAS_DEMO_PORT` 时，配置命令、Compose 和 DSH 应使用同一端口。

| 场景 | 示例站设置和操作 | 可见结果 |
| --- | --- | --- |
| 首次及复用 | 默认模式，配置好账号，连续从两个对话登录 | 首次核对成功，后续复用会话 |
| 服务端过期 | 缩短 `WEBTEST_ATLAS_DEMO_SESSION_SECONDS`，等过期后再登录，或重启专用示例容器使其内存会话失效 | 自动重新登录一次并核对账号 |
| 人工验证 | 设置 `WEBTEST_ATLAS_DEMO_MODE=challenge`，登录后在可视窗口确认 | 原运行从等待人工转为成功 |
| 权限不足 | 设置 `WEBTEST_ATLAS_DEMO_MODE=forbidden` | 已认证账号访问受保护页面得到 403，工具报告阻塞 |
| 错误凭据 | 在终端录入与示例站不同的合成密码 | 工具报告凭据被拒绝，更新配置后可重试 |

修改示例站环境变量后运行 `docker compose up -d demo` 使变更生效。三种模式只影响合成站。示例的合成账号和合成密码可分别用 `WEBTEST_ATLAS_DEMO_ACCOUNT`、`WEBTEST_ATLAS_DEMO_PASSWORD` 指定。

## 数据与诊断

| 位置 | 内容 |
| --- | --- |
| `.local/workspace/` | 作为 DSH 默认工作区的独立目录 |
| `.local/dsh/` | 项目 DSH 主页，包含 profile、会话、DSH 自身管理的模型设置和可能配置的本地凭据 |
| `.local/atlas.sqlite3` | 站点名称、各环境当前入口、历史运行及脱敏步骤 |
| `.local/site/site.sqlite3` | 当前用户 DPAPI 加密的站点配置、凭据和会话 |
| `integrations/dsh/` | 固定的 Web 组合、预设和 MCP 工具定义 |

`.local/` 已加入 Git 忽略规则。项目预设显式禁用新版的 DeepSeek 会话日志上传和 OTel 遥测插件，启动脚本同时设置 `DSH_TELEMETRY_DISABLED=1`。本地会话可用于回查。使用云模型时，模型提供方会收到本次对话与工具结果。

如果新会话无法装载工具，先核对项目 `.venv/Scripts/python.exe` 及依赖，再查看 DSH 启动终端的 MCP 连接错误，修复后重新启动并新建对话。子进程断线时，DSH 的 MCP 插件默认自动重连；重试耗尽后可重启宿主。示例站不可达时检查 Compose 服务和端口后重新读取。查询历史只需要有效的运行 ID 和原项目运行库。

本地工具直接使用当前 SQLite 表结构。如果更早的测试库缺少 `runs.target_key`，可将 `WEBTEST_ATLAS_DB` 指向新的数据库文件并重启宿主。首次读取将初始化该文件，原库仍留在原处。

在复用本机模型的模式下，DSH 会向子进程传播其 Node 写权限参数。项目 stdio 适配器在启动 Playwright 的独立 Node 驱动之前恢复调用终端原有的 `NODE_OPTIONS`，以便读取本地示例站。调用方设置的端口、浏览器通道和可视模式由 Python 工具继承。
