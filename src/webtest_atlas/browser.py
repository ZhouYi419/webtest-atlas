"""在临时 Chromium 上下文中读取唯一获准的本地合成页面。"""

import os
import re

from playwright.sync_api import Browser, Error, Route, TimeoutError as PlaywrightTimeoutError, sync_playwright


class PageFailure(Exception):
    """携带可保存的分类与可向用户展示的脱敏原因。"""

    def __init__(self, code: str, message: str) -> None:
        """为页面操作记录失败类别和安全提示。"""
        super().__init__(message)
        self.code = code


def _request_failure(error: Error) -> PageFailure:
    """从驱动错误中提取允许公开的网络诊断，不输出调用栈、URL 或请求头。"""
    message = str(error)
    if isinstance(error, PlaywrightTimeoutError) or "Timeout " in message or "timed out" in message.lower():
        return PageFailure("site_unreachable", "示例站响应超时。请检查服务后重试。")
    # 仅提取操作系统提供的固定标识，其他驱动故障不能推断为站点不可达。
    code = re.search(r"\b(ECONNREFUSED|ECONNRESET|ETIMEDOUT|EHOSTUNREACH|ENETUNREACH|"
                     r"EACCES|EPERM|EBUSY|EMFILE|ENFILE|ENOBUFS|EADDRNOTAVAIL)\b", message)
    if code:
        value = code[1]
        if value in {"ECONNREFUSED", "ECONNRESET", "ETIMEDOUT", "EHOSTUNREACH", "ENETUNREACH"}:
            return PageFailure("site_unreachable", f"未能连接示例站（{value}）。请检查服务和端口后重试。")
        return PageFailure("browser_error", f"本机读取进程无法建立连接（{value}）。请检查权限及系统资源。")
    return PageFailure("browser_error", "Playwright 读取主文档失败。请检查本机驱动与运行环境后重试。")


def read_page(target: str, allowed_url: str) -> dict[str, str]:
    """验证目标后打开临时浏览器，返回实际标题和可见的合成页面标志。"""
    # 字面匹配拒绝凭据、查询、片段及其他路径；只允许显式回环端口。
    address = re.fullmatch(r"http://127\.0\.0\.1:([1-9][0-9]{0,4})/", allowed_url)
    if target != allowed_url or not address or int(address[1]) > 65535:
        raise PageFailure("out_of_scope", "只允许读取已配置的本地示例站首页。")
    # Chromium 序列化 URL 时会移除 HTTP 默认端口。
    browser_url = "http://127.0.0.1/" if address[1] == "80" else allowed_url
    channel = os.environ.get("WEBTEST_ATLAS_BROWSER_CHANNEL", "chrome")
    if channel not in {"chrome", "msedge"}:
        raise PageFailure("browser_error", "浏览器通道应配置为 chrome 或 msedge。")

    try:
        with sync_playwright() as driver:
            with driver.chromium.launch(
                channel=channel,
                headless=os.environ.get("WEBTEST_ATLAS_HEADED") != "1",
                args=["--no-proxy-server"],
            ) as browser:
                return _read_document(browser, browser_url)
    except (Error, OSError) as exc:
        raise PageFailure("browser_error", "Chromium 启动或读取中断，请检查浏览器后重试。") from exc


def _read_document(browser: Browser, url: str) -> dict[str, str]:
    """只向获准地址请求一次主文档，在 Chromium 渲染后提取经过校验的文本。"""
    context = browser.new_context(
        java_script_enabled=False,
        service_workers="block",
        accept_downloads=False,
        # 所有主文档通过路由读取并交付；渲染器不需要独立连接网络。
        offline=True,
    )
    page = context.new_page()
    requested = False
    failure: PageFailure | None = None

    def route_document(route: Route) -> None:
        """中止附属请求和跳转，将获准主文档的真实 HTML 交给 Chromium。"""
        nonlocal requested, failure
        request = route.request
        if not request.is_navigation_request() or request.frame != page.main_frame:
            route.abort()
            return
        if (requested or request.url != url or request.method != "GET"
                or request.resource_type != "document"):
            failure = PageFailure("out_of_scope", "示例页尝试额外导航，已拦截。")
            route.abort()
            return
        requested = True
        try:
            # 直接放行时浏览器可能跟随未再次触发路由的 HTTP 跳转。
            response = route.fetch(max_redirects=0, timeout=5_000)
        except Error as exc:
            if browser.is_connected() and not page.is_closed():
                failure = _request_failure(exc)
            route.abort()
            return
        try:
            if 300 <= response.status < 400 or "refresh" in response.headers:
                failure = PageFailure("out_of_scope", "示例站请求跳转，已在跟随前拦截。")
            elif response.status != 200:
                failure = PageFailure("site_unreachable", f"示例站返回 HTTP {response.status}，请检查服务后重试。")
            elif response.headers.get("content-type", "").split(";")[0].strip().lower() != "text/html":
                failure = PageFailure("unexpected_page", "示例站首页未返回 HTML，请核对服务。")
            if failure:
                route.abort()
                return
            # 仅保留用于本地静态示例的 HTML；屏蔽子资源、脚本、刷新导航及响应头副作用。
            route.fulfill(
                status=200,
                headers={
                    "content-type": "text/html; charset=utf-8",
                    "content-security-policy": (
                        "sandbox; default-src 'none'; style-src 'unsafe-inline'; "
                        "base-uri 'none'; form-action 'none'"
                    ),
                    "x-dns-prefetch-control": "off",
                },
                body=response.body(),
            )
        finally:
            response.dispose()

    context.route("**/*", route_document)
    try:
        page.goto(url, wait_until="load", timeout=12_000)
    except Error:
        if failure:
            raise failure from None
        raise
    if failure:
        raise failure
    if page.locator("meta[http-equiv='refresh' i]").count():
        raise PageFailure("out_of_scope", "示例页包含自动跳转，已拦截。")
    marker = page.locator("[data-atlas-marker]")
    title = page.title()
    if title == "Atlas 示例后台" and marker.count() == 1 and marker.is_visible():
        visible_text = marker.inner_text().strip()
        if visible_text == "申请列表":
            return {"title": title, "marker": visible_text}
    raise PageFailure("unexpected_page", "页面标题或可见标志与合成示例不符，请核对服务。")
