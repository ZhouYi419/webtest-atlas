# WebTest Atlas

WebTest Atlas 是一个通过 DeepSeek Harness（DSH）对话使用的 Web 功能测试助手。项目计划支持站点接入、浏览器探索、项目知识复用、UI 与接口测试，以及可独立运行的自动化脚本。

当前可通过 DSH 工具读取本机合成示例后台首页，并按运行 ID 查询页面结果和步骤。登录、探索、业务断言和脚本导出由后续任务交付。

## Windows 本机启动

在 PowerShell 7 中启动。运行环境为 Python 3.13、Chrome 或 Edge、Docker Compose，以及带 Node.js 的 DSH。已验证的 DSH 版本和模型配置见 [DSH 运行说明](docs/DSH.md)。

在项目根目录使用 Python 3.13 虚拟环境安装依赖。若尚无虚拟环境，先运行 `py -3.13 -m venv .venv`。

```powershell
.\.venv\Scripts\python.exe -m pip install -e ".[test]"
docker compose up -d demo
.\scripts\start-dsh.ps1 -UseLocalModels -OpenBrowser
```

`-UseLocalModels` 沿用本机 DSH 的模型设置和凭据。首次配置模型可省略此参数，在项目独立的 DSH Web 中配置。`-OpenBrowser` 让 DSH 在默认浏览器打开带本次登录信息的入口，省略时使用启动终端打印的链接。

如果本机已有包含 Nginx 的兼容镜像，可先将 `WEBTEST_ATLAS_DEMO_IMAGE` 设置为其本地标签。Compose 从项目挂载完整的 Nginx 配置和合成页面。示例站默认在 [本机 18765 端口](http://127.0.0.1:18765/) 提供服务，DSH 在 [本机 3081 端口](http://127.0.0.1:3081/) 打开。自定义示例端口时，在启动两者的终端统一设置 `WEBTEST_ATLAS_DEMO_PORT`。

选择 **WebTest Atlas**，输入“读取本地示例后台首页，告诉我运行 ID、标题和步骤”。另开新对话输入“查询运行 <完整 ID>”。运行库保存在 `.local/atlas.sqlite3`，服务重启后可继续查询。停用示例站可执行 `docker compose stop demo`。

## 回归验证

```powershell
.\.venv\Scripts\python.exe -m pytest -q
```

回归包含真实 Chromium 读取、越界拦截、历史恢复，以及独立 MCP 进程之间的查询。
