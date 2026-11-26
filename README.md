# WebTest Atlas

WebTest Atlas 是一个通过 DeepSeek Harness（DSH）对话使用的 Web 功能测试助手。项目计划支持站点接入、浏览器探索、项目知识复用、UI 与接口测试，以及可独立运行的自动化脚本。

在 Windows 的 DSH 对话里发送公开网址和环境，Atlas 会用 Chromium 实际打开入口页，返回读到的标题、可见一级标题和运行 ID。不同网址或环境分别保存，旧结果可在新对话按 ID 查回。另支持对本机预先配置的一个账号执行登录、复用或恢复会话。

## Windows 本机启动

可在 Windows PowerShell 5.1 或 PowerShell 7 中启动。DSH 使用本机 Node.js、Python 3.13 以及 Chrome 或 Edge。运行本地合成示例站时再启动 Docker Compose。已验证的 DSH 版本和模型配置见 [DSH 运行说明](docs/DSH.md)。

在项目根目录使用 Python 3.13 虚拟环境安装依赖。若尚无虚拟环境，先运行 `py -3.13 -m venv .venv`。

```powershell
.\.venv\Scripts\python.exe -m pip install -e ".[test]"
.\scripts\start-dsh.ps1 -UseLocalModels -OpenBrowser
```

`-UseLocalModels` 沿用本机 DSH 的模型设置和凭据。首次配置模型可省略此参数，在项目独立的 DSH Web 中配置。`-OpenBrowser` 让 DSH 在默认浏览器打开带本次登录信息的入口，省略时使用启动终端打印的链接。

DSH 默认使用 [本机 3081 端口](http://127.0.0.1:3081/)。

## 在对话里打开网站

选择 WebTest Atlas，把下面的示例网址换成自己的，即可在对话里使用。

- “这是 Aweb 的测试环境，地址是 qa.example.org。”Atlas 会保存名称、环境和地址，打开首页并给出运行 ID。只发网址也能查看页面。
- 在新对话里说“打开 Aweb”，会从同一份 Atlas 数据库找到当前地址，重新读取。连接这份库的其他 Atlas 工作区也可使用这个名称。
- “把 Aweb 的测试环境地址更新为 qa2.example.org。”只改当前地址。若省略环境且无法唯一确定，点选追问卡里的环境即可继续。以后按名称打开会访问新地址，凭旧运行 ID 仍能查到当时的地址和页面结果。

此入口匿名读取 HTTP(S) 网页，支持普通路径和不带参数的 `#/...` 路由。请使用公开入口地址，把登录账号、密码和带令牌的网址留在聊天之外。遇到可见密码框或 HTTP 拒绝时，对话会说明实际看到的情况。当前凭据接入方式见下节。

## 登录预先配置的测试站

```powershell
.\.venv\Scripts\python.exe -m webtest_atlas.site_config
```

在终端输入测试账号、环境、完整登录页地址和受保护页面。指定受保护页面上显示当前账号的元素（CSS 选择器）和预期完整文字。例如，若当前用户名显示在 `<span id="current-user">` 内，选择器填 `#current-user`。配置同源资源读取路径和登录表单实际提交的路径，按需补充表单、验证码及错误提示的选择器。最后隐藏输入密码。

在 **WebTest Atlas** 对话中输入“登录测试后台”。首次登录将核对受保护页面与账号。后续优先复用有效会话，失效后最多自动提交一次密码。需要本人验证时，在打开的 Chromium 窗口完成操作，回到对话输入“查询运行 <完整 ID>”。错误密码可通过同一条本机配置命令更新；重配会清除先前的登录态。

支持同源、基于表单的账号密码登录，远程站使用 HTTPS，本机回环可用 HTTP。业务读取和登录提交范围由终端配置控制。需要跨域单点登录、嵌入第三方验证框或跨域资源的流程会提示阻塞。站点、账号或密码均在本机命令中配置。

## 本地合成示例

```powershell
docker compose up -d demo
.\.venv\Scripts\python.exe -m webtest_atlas.site_config --demo
```

示例站默认在 [本机 18765 端口](http://127.0.0.1:18765/) 提供服务。`--demo` 预填它的路径和账号标志。终端默认账号为 `atlas-demo`，该示例专用的公开合成密码为 `atlas-demo-only`。有效登录后才能访问 `/app`。也可以在 DSH 输入“读取本地示例后台首页”，验证独立的公开首页读取工具。停用示例站可运行 `docker compose stop demo`。镜像、端口、人工挑战和过期场景见 [DSH 运行说明](docs/DSH.md)。

## 回归验证

```powershell
.\.venv\Scripts\python.exe -m pytest -q
```

回归涵盖 Chromium 表单登录、会话复用和恢复、挑战与阻塞、请求范围、加密存储、历史升级，以及独立 MCP 进程之间的查询。
