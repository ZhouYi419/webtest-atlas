"""用本地 HTTP 合成站验证首次匿名读取、渲染、地址记忆与请求边界。"""

import json
import socket
from contextlib import contextmanager
from html import escape
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Event, Thread
from time import sleep

import pytest

from webtest_atlas import landing
from webtest_atlas.browser import PageFailure
from webtest_atlas.landing import canonical_entry, read_landing


@contextmanager
def website(routes):
    """按请求路径提供独立合成响应，记录服务器确实收到的方法和路径。"""
    visits = []

    class Handler(BaseHTTPRequestHandler):
        """处理当前测试注册的资源，不与外部服务连接。"""

        def do_GET(self):
            """返回预设的页面或资源，同时记录是否访问未经授权的端点。"""
            visits.append((self.command, self.path))
            response = routes.get(self.path, (404, {}, ""))
            if callable(response):
                response = response()
            if response is None:
                return
            status, headers, body = response
            self.send_response(status)
            for key, value in {"Content-Type": "text/html; charset=utf-8", **headers}.items():
                self.send_header(key, value)
            self.end_headers()
            self.wfile.write(body.encode("utf-8"))

        do_POST = do_GET

        def log_message(self, format, *args):
            """以 visits 提供断言，不打印地址。"""

    with ThreadingHTTPServer(("127.0.0.1", 0), Handler) as server:
        thread = Thread(target=server.serve_forever, kwargs={"poll_interval": .05}, daemon=True)
        thread.start()
        try:
            yield f"http://127.0.0.1:{server.server_port}", visits
        finally:
            server.shutdown()
            thread.join()


def test_canonical_entry_supports_stable_paths_and_hash_routes():
    """部署地址统一域名与默认端口，Vue 路由及中文路径保持可用。"""
    assert canonical_entry("HTTPS://EXAMPLE.TEST:443/#/orders") == "https://example.test/#/orders"
    assert canonical_entry("Example.test/orders") == "https://example.test/orders"
    assert canonical_entry("http://intranet.test:8081") == "http://intranet.test:8081/"
    assert canonical_entry("https://example.test/目录") == "https://example.test/%E7%9B%AE%E5%BD%95"
    assert canonical_entry("http://[::1]:8080/#/") == "http://[::1]:8080/#/"


@pytest.mark.parametrize("url", [
    "file:///tmp/doc", "https://account:FAKE_SECRET@host.test/app",
    "https://example.test/app?search=value", "https://example.test/#/orders?token=FAKE_SECRET",
    "https://example.test/#access_token=FAKE_SECRET", "https://example.test/%2e%2e/private",
    "https://example.test//other/path", "javascript:alert(1)",
])
def test_unsafe_or_parameterized_entry_is_not_persistable(url):
    """带凭据、参数、歧义路径或不支持协议的输入，不回显到失败说明。"""
    with pytest.raises(PageFailure) as result:
        canonical_entry(url)
    assert result.value.code == "invalid_url"
    assert url not in str(result.value)
    assert "FAKE_SECRET" not in str(result.value)


@pytest.mark.browser
def test_renders_same_origin_spa_and_preserves_hash_through_redirect():
    """同源脚本、数据请求、后台入口跳转和 Vue 哈希均交给真实页面渲染。"""
    html = '<meta charset=utf-8><title>入口</title><h1 hidden>隐藏提示</h1><div id=app></div><script src="entry.js?v=1"></script>'
    js = """
        async function mount() {
          const data = await (await fetch('/heading')).json();
          await new Promise(resolve => setTimeout(resolve, 1250));
          document.title = '公开申请后台';
          const h1 = document.createElement('h1');
          h1.textContent = location.hash === '#/orders' ? data.marker : '不是目标页面';
          document.querySelector('#app').append(h1);
        }
        mount();
    """
    routes = {
        "/start": (302, {"Location": "/shell/"}, ""),
        "/shell/": (200, {}, html),
        "/shell/entry.js?v=1": (200, {"Content-Type": "application/javascript; charset=utf-8"}, js),
        "/heading": (200, {"Content-Type": "application/json"}, json.dumps({"marker": "申请列表"})),
    }
    with website(routes) as (origin, visits):
        result = read_landing(origin + "/start#/orders")
    assert result == {"http_status": 200, "title": "公开申请后台", "marker": "申请列表", "password_field": False}
    assert visits == [("GET", path) for path in routes]


