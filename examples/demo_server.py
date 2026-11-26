"""为本机 Atlas 验收提供公开示例页、账号登录、人工确认和会话过期。"""

import hmac
import os
import secrets
import threading
import time
from html import escape
from http import HTTPStatus
from http.cookies import CookieError, SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs


PUBLIC_PAGE = Path(__file__).with_name("demo") / "index.html"
# 以下是公开的合成测试数据，容器仅在本机回环端口提供服务。
DEMO_ACCOUNT = "atlas-demo"
DEMO_PASSWORD = "atlas-demo-only"
COOKIE_NAME = "atlas_demo_session"


class DemoServer(ThreadingHTTPServer):
    """为一个合成账号保存服务端会话，实例重启后旧会话立即失效。"""

    def __init__(self, address, mode: str, lifetime: int, account: str, password: str):
        """指定合成账号、会话有效秒数，以及正常、人工确认或权限不足的验收模式。"""
        if mode not in {"normal", "challenge", "forbidden"} or lifetime < 1:
            raise ValueError("示例站模式无效，或会话秒数小于 1。")
        if not account or not password:
            raise ValueError("示例站需要非空的合成账号和合成密码。")
        self.mode = mode
        self.lifetime = lifetime
        self.account = account
        self.password = password
        self.sessions: dict[str, tuple[float, str]] = {}
        self.lock = threading.Lock()
        super().__init__(address, DemoHandler)

    def issue_session(self, state: str, previous: str, *, confirm: bool = False) -> str | None:
        """生成随机会话；人工确认在同一把锁内核验并换掉一次有效的待确认会话。"""
        token = secrets.token_urlsafe(32)
        now = time.monotonic()
        with self.lock:
            for key, (deadline, _) in list(self.sessions.items()):
                if deadline <= now:
                    del self.sessions[key]
            if confirm and self.sessions.get(previous, (0, ""))[1] != "pending":
                return None
            self.sessions.pop(previous, None)
            self.sessions[token] = (now + self.lifetime, state)
        return token

    def session_state(self, token: str) -> str:
        """按服务端时钟核验 Cookie，只返回有效会话的状态。"""
        with self.lock:
            deadline, state = self.sessions.get(token, (0, ""))
            if deadline <= time.monotonic():
                self.sessions.pop(token, None)
                return ""
            return state


