"""在用户配置的同站请求范围内登录，并在受保护页面核对当前账号。"""

import os
from collections.abc import Callable
from html import escape
from time import monotonic
from typing import TYPE_CHECKING
from urllib.parse import unquote, unquote_plus, urldefrag, urljoin, urlsplit

from playwright.sync_api import Error, Page, Route, TimeoutError as PlaywrightTimeoutError
from playwright.sync_api import WebSocketRoute, sync_playwright

from .browser import PageFailure

if TYPE_CHECKING:
    from .site_config import SiteProfile


_ACTION_MS = 8_000
_MANUAL_SECONDS = 180
_POLL_MS = 150


class _SiteTraffic:
    """只让登录页的允许请求出网，记下影响本次登录的响应状态。"""

    def __init__(self, page: Page, profile: "SiteProfile"):
        """为单个受控标签页设置请求预算，并阻止其他页面和 WebSocket 访问站点。"""
        self.page = page
        self.profile = profile
        self.failure: PageFailure | None = None
        self.main_status = 0
        self.main_url = ""
        self.main_method = ""
        self.post_status = 0
        self.can_submit = False
        self.submitting_credentials = False
        self.redirects = 0
        self.redirecting = False
        page.context.route("**/*", self.handle)
        page.context.route_web_socket("**/*", self.block_socket)

    def block_socket(self, socket: WebSocketRoute) -> None:
        """关闭页面提出的长连接，只对已配置的普通 HTTP 请求授权。"""
        self.failure = PageFailure("out_of_scope", "站点需要未授权的 WebSocket 连接。请核对登录方式。")
        socket.close(code=1008)

    def check(self) -> None:
        """将路由中的具体失败交回登录流程，并识别人工关闭的目标窗口。"""
        if self.failure:
            raise self.failure
        if self.page.is_closed():
            raise PageFailure("browser_error", "登录窗口已关闭。请重新发起登录。")

    def settle_redirects(self) -> None:
        """等最后一跳真实文档提交到主标签页，避免在上一跳中读取标志或争抢导航。"""
        deadline = monotonic() + _ACTION_MS / 1000
        while self.redirecting:
            self.check()
            if (self.main_status not in (301, 302, 303, 307, 308)
                    and urldefrag(self.page.url).url == self.main_url):
                self.redirecting = False
                break
            if monotonic() >= deadline:
                raise PageFailure("site_unreachable", "等待站点跳转超时。请检查登录入口和受保护页面。")
            self.page.wait_for_timeout(35)

    def handle(self, route: Route) -> None:
        """逐请求检查范围；渲染器离线，未经过此处的请求无法自行连网。"""
        try:
            self.check()
            req = route.request
            if (req.frame != self.page.main_frame
                    or not self.profile.config.allows(req.url, req.method)):
                raise PageFailure("out_of_scope", "已拦截配置范围之外的请求。请核对站点及允许的路径。")
            address = urlsplit(req.url)
            path = unquote(address.path)
            known_paths = (*self.profile.config.read_paths, *self.profile.config.post_paths)
            if (self.profile.password in unquote_plus(address.query)
                    or (path not in known_paths and self.profile.password in path)):
                raise PageFailure("out_of_scope", "表单试图在地址中提交凭据，已拦截。请核对登录表单。")
            if (self.submitting_credentials and not self.post_status and req.is_navigation_request()
                    and req.method == "GET"):
                raise PageFailure("unsupported_login", "登录表单要求通过 GET 地址提交，已拦截。请核对登录方式。")
            if req.method == "POST":
                if not self.can_submit:
                    raise PageFailure("out_of_scope", "已拦截当前步骤未授权的提交。请核对登录流程。")
                self.can_submit = False
            self._respond(route)
        except PageFailure as exc:
            self.failure = exc
            route.abort()
        except Error:
            if self.page.is_closed() or not self.page.context.browser.is_connected():
                self.failure = PageFailure("browser_error", "登录窗口或浏览器进程已关闭。请重新发起登录。")
                return
            self.failure = PageFailure("site_unreachable", "站点请求失败。请检查站点、证书和本机连接后重试。")
            route.abort()

    def _respond(self, route: Route) -> None:
        """取得单跳响应，每次导航跳转都重新经过范围检查，不自动重发密码。"""
        req = route.request
        response = route.fetch(max_redirects=0, max_retries=0, timeout=_ACTION_MS)
        try:
            is_document = req.is_navigation_request()
            if req.method == "POST":
                self.post_status = response.status
            if is_document:
                self.main_status = response.status
                self.main_url = req.url
                self.main_method = req.method
            location = response.headers.get("location")
            if location and response.status in (301, 302, 303, 307, 308):
                target = urljoin(req.url, location)
                if not self.profile.config.allows(target, "GET"):
                    raise PageFailure("out_of_scope", "已在跳转前拦截未授权地址。请核对登录范围。")
                if not is_document or (req.method == "POST" and response.status in (307, 308)):
                    raise PageFailure("unsupported_login", "登录依赖资源跳转或要求重新提交表单。请配置直接入口。")
                self.redirects += 1
                self.redirecting = True
                if self.redirects > 16:
                    raise PageFailure("unsupported_login", "站点重复跳转。请核对登录入口和受保护页面。")
                # 将已校验的 GET 导航交回 Chromium，使后续每一跳仍进入路由钩子。
                target_attr = escape(target, quote=True)
                route.fulfill(status=200, content_type="text/html; charset=utf-8",
                              body=f'<meta http-equiv="refresh" content="0;url={target_attr}">')
                return
            headers = response.headers
            # 服务端通过显式表单和同站资源工作；阻止后台工作线程、插件及额外窗口。
            if is_document:
                policy = ("sandbox allow-forms allow-scripts allow-same-origin; "
                          "worker-src 'none'; object-src 'none'; frame-src 'none'; base-uri 'self'")
                original = headers.get("content-security-policy")
                headers["content-security-policy"] = f"{original}, {policy}" if original else policy
                headers["x-dns-prefetch-control"] = "off"
            route.fulfill(response=response, headers=headers)
        finally:
            response.dispose()


