"""对用户明确给出的网页入口执行一次受限的匿名 Chromium 读取。"""

import os
import re
import unicodedata
from time import monotonic
from typing import TypedDict
from urllib.parse import quote, urljoin, urlsplit, urlunsplit

from playwright.sync_api import Error, Page, Route, TimeoutError as PlaywrightTimeoutError, sync_playwright

from .browser import PageFailure
from .site_config import _path


# 主文档可能经历 DNS、TLS 和服务端首字节等待；附属资源只分配较短的独立等待时间。
_PAGE_BUDGET_MS = 30_000
_DOCUMENT_TIMEOUT_MS = 20_000
_RESOURCE_TIMEOUT_MS = 6_000


class LandingResult(TypedDict):
    """主文档的实际 HTTP 状态、可公开标题以及页面上可见的密码框。"""

    http_status: int
    title: str
    marker: str
    password_field: bool


# 路径或页面标题含凭据标记、网址、长标识或常见个人联系方式时，不作为公开证据。
_SENSITIVE = re.compile(
    r"password|passwd|secret|token|session[_-]?id|bearer|cookie|api[_ -]?key|private[_ -]?key|"
    r"密钥|令牌|口令|验证码|密码\s*[:：=]|[A-Za-z0-9_-]{26,}|\d{7,}|[^\s@]+@[^\s@]+",
    re.IGNORECASE,
)
_SAFE_HEADING = re.compile(r"[\w .·•,，。!！?？|｜–—\-()（）「」《》【】&＆:：'’+＋]{1,80}")
_PRIVATE_HEADING = re.compile(
    r"\b(?:account|username|user(?:\s+name|\s+id)?|login\s+id)\s*[:=]|"
    r"(?:账号|账户|帐号|用户名|凭据|密码)(?:\s*[:=]|\s*[为是]|\s+\S)|"
    r"\b(?:mailto|tel|data|https?):|\b(?:[a-z0-9-]+\.)+[a-z]{2,}\b|"
    r"\b(?:[0-9]{1,3}\.){3}[0-9]{1,3}\b", re.IGNORECASE,
)


def _origin(url: str) -> tuple[str, str, int]:
    """按协议、国际化主机和有效端口识别同源，拒绝浏览器会另行解释的地址。"""
    if not isinstance(url, str) or re.search(r"[\x00-\x20\x7f\\]", url):
        raise ValueError
    parts = urlsplit(url)
    if parts.scheme not in {"http", "https"} or not parts.hostname or parts.username is not None or "%" in parts.netloc:
        raise ValueError
    host = parts.hostname.encode("idna").decode("ascii").lower()
    if ":" not in host and not re.fullmatch(r"[a-z0-9.-]+", host):
        raise ValueError
    if parts.port == 0:
        raise ValueError
    return parts.scheme, host, parts.port or (443 if parts.scheme == "https" else 80)


def _entry_path(value: str) -> str:
    """编码普通路径及 Vue 路由，过滤可能夹带凭据、一次性令牌或查询的入口。"""
    path = _path(value)
    if _SENSITIVE.search(path) or not re.fullmatch(r"[/\w. ~\-]*", path):
        raise ValueError
    return quote(path, safe="/-._~")


def canonical_entry(url: str) -> str:
    """返回适合记忆的完整入口，保留无查询参数的普通路径和 Vue 哈希路由。"""
    try:
        if not isinstance(url, str):
            raise ValueError
        url = url.strip()
        if len(url) > 2048 or "?" in url:
            raise ValueError
        # 日常对话可以只写域名，补上 HTTPS 后仍按同一入口规则校验主机、路径和片段。
        if re.fullmatch(r"(?:[a-zA-Z0-9-]+\.)+[a-zA-Z][a-zA-Z0-9-]*(?::\d+)?(?:[/#].*)?", url):
            url = "https://" + url
        scheme, host, port = _origin(url)
        authority = f"[{host}]" if ":" in host else host
        if port != (443 if scheme == "https" else 80):
            authority += f":{port}"
        parts = urlsplit(url)
        fragment = _entry_path(parts.fragment) if parts.fragment else ""
        return urlunsplit((scheme, authority, _entry_path(parts.path or "/"), "", fragment))
    except ValueError:
        raise PageFailure("invalid_url", "请提供完整的 HTTP 或 HTTPS 页面地址。可保留普通路径或 #/ 路由，请移除凭据、查询及临时参数。") from None