@pytest.mark.browser
def test_slow_script_does_not_discard_an_already_visible_landing_page():
    """同源脚本尚未响应时，已显示的首屏标题和 HTTP 状态仍可作为证据。"""
    release = Event()
    wait_expired = Event()

    def pending_script():
        """保持脚本请求待定，等读取结束后释放，给测试失败留出有界退出时间。"""
        if not release.wait(timeout=12):
            wait_expired.set()

    html = '<title>库存看板</title><h1>本周库存</h1><script src="/slow.js"></script>'
    with website({"/": (200, {}, html), "/slow.js": pending_script}) as (origin, visits):
        try:
            result = read_landing(origin)
        finally:
            release.set()
    assert ("GET", "/slow.js") in visits
    assert not wait_expired.is_set()
    assert (result["http_status"], result["title"], result["marker"]) == (200, "库存看板", "本周库存")


@pytest.mark.browser
def test_slow_main_document_can_still_provide_page_evidence():
    """主文档响应稍慢但在首读总预算内时，仍能得到该文档自己的标题。"""
    def slow_document():
        """收到请求后超过原来的六秒限制再发送主文档及响应头。"""
        sleep(6.5)
        return 200, {}, "<title>公共服务</title><h1>办理进度</h1>"

    with website({"/": slow_document}) as (origin, visits):
        result = read_landing(origin)
    assert visits.count(("GET", "/")) == 1
    assert (result["http_status"], result["title"], result["marker"]) == (200, "公共服务", "办理进度")


@pytest.mark.browser
@pytest.mark.parametrize("stall_main", [True, False], ids=["main-document", "render-after-200"])
def test_timeout_reason_identifies_whether_the_main_document_arrived(monkeypatch, stall_main):
    """主文档未响应和收到 HTTP 200 后首屏仍无法渲染时，返回各阶段可核实的原因。"""
    monkeypatch.setattr(landing, "_PAGE_BUDGET_MS", 1400)
    release = Event()

    def hold_response():
        """阻塞主文档或最先执行的脚本，直至读取超时或测试结束。"""
        release.wait(timeout=8)

    blocking_html = '<script src="/slow.js"></script><title>稍后可见</title><h1>稍后可见</h1>'
    routes = {"/": hold_response} if stall_main else {"/": (200, {}, blocking_html), "/slow.js": hold_response}
    with website(routes) as (origin, _):
        try:
            with pytest.raises(PageFailure) as result:
                read_landing(origin)
        finally:
            release.set()
    if stall_main:
        assert result.value.code == "site_unreachable" and "主文档" in str(result.value)
    else:
        assert result.value.code == "render_timeout" and "HTTP 200" in str(result.value)


@pytest.mark.browser
def test_never_sends_form_or_external_requests_and_sees_visible_password():
    """页面自带的写请求、表单或跨源资源不会送达，单纯看到密码框只作为观察值。"""
    with website({}) as (other, external):
        html = f"""
        <title>站点首页</title><h1>账号登录</h1><form action="/submit" method="POST"><input type=password></form>
        <img src="{other}/image"><iframe src="{other}/frame"></iframe>
        <script>fetch('/modify', {{method:'POST'}}).catch(()=>{{}}); document.forms[0].submit();</script>
        """
        with website({"/": (200, {}, html)}) as (origin, visits):
            assert read_landing(origin) == {"http_status": 200, "title": "站点首页", "marker": "账号登录", "password_field": True}
    assert visits == [("GET", "/")]
    assert not external


@pytest.mark.browser
def test_cross_origin_redirect_stops_before_second_site():
    """不同端口代表不同来源，目标服务器不会收到跳转请求。"""
    with website({}) as (other, external):
        with website({"/": (302, {"Location": other + "/?token=FAKE_SECRET"}, "")}) as (origin, _):
            with pytest.raises(PageFailure) as result:
                read_landing(origin)
    assert result.value.code == "out_of_scope" and "FAKE_SECRET" not in str(result.value)
    assert not external


