"""从示例后台的 HTTP 入口核对真实登录、人工确认、权限和会话失效。"""

from contextlib import contextmanager
from http.client import HTTPConnection
from http.cookies import SimpleCookie
from threading import Thread
from types import SimpleNamespace
from urllib.parse import urlencode

import pytest
from playwright.sync_api import sync_playwright

from examples import demo_server as demo


@contextmanager
def demo_site(mode="normal"):
    """启动独立的回环示例站，结束时关闭线程和监听端口。"""
    with demo.DemoServer(("127.0.0.1", 0), mode, 300, demo.DEMO_ACCOUNT, demo.DEMO_PASSWORD) as site:
        thread = Thread(target=site.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
        thread.start()
        try:
            yield site
        finally:
            site.shutdown()
            thread.join()


def request(site, path, fields=None, cookie=""):
    """提交真实请求，保留实际重定向地址、会话 Cookie 与页面正文供断言。"""
    headers = {"Cookie": cookie}
    body = None
    if fields is not None:
        headers["Content-Type"] = "application/x-www-form-urlencoded"
        body = urlencode(fields)
    client = HTTPConnection("127.0.0.1", site.server_port, timeout=3)
    try:
        client.request("GET" if body is None else "POST", path, body, headers)
        response = client.getresponse()
        cookies = SimpleCookie(response.getheader("Set-Cookie", ""))
        session = cookies.output(attrs=[], header="").strip()
        return response.status, response.getheader("Location"), session, response.read().decode()
    finally:
        client.close()


def test_authenticated_page_and_session_expiration(monkeypatch):
    """公开页不检查账号；受保护页拒绝未登录和过期会话，错密可重试。"""
    with demo_site() as site:
        assert "data-atlas-marker" in request(site, "/")[3]
        assert request(site, "/app")[:2] == (302, "/login")
        result = request(site, "/login", {"username": demo.DEMO_ACCOUNT, "password": "incorrect"})
        assert result[0] == 401 and 'id="login-error"' in result[3] and not result[2]
        status, path, cookie, _ = request(site, "/login", {"username": demo.DEMO_ACCOUNT, "password": demo.DEMO_PASSWORD})
        assert (status, path) == (303, "/app")
        result = request(site, "/app", cookie=cookie)
        assert result[0] == 200 and f'id="account">{demo.DEMO_ACCOUNT}<' in result[3]
        # 只拨动示例站模块的时钟，发送仍然携带旧 Cookie 的真实 HTTP 请求。
        expired = demo.time.monotonic() + site.lifetime + 1
        monkeypatch.setattr(demo, "time", SimpleNamespace(monotonic=lambda: expired))
        assert request(site, "/app", cookie=cookie)[:2] == (302, "/login")


def test_human_challenge_pending_cookie_is_single_use():
    """完成表单前不能访问受保护页，完成后旧的待确认 Cookie 无法再次换取有效登录态。"""
    with demo_site("challenge") as site:
        fields = {"username": demo.DEMO_ACCOUNT, "password": demo.DEMO_PASSWORD}
        status, path, pending, _ = request(site, "/login", fields)
        assert (status, path) == (303, "/challenge")
        assert request(site, "/app", cookie=pending)[:2] == (302, "/challenge")
        assert 'id="challenge"' in request(site, "/challenge", cookie=pending)[3]
        assert request(site, "/challenge", {"confirm": "yes"})[:2] == (302, "/login")
        status, path, active, _ = request(site, "/challenge", {"confirm": "yes"}, pending)
        assert (status, path) == (303, "/app") and active and active != pending
        assert request(site, "/app", cookie=active)[0] == 200
        assert request(site, "/challenge", {"confirm": "yes"}, pending)[:2] == (302, "/login")


def test_forbidden_after_successful_login():
    """权限不足的账号能完成身份核验，但无权读取受保护页面及账号检查点。"""
    with demo_site("forbidden") as site:
        status, path, cookie, _ = request(site, "/login", {"username": demo.DEMO_ACCOUNT, "password": demo.DEMO_PASSWORD})
        assert (status, path) == (303, "/app")
        denied = request(site, path, cookie=cookie)
        assert denied[0] == 403 and 'id="account"' not in denied[3]


@pytest.mark.browser
def test_chromium_same_origin_form_submission():
    """使用原生表单导航验证同源请求头、人工确认按钮和受保护页面的账号检查点。"""
    with demo_site("challenge") as site, sync_playwright() as driver:
        with driver.chromium.launch(channel="chrome", args=["--no-proxy-server"]) as browser:
            page = browser.new_page(java_script_enabled=False)
            origin = f"http://127.0.0.1:{site.server_port}"
            page.goto(origin + "/app")
            page.locator("#username").fill(demo.DEMO_ACCOUNT)
            page.locator("#password").fill(demo.DEMO_PASSWORD)
            page.get_by_role("button", name="登录").click()
            page.wait_for_url(origin + "/challenge", timeout=5_000)
            page.locator("#challenge").get_by_role("button", name="完成人工确认").click()
            page.wait_for_url(origin + "/app", timeout=5_000)
            assert page.locator("#account").inner_text() == demo.DEMO_ACCOUNT