def _public_text(value: str) -> str:
    """只返回短标题；可疑文本整段省略，避免截断后泄露部分凭据。"""
    text = " ".join(value.split())
    normalized = unicodedata.normalize("NFKC", text)
    if not _SAFE_HEADING.fullmatch(text) or _SENSITIVE.search(normalized) or _PRIVATE_HEADING.search(normalized):
        return ""
    return text


class _LandingTraffic:
    """在离线渲染上下文中只代理本页发起的同源只读请求，并逐跳校验重定向。"""

    def __init__(self, page: Page, url: str):
        """设置单页的来源和读取预算，跟踪主文档是否确实收到成功响应。"""
        self.page = page
        self.origin = _origin(url)
        self.deadline = monotonic() + _PAGE_BUDGET_MS / 1000
        self.last_document = monotonic()
        self.status = 0
        self.documents = 0
        self.requests = 0
        self.follow = ""
        self.request_fragment = urlsplit(url).fragment
        self.failure: PageFailure | None = None

    def allows(self, url: str) -> bool:
        """只允许与用户入口完全同源的 HTTP 请求。"""
        try:
            return _origin(url) == self.origin
        except (UnicodeError, ValueError):
            return False

    def timed_out(self) -> PageFailure:
        """从当前主文档的真实响应区分连接等待与渲染等待。"""
        if self.status:
            return PageFailure("render_timeout", f"入口页已返回 HTTP {self.status}，等待首屏内容超时，登录状态未判定。")
        return PageFailure("site_unreachable", "等待入口页的 HTTP 主文档响应超时，未取得可核实的页面内容。")

    def timeout(self, limit: int = _PAGE_BUDGET_MS) -> int:
        """将本次请求的时限限制在整次首读的剩余预算内。"""
        remaining = int((self.deadline - monotonic()) * 1000)
        if remaining <= 0:
            raise self.timed_out()
        return min(remaining, limit)

    def handle(self, route: Route) -> None:
        """渲染器只收到逐项校验的响应，跨源主导航、写入或附属窗口无法出网。"""
        request = route.request
        main = request.resource_type == "document" and request.is_navigation_request()
        try:
            if self.failure or request.method not in {"GET", "HEAD"} or request.frame != self.page.main_frame:
                route.abort()
                return
            if not self.allows(request.url):
                if main:
                    raise PageFailure("out_of_scope", "页面尝试跳转到另一个来源，已在访问前停止。")
                route.abort()
                return
            if request.resource_type not in {"document", "script", "stylesheet", "image", "font", "xhr", "fetch"}:
                route.abort()
                return
            self.requests += 1
            if main:
                self.status = 0
                self.documents += 1
                if self.documents > 8:
                    raise PageFailure("unexpected_page", "站点重复导航，未能取得稳定页面。")
            if self.requests > 150:
                raise PageFailure("unexpected_page", "页面加载请求超出本次读取预算。")
            self._respond(route, main)
        except PageFailure as exc:
            self.failure = exc
            route.abort()
        except PlaywrightTimeoutError:
            if main:
                self.failure = self.timed_out()
            if not self.page.is_closed():
                route.abort()
        except Error:
            if main:
                self.failure = PageFailure("site_unreachable", "无法取得入口页响应。请检查地址、网络和证书后重试。")
            if not self.page.is_closed():
                route.abort()

    def _respond(self, route: Route, main: bool) -> None:
        """手工处理有限的同源 HTTP 跳转，保持浏览器地址和最终主文档一致。"""
        url = route.request.url
        for _ in range(8):
            limit = _DOCUMENT_TIMEOUT_MS if main else _RESOURCE_TIMEOUT_MS
            response = route.fetch(url=url, max_redirects=0, max_retries=0, timeout=self.timeout(limit))
            try:
                status = response.status
                location = response.headers.get("location", "")
                if status in {301, 302, 303, 307, 308} and location:
                    try:
                        url = urljoin(url, location)
                    except ValueError:
                        if main:
                            raise PageFailure("unexpected_page", "入口返回了无法解析的重定向地址。") from None
                        route.abort()
                        return
                    if not self.allows(url):
                        if main:
                            raise PageFailure("out_of_scope", "入口重定向到了另一个来源，已在跟随前停止。")
                        route.abort()
                        return
                    if not main:
                        continue
                    # 由主循环在真实目标地址重新导航，站点相对路径和 Vue 路由沿用正确的基址。
                    if "#" not in location:
                        fragment = urlsplit(self.page.url).fragment or self.request_fragment
                        if fragment:
                            url += "#" + fragment
                    self.follow = url
                    route.fulfill(status=200, content_type="text/html", body="<!doctype html>")
                    return
                headers = response.headers
                if main:
                    if status == 401:
                        raise PageFailure("login_required", "服务端返回 HTTP 401，此页面需要认证。")
                    if status == 403:
                        raise PageFailure("access_denied", "服务端返回 HTTP 403，拒绝了本次匿名访问，具体原因未确认。")
                    if not 200 <= status < 300:
                        raise PageFailure("http_error", f"入口页返回 HTTP {status}，请核对站点和路径。")
                    if headers.get("content-type", "").split(";", 1)[0].lower().strip() not in {"text/html", "application/xhtml+xml"}:
                        raise PageFailure("unexpected_page", "入口响应没有提供 HTML 页面。")
                    policy = "sandbox allow-scripts allow-same-origin; form-action 'none'; frame-src 'none'; worker-src 'none'; object-src 'none'; base-uri 'self'"
                    existing = headers.get("content-security-policy")
                    headers["content-security-policy"] = f"{existing}, {policy}" if existing else policy
                    headers["x-dns-prefetch-control"] = "off"
                    self.status = status
                    self.last_document = monotonic()
                route.fulfill(response=response, headers=headers)
                return
            finally:
                response.dispose()
        route.abort()

    def observe(self, url: str) -> LandingResult:
        """打开指定页面并给同源脚本一次短暂渲染时间，仅提取主文档的最少可见证据。"""
        self.page.context.route("**/*", self.handle)
        self.page.context.route_web_socket("**/*", lambda socket: socket.close())
        heading = self.page.locator("h1:visible, [role=heading][aria-level='1']:visible").first
        password = self.page.locator("input[type=password]:visible")
        self.follow = url
        while True:
            if self.failure:
                raise self.failure
            self.timeout()
            if self.follow:
                url, self.follow = self.follow, ""
                self.request_fragment = urlsplit(url).fragment
                try:
                    # 浏览器接收主文档后即可观察 DOM，尾部脚本或样式仍可在后台加载。
                    self.page.goto(url, wait_until="commit", timeout=self.timeout())
                except Error:
                    if self.failure:
                        raise self.failure from None
                    raise
                continue
            if not self.allows(self.page.url):
                raise PageFailure("out_of_scope", "页面已离开获准入口的来源。")
            # 给未绘制标题或表单的应用外壳多一点时间，始终空白的页面仍能返回 HTTP 证据。
            elapsed = monotonic() - self.last_document
            if self.status and elapsed >= 1.0:
                document = self.documents
                titles = heading.all_inner_texts()
                has_password = bool(password.count())
                if titles or has_password or elapsed >= 2.5:
                    title = _public_text(self.page.title())
                    if not (title or titles or has_password) and self.page.evaluate("document.readyState") == "loading":
                        # HTML 解析可能被脚本阻塞；没有可公开文本时继续等本次主文档的渲染结果。
                        self.page.wait_for_timeout(80)
                        continue
                    result = LandingResult(
                        http_status=self.status, title=title,
                        marker=_public_text(titles[0]) if titles else "", password_field=has_password,
                    )
                    if self.failure:
                        raise self.failure
                    if self.status and document == self.documents and not self.follow:
                        return result
            self.page.wait_for_timeout(80)


def read_landing(url: str) -> LandingResult:
    """用全新匿名 Chromium 上下文访问用户给出的入口，返回实际状态与脱敏页面证据。"""
    entry = canonical_entry(url)
    channel = os.environ.get("WEBTEST_ATLAS_BROWSER_CHANNEL", "chrome")
    if channel not in {"chrome", "msedge"}:
        raise PageFailure("browser_error", "浏览器通道应配置为 chrome 或 msedge。")
    try:
        with sync_playwright() as driver, driver.chromium.launch(
            channel=channel, headless=os.environ.get("WEBTEST_ATLAS_HEADED") != "1",
            args=["--no-proxy-server", "--force-webrtc-ip-handling-policy=disable_non_proxied_udp"],
        ) as browser:
            with browser.new_context(offline=True, service_workers="block", accept_downloads=False) as context:
                traffic = _LandingTraffic(context.new_page(), entry)
                try:
                    return traffic.observe(entry)
                except PlaywrightTimeoutError:
                    raise (traffic.failure or traffic.timed_out()) from None
    except (Error, OSError):
        raise PageFailure("browser_error", "Chromium 启动或读取中断，请检查本机浏览器后重试。") from None
