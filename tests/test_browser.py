"""用两个独立的本地 HTTP 端点验证 Chromium 的真实页面读取与网络边界。"""

import socket
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread

import pytest
from playwright.sync_api import Error, Page, Route

from webtest_atlas.browser import PageFailure, read_page


pytestmark = pytest.mark.browser
EXAMPLE = "<title>Atlas 示例后台</title><h1 data-atlas-marker>申请列表</h1>"


@contextmanager
def local_page(body: str = EXAMPLE, status: int = 200, **headers: str):
    """提供临时回环页面并记录实际收到的方法、路径，以便断言有无越界请求。"""
    visits = []

    class Handler(BaseHTTPRequestHandler):
        """提供本次测试指定的 HTML、状态和响应头。"""

        def do_GET(self):
            """记录真实 GET 并发送测试响应。"""
            visits.append((self.command, self.path))
            self.send_response(status)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            for key, value in headers.items():
                self.send_header(key, value)
            self.end_headers()
            self.wfile.write(body.encode("utf-8"))

        def do_CONNECT(self):
            """记录可能的代理隧道请求并直接拒绝，测试端点从不连接第三方。"""
            visits.append((self.command, self.path))
            self.send_error(403)

        def log_message(self, format, *args):
            """保持测试安静，由 visits 提供请求记录。"""

    with ThreadingHTTPServer(("127.0.0.1", 0), Handler) as server:
        thread = Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
        thread.start()
        try:
            yield f"http://127.0.0.1:{server.server_port}/", visits
        finally:
            server.shutdown()
            thread.join()


def test_only_reads_main_document():
    """校验真实主文档及可见文本，确认附加资源没有访问第二个端点。"""
    with local_page() as (other, external_visits):
        body = EXAMPLE + f'<img src="{other}asset"><iframe src="{other}frame"></iframe>'
        with local_page(body) as (target, visits):
            assert read_page(target, target) == {"title": "Atlas 示例后台", "marker": "申请列表"}
            assert visits == [("GET", "/")]
        assert external_visits == []


def test_reads_loopback_directly_under_host_proxy(monkeypatch):
    """宿主给模型配置代理时，用真实回环代理确认示例 GET 仍直达站点。"""
    with local_page(status=502) as (proxy, proxy_visits):
        for key in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY"):
            monkeypatch.setenv(key, proxy)
            monkeypatch.setenv(key.lower(), proxy)
        monkeypatch.setenv("NO_PROXY", "")
        monkeypatch.setenv("no_proxy", "")
        monkeypatch.setenv("NODE_USE_ENV_PROXY", "1")
        with local_page() as (target, visits):
            assert read_page(target, target) == {"title": "Atlas 示例后台", "marker": "申请列表"}
            assert visits == [("GET", "/")]
        assert proxy_visits == []


def test_rejects_http_redirect_before_following():
    """确认即使跳转到另一个回环端口，也会在第二个服务器收到请求前拦截。"""
    with local_page() as (other, external_visits):
        with local_page(status=302, Location=other) as (target, visits):
            with pytest.raises(PageFailure) as result:
                read_page(target, target)
            assert result.value.code == "out_of_scope"
            assert visits == [("GET", "/")]
        assert external_visits == []


def test_rejects_page_refresh_before_following():
    """确认声明自动刷新的 HTML 也无法导航到另一个回环端口。"""
    with local_page() as (other, external_visits):
        body = f'<meta http-equiv="refresh" content="0;url={other}">' + EXAMPLE
        with local_page(body) as (target, _):
            with pytest.raises(PageFailure) as result:
                read_page(target, target)
            assert result.value.code == "out_of_scope"
        assert external_visits == []


@pytest.mark.parametrize("suffix", ["private", "?query=demo", "#section"])
def test_mismatched_target_makes_no_request(suffix):
    """地址不完全匹配时，示例站不会收到任何请求。"""
    with local_page() as (allowed, visits):
        with pytest.raises(PageFailure) as result:
            read_page(allowed + suffix, allowed)
        assert result.value.code == "out_of_scope"
        assert not visits


@pytest.mark.parametrize("body", [
    EXAMPLE.replace("申请列表", "合成私有标志"),
    EXAMPLE.replace("<h1 ", "<h1 hidden "),
    EXAMPLE.replace("Atlas 示例后台", "合成私有标题"),
])
def test_refuses_unverified_evidence(body):
    """缺少预期的真实标题或可见标志时，错误信息不会回显原始页面文本。"""
    with local_page(body) as (target, _):
        with pytest.raises(PageFailure) as result:
            read_page(target, target)
        assert result.value.code == "unexpected_page"
        assert "合成私有" not in str(result.value)


def test_reports_unavailable_server():
    """端口已保留但没有监听者时，将错误归为站点不可达。"""
    with socket.socket() as reserved:
        reserved.bind(("127.0.0.1", 0))
        url = f"http://127.0.0.1:{reserved.getsockname()[1]}/"
        with pytest.raises(PageFailure) as result:
            read_page(url, url)
        assert result.value.code == "site_unreachable"
        assert "ECONNREFUSED" in str(result.value)


def test_distinguishes_local_permission_failure(monkeypatch):
    """本机权限拒绝归入执行错误，回传系统错误码并丢弃原始诊断中的敏感文本。"""
    def denied(route, **kwargs):
        """模拟驱动无法打开连接时携带本地诊断附文的错误。"""
        raise Error("connect EACCES synthetic-private-detail")

    monkeypatch.setattr(Route, "fetch", denied)
    with local_page() as (target, visits):
        with pytest.raises(PageFailure) as result:
            read_page(target, target)
        assert result.value.code == "browser_error"
        assert "EACCES" in str(result.value)
        assert "synthetic-private-detail" not in str(result.value)
        assert not visits


def test_reports_browser_configuration_error(monkeypatch):
    """浏览器通道不可用时报告执行错误，示例站不收到请求。"""
    monkeypatch.setenv("WEBTEST_ATLAS_BROWSER_CHANNEL", "unconfigured")
    with local_page() as (target, visits):
        with pytest.raises(PageFailure) as result:
            read_page(target, target)
        assert result.value.code == "browser_error"
        assert not visits


def test_reports_browser_closed_during_navigation(monkeypatch):
    """用真实已关闭的 Chromium 页面区分浏览器中断和示例站不可达。"""
    original_goto = Page.goto

    def close_before_navigation(page, *args, **kwargs):
        """在已创建的标签页关闭后执行原生导航，触发浏览器自身的错误。"""
        page.close()
        return original_goto(page, *args, **kwargs)

    monkeypatch.setattr(Page, "goto", close_before_navigation)
    with local_page() as (target, visits):
        with pytest.raises(PageFailure) as result:
            read_page(target, target)
        assert result.value.code == "browser_error"
        assert not visits