class DemoHandler(BaseHTTPRequestHandler):
    """处理同源 HTML 表单，页面和日志中均省略会话及密码。"""

    server: DemoServer

    def log_message(self, format, *args):
        """关闭请求日志，避免意外输出 URL 中的用户输入。"""

    def cookie_token(self) -> str:
        """读取本示例站的 Cookie，格式无效时按未登录处理。"""
        try:
            cookies = SimpleCookie(self.headers.get("Cookie", ""))
        except CookieError:
            return ""
        item = cookies.get(COOKIE_NAME)
        return item.value if item else ""

    def respond(self, status: int, body: bytes = b"", location: str = "", token: str = ""):
        """发送不可缓存的页面或同站跳转，可附上仅供浏览器使用的会话 Cookie。"""
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Security-Policy", "default-src 'none'; form-action 'self'; frame-ancestors 'none'")
        # Chromium 的表单导航需保留同源 Origin，供下面的提交来源核验使用。
        self.send_header("Referrer-Policy", "same-origin")
        if location:
            self.send_header("Location", location)
        if token:
            self.send_header("Set-Cookie", f"{COOKIE_NAME}={token}; Path=/; Max-Age={self.server.lifetime}; HttpOnly; SameSite=Strict")
        self.end_headers()
        self.wfile.write(body)

    def page(self, title: str, content: str, status: int = HTTPStatus.OK):
        """输出带标题的合成 HTML 页面，调用方预先转义可配置的文本。"""
        page = (f'<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">'
                f'<meta name="viewport" content="width=device-width, initial-scale=1">'
                f'<title>{title} · Atlas 示例后台</title></head><body><main>'
                f'<h1>{title}</h1>{content}</main></body></html>')
        self.respond(status, page.encode("utf-8"))

    def login_form(self, invalid: bool = False):
        """显示合成账号的登录表单；错密时保留重新输入的入口。"""
        error = '<p id="login-error" role="alert">账号或密码错误。</p>' if invalid else ""
        self.page("登录", error + '<form method="post" action="/login">'
                  '<p><label for="username">账号</label> <input id="username" name="username" '
                  'autocomplete="username" required></p>'
                  '<p><label for="password">密码</label> <input id="password" name="password" '
                  'type="password" autocomplete="current-password" required></p>'
                  '<button type="submit">登录</button></form>',
                  HTTPStatus.UNAUTHORIZED if invalid else HTTPStatus.OK)

    def do_GET(self):
        """公开首页用于原有读取验收；挑战页与受保护页均核验服务端会话。"""
        if self.path == "/":
            self.respond(HTTPStatus.OK, PUBLIC_PAGE.read_bytes())
        elif self.path == "/login":
            self.login_form()
        elif self.path in {"/app", "/challenge"}:
            state = self.server.session_state(self.cookie_token())
            if not state:
                self.respond(HTTPStatus.FOUND, location="/login")
            elif state == "pending" and self.path == "/challenge":
                self.page("人工确认", '<form id="challenge" method="post" action="/challenge">'
                          '<p>请在此窗口完成本次示例登录。</p>'
                          '<button type="submit" name="confirm" value="yes">完成人工确认</button></form>')
            elif state == "pending":
                self.respond(HTTPStatus.FOUND, location="/challenge")
            elif self.path == "/challenge":
                self.respond(HTTPStatus.FOUND, location="/app")
            elif self.server.mode == "forbidden":
                self.page("权限不足", "<p>此账号没有查看申请列表的权限。</p>", HTTPStatus.FORBIDDEN)
            else:
                self.page("申请列表", f'<p>当前账号 <strong id="account">{escape(self.server.account)}</strong></p>')
        else:
            self.respond(HTTPStatus.NOT_FOUND)

    def do_POST(self):
        """核验同源表单后完成账号登录或人工确认，限制请求体大小。"""
        if self.path not in {"/login", "/challenge"}:
            self.respond(HTTPStatus.NOT_FOUND)
            return
        origin = self.headers.get("Origin")
        if origin is not None and origin != f"http://{self.headers.get('Host', '')}":
            self.respond(HTTPStatus.FORBIDDEN)
            return
        try:
            length = int(self.headers.get("Content-Length", ""))
            if not 0 < length <= 4096 or self.headers.get_content_type() != "application/x-www-form-urlencoded":
                raise ValueError
            fields = parse_qs(self.rfile.read(length).decode("utf-8"), strict_parsing=True, max_num_fields=4)
        except (UnicodeError, ValueError):
            self.respond(HTTPStatus.BAD_REQUEST)
            return
        previous = self.cookie_token()
        if self.path == "/login":
            user = fields.get("username", [])
            password = fields.get("password", [])
            if (user != [self.server.account] or len(password) != 1 or
                    not hmac.compare_digest(password[0].encode(), self.server.password.encode())):
                self.login_form(invalid=True)
                return
            state = "pending" if self.server.mode == "challenge" else "active"
        else:
            if fields.get("confirm") != ["yes"]:
                self.respond(HTTPStatus.FOUND, location="/login")
                return
            state = "active"
        token = self.server.issue_session(state, previous, confirm=self.path == "/challenge")
        if token is None:
            self.respond(HTTPStatus.FOUND, location="/login")
            return
        self.respond(HTTPStatus.SEE_OTHER, location="/challenge" if state == "pending" else "/app", token=token)


if __name__ == "__main__":
    # Compose 将容器的 8080 端口仅发布到宿主回环地址。
    with DemoServer(
        ("0.0.0.0", 8080),
        os.getenv("WEBTEST_ATLAS_DEMO_MODE", "normal"),
        int(os.getenv("WEBTEST_ATLAS_DEMO_SESSION_SECONDS", "3600")),
        os.getenv("WEBTEST_ATLAS_DEMO_ACCOUNT", DEMO_ACCOUNT),
        os.getenv("WEBTEST_ATLAS_DEMO_PASSWORD", DEMO_PASSWORD),
    ) as server:
        server.serve_forever()