class _LoginSession:
    """以一次密码提交预算完成检查点、表单登录及可选的人工验证。"""

    def __init__(self, page: Page, profile: "SiteProfile", on_waiting: Callable[[], None]):
        """绑定同一可视页面、站点配置和写入等待人工状态的回调。"""
        self.page = page
        self.profile = profile
        self.config = profile.config
        self.on_waiting = on_waiting
        self.traffic = _SiteTraffic(page, profile)
        self.manual_deadline: float | None = None
        page.set_default_timeout(_ACTION_MS)

    def _visible(self, selector: str) -> bool:
        """只识别唯一且可见的标志，空配置表示该标志未提供。"""
        if not selector:
            return False
        locator = self.page.locator(selector)
        return locator.count() == 1 and locator.is_visible()

    def _at(self, url: str) -> bool:
        """按当前同源页面的路径核对入口，忽略登录回跳时附加的查询和片段。"""
        return self.config.at_page(self.page.url, url)

    def _check_denial(self) -> None:
        """识别服务端拒绝、授权越界和表单明确给出的凭据错误。"""
        self.traffic.check()
        self.traffic.settle_redirects()
        if self._visible(self.config.invalid_selector) or self.traffic.post_status == 401:
            raise PageFailure("invalid_credentials", "站点拒绝了登录凭据。请在本机配置中更新账号密码后重试。")
        if 403 in (self.traffic.main_status, self.traffic.post_status):
            raise PageFailure("permission_denied", "站点拒绝访问。请核对测试账号对受保护页面的权限。")
        if self.traffic.main_status >= 400 and self.traffic.main_status != 401:
            raise PageFailure("site_unreachable", "站点未正常提供配置的页面。请检查服务和页面地址。")
        if self.traffic.post_status >= 400:
            raise PageFailure("login_blocked", "站点拒绝了登录请求。请检查表单配置和站点状态。")

    def _account_matches(self) -> bool:
        """仅在明确的保护页上读取账号标志，身份不符时保留原会话文件。"""
        # 页面脚本可能只切换地址栏；核对实际收到的文档及浏览器当前地址。
        if not all(self.config.at_page(url, self.config.protected_url)
                   and not urlsplit(url).query and not urlsplit(url).fragment
                   for url in (self.page.url, self.traffic.main_url)):
            return False
        if self.traffic.main_method != "GET" or not 200 <= self.traffic.main_status < 300:
            return False
        if not self._visible(self.config.account_selector):
            return False
        if self.page.locator(self.config.account_selector).inner_text().strip() != self.config.account_text:
            raise PageFailure("wrong_account", "受保护页面显示的账号与配置不符。请核对账号标志并重新配置。")
        return True

    def _visit(self, url: str) -> None:
        """实际请求站点页面并完成主文档解析，将被拦导航还原为可处理的原因。"""
        self.traffic.settle_redirects()
        previous_redirects = self.traffic.redirects
        try:
            self.page.goto(url, wait_until="commit", timeout=_ACTION_MS)
        except Error as exc:
            self.traffic.check()
            # Chromium 有时会在 goto 返回前启动已核准的下一跳，原调用会报告被新导航中断。
            expected_redirect = self.traffic.redirects > previous_redirects
            superseded = "interrupted by another navigation" in str(exc) or "net::ERR_ABORTED" in str(exc)
            if not expected_redirect or not superseded:
                raise
        self.traffic.settle_redirects()
        self.page.wait_for_load_state("domcontentloaded", timeout=_ACTION_MS)
        self.traffic.check()

    def _wait_challenge(self) -> None:
        """通知 DSH 后保留当前窗口，等本人完成可见挑战、关闭窗口或者超时。"""
        if self.manual_deadline is None:
            self.manual_deadline = monotonic() + _MANUAL_SECONDS
            self.on_waiting()
        self.traffic.can_submit = True
        while self._visible(self.config.challenge_selector):
            self._check_denial()
            if monotonic() >= self.manual_deadline:
                raise PageFailure("manual_timeout", "等待人工验证已超时。请重新发起登录并在浏览器完成验证。")
            self.page.wait_for_timeout(_POLL_MS)
        self.traffic.can_submit = False
        self.traffic.check()

    def _checkpoint(self) -> bool:
        """读取真实受保护页面，审查会话身份并识别回跳登录页、过期或权限不足。"""
        self._visit(self.config.protected_url)
        deadline = monotonic() + _ACTION_MS / 1000
        while True:
            self._check_denial()
            if self._account_matches():
                return True
            if self._visible(self.config.challenge_selector):
                # 获取新文档的可见挑战之后再判别表单，避免跳转期间混用前一页地址。
                if self._visible(self.config.password_selector) and self._at(self.config.login_url):
                    return False
                self._wait_challenge()
                self._visit(self.config.protected_url)
            elif (self.traffic.main_status == 401 or self._at(self.config.login_url)
                    or (self._at(self.config.protected_url) and
                        (urlsplit(self.page.url).query or urlsplit(self.page.url).fragment))
                    or self._visible(self.config.password_selector) or monotonic() >= deadline):
                return False
            self.page.wait_for_timeout(_POLL_MS)

    def authenticate(self, had_state: bool) -> str:
        """先检查登录态，只在需要时提交一次凭据，再次读取保护页以验证结果。"""
        if self._checkpoint():
            if had_state:
                return "已复用有效登录态，受保护页面与测试账号均已核对。"
            if self.manual_deadline is not None:
                return "已完成人工登录并核对受保护页面与测试账号。"
            raise PageFailure("public_checkpoint", "匿名访问即可看到配置的账号标志。请改用需要登录的受保护页面。")
        self._visit(self.config.login_url)
        if self._visible(self.config.challenge_selector) and not self._visible(self.config.password_selector):
            self._wait_challenge()
            if self._checkpoint():
                return "已完成人工登录并核对受保护页面与测试账号。"
            self._visit(self.config.login_url)
        self._check_denial()
        if not self._at(self.config.login_url):
            raise PageFailure("unsupported_login", "登录表单地址与配置不符。请配置表单实际所在的同站地址。")
        try:
            self.page.locator(self.config.username_selector).fill(self.profile.username)
            self.page.locator(self.config.password_selector).fill(self.profile.password)
            self.traffic.submitting_credentials = True
            if self._visible(self.config.challenge_selector):
                self._wait_challenge()
            # 合并表单由本人提交时，后续只核对结果；答案就绪但尚未提交时由工具点击一次。
            if not self.traffic.post_status:
                self.traffic.can_submit = True
                self.page.locator(self.config.submit_selector).click()
        except Error:
            self.traffic.check()
            raise PageFailure("unsupported_login", "无法操作配置的登录表单。请检查账号、密码和提交按钮的定位。") from None

        # 给异步表单时间展示挑战或错误；成功跳到普通首页时仍重新核对保护页。
        deadline = monotonic() + _ACTION_MS / 1000
        while True:
            self._check_denial()
            if self._visible(self.config.challenge_selector):
                self._wait_challenge()
                break
            if self._account_matches():
                break
            if self.traffic.post_status and not self._at(self.config.login_url) and 200 <= self.traffic.main_status < 300:
                break
            if monotonic() >= deadline:
                break
            self.page.wait_for_timeout(_POLL_MS)
        self.traffic.can_submit = False
        self.traffic.submitting_credentials = False
        if not self._checkpoint():
            raise PageFailure("checkpoint_missing", "提交后仍无法核对受保护页面上的账号。请检查登录方式及账号标志。")
        if had_state:
            return "原登录态已失效，已重新登录并核对受保护页面与测试账号。"
        return "已首次登录并核对受保护页面与测试账号。"