@pytest.mark.browser
def test_client_side_redirects_are_checked_in_the_browser():
    """页面自身的跳转可以落在同源入口，跨源跳转在发给目标服务器前停止。"""
    html = "<script>location.replace('/final')</script>"
    with website({"/": (200, {}, html), "/final": (200, {}, "<h1>最终页面</h1>")}) as (origin, visits):
        assert read_landing(origin)["marker"] == "最终页面"
        assert visits == [("GET", "/"), ("GET", "/final")]
    with website({}) as (other, external):
        with website({"/": (200, {}, f'<meta http-equiv="refresh" content="0;url={other}/next">')}) as (origin, _):
            with pytest.raises(PageFailure) as result:
                read_landing(origin)
    assert result.value.code == "out_of_scope"
    assert not external


@pytest.mark.browser
def test_repeated_main_redirect_has_a_request_limit():
    """重复跳转的入口返回失败，只允许有限次同源请求。"""
    with website({"/": (302, {"Location": "/"}, "")}) as (origin, visits):
        with pytest.raises(PageFailure) as result:
            read_landing(origin)
    assert result.value.code == "unexpected_page"
    assert len(visits) == 8


@pytest.mark.browser
def test_malformed_redirect_does_not_disclose_location():
    """站点给出损坏的跳转地址时按固定失败返回，避免将响应头带入日志或对话。"""
    with website({"/": (302, {"Location": "http://[invalid/FAKE_SECRET"}, "")}) as (origin, _):
        with pytest.raises(PageFailure) as result:
            read_landing(origin)
    assert result.value.code == "unexpected_page" and "FAKE_SECRET" not in str(result.value)


@pytest.mark.browser
@pytest.mark.parametrize("code,expected", [(401, "login_required"), (403, "access_denied"), (404, "http_error")])
def test_actual_http_failures_are_distinct_and_private(code, expected):
    """保留可以确定的实际状态，不将错误页正文或账号权限猜测回传。"""
    with website({"/": (code, {}, "<h1>FAKE_SECRET</h1>")}) as (origin, _):
        with pytest.raises(PageFailure) as result:
            read_landing(origin)
    assert result.value.code == expected and str(code) in str(result.value)
    assert "FAKE_SECRET" not in str(result.value)


@pytest.mark.browser
def test_minimal_evidence_when_title_is_sensitive_and_heading_invisible():
    """原始页面文本不适合公开时，仍保留真实 HTTP 结果和可见字段判断。"""
    html = "<title>ｔｏｋｅｎ samplevalue</title><h1 hidden>个人信息</h1><input hidden type=password>"
    with website({"/": (200, {}, html)}) as (origin, _):
        result = read_landing(origin)
    assert result == {"http_status": 200, "title": "", "marker": "", "password_field": False}


@pytest.mark.browser
@pytest.mark.parametrize("title,heading,safe", [
    ("Shipping & Returns", "News: Updates", True),
    ("Today's Orders", "产品 + 服务", True),
    ("Docs: https://example.invalid", "Public: demo@example.invalid", False),
    ("Account: sample-user", "账号：sample-user", False),
    ("Help: account.example.invalid", "密码：samplevalue", False),
])
def test_public_heading_punctuation_and_private_values(title, heading, safe):
    """两类标题都保留日常标点，网址、邮箱和带值的账号或凭据不进入证据。"""
    html = f"<title>{escape(title)}</title><h1>{escape(heading)}</h1>"
    with website({"/": (200, {}, html)}) as (origin, _):
        result = read_landing(origin)
    assert (result["title"], result["marker"]) == ((title, heading) if safe else ("", ""))


@pytest.mark.browser
def test_entry_unreachable_is_distinct_from_browser_start_error():
    """通过实际未监听的回环端口验证不可达原因。"""
    with socket.socket() as port:
        port.bind(("127.0.0.1", 0))
        with pytest.raises(PageFailure) as result:
            read_landing(f"http://127.0.0.1:{port.getsockname()[1]}")
    assert result.value.code == "site_unreachable"