def execute_login(profile: "SiteProfile", on_waiting: Callable[[], None]) -> str:
    """打开隔离的 Chromium 登录窗口，仅在账号核对成功后保存加密会话。"""
    channel = os.getenv("WEBTEST_ATLAS_BROWSER_CHANNEL", "chrome")
    if channel not in {"chrome", "msedge"}:
        raise PageFailure("browser_error", "浏览器通道应配置为 chrome 或 msedge。")
    state = profile.load_state()
    try:
        with sync_playwright() as driver, driver.chromium.launch(
            channel=channel, headless=os.getenv("WEBTEST_ATLAS_HEADED", "1") == "0",
            args=["--no-proxy-server", "--force-webrtc-ip-handling-policy=disable_non_proxied_udp"],
        ) as browser:
            with browser.new_context(storage_state=state, offline=True, accept_downloads=False,
                                     service_workers="block") as context:
                session = _LoginSession(context.new_page(), profile, on_waiting)
                try:
                    result = session.authenticate(state is not None)
                    session.traffic.check()
                    profile.save_state(context.storage_state(indexed_db=True))
                    return result
                except Error:
                    session.traffic.check()
                    raise
    except PlaywrightTimeoutError:
        raise PageFailure("site_unreachable", "等待站点响应超时。请检查站点和配置后重试。") from None
    except Error:
        raise PageFailure("browser_error", "登录窗口或浏览器执行中断。请检查本机浏览器后重试。") from None
