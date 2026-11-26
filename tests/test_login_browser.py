"""通过真实 Chromium 和合成站的服务端会话核对登录、人工确认和请求范围。"""

from dataclasses import replace
from threading import Thread
from types import SimpleNamespace
from urllib.parse import parse_qs

import pytest
from playwright.sync_api import Page

from examples.demo_server import DEMO_ACCOUNT, DEMO_PASSWORD, DemoHandler, DemoServer
from webtest_atlas import login_browser
from webtest_atlas.browser import PageFailure
from webtest_atlas.login_browser import execute_login
from webtest_atlas.site_config import SiteConfig, demo_config


pytestmark = pytest.mark.browser


@pytest.fixture
def site(monkeypatch):
    """启动独立的合成站并记录登录次数，以内存中的会话快照隔离本机真实配置。"""
    posts = []
    received = DemoHandler.do_POST

    def count_post(handler):
        """仅记录提交路径，账号密码不进入路径计数。"""
        posts.append(handler.path)
        received(handler)

    monkeypatch.setattr(DemoHandler, "do_POST", count_post)
    monkeypatch.setenv("WEBTEST_ATLAS_HEADED", "0")
    with DemoServer(("127.0.0.1", 0), "normal", 3600, DEMO_ACCOUNT, DEMO_PASSWORD) as server:
        thread = Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
        thread.start()
        monkeypatch.setenv("WEBTEST_ATLAS_DEMO_PORT", str(server.server_port))
        state = {}
        profile = SimpleNamespace(config=demo_config(DEMO_ACCOUNT), username=DEMO_ACCOUNT,
                                  password=DEMO_PASSWORD, load_state=lambda: state.get("snapshot"),
                                  save_state=lambda value: state.update(snapshot=value))
        try:
            yield profile, server, posts
        finally:
            server.shutdown()
            thread.join()


def no_challenge():
    """没有开启挑战的用例不能进入人工等待状态。"""
    pytest.fail("意外触发人工验证")


def test_login_reuse_and_server_expiry(site):
    """真实表单只提交一次，新浏览器复用会话，服务端撤销会话后才再次登录。"""
    profile, server, posts = site
    assert "首次登录" in execute_login(profile, no_challenge)
    assert posts == ["/login"]
    assert "复用" in execute_login(profile, no_challenge)
    assert posts == ["/login"]
    with server.lock:
        server.sessions.clear()
    assert "重新登录" in execute_login(profile, no_challenge)
    assert posts == ["/login", "/login"]


def test_user_selected_site_logs_in_and_reuses_session(monkeypatch):
    """用另一套站点地址、表单字段和账号显示名验证用户自选站点可以登录并复用。"""
    username, password = "shift-qa", "synthetic-shift-pass"
    display_name = "排班测试员"
    login_path, submit_path, protected_path = "/auth/sign-in", "/auth/session", "/backoffice/roster"
    requests = []

    class RosterHandler(DemoHandler):
        """用排班后台自己的表单和页面标志验证身份，复用合成站的服务端会话。"""

        def do_GET(self):
            """未认证的值班表引导登录，有效 Cookie 可查看当前操作员的显示名。"""
            requests.append(("GET", self.path))
            if self.path == login_path:
                self.page("操作员登录", f'<form action="{submit_path}" method="post">'
                          '<input name="badge"><input type="password" name="passphrase">'
                          '<button class="enter-roster">进入排班</button></form>')
            elif self.path != protected_path:
                self.respond(404)
            elif self.server.session_state(self.cookie_token()) == "active":
                self.page("值班表", f'<span data-current-operator>{display_name}</span>')
            else:
                self.respond(302, location=login_path)

        def do_POST(self):
            """仅在独立的会话提交入口核对合成工号和口令，成功后签发服务端会话。"""
            requests.append(("POST", self.path))
            if self.path != submit_path:
                self.respond(404)
                return
            fields = parse_qs(self.rfile.read(int(self.headers["Content-Length"])).decode())
            if fields != {"badge": [username], "passphrase": [password]}:
                self.respond(401)
                return
            token = self.server.issue_session("active", self.cookie_token())
            self.respond(303, location=protected_path, token=token)

    monkeypatch.setenv("WEBTEST_ATLAS_HEADED", "0")
    with DemoServer(("127.0.0.1", 0), "normal", 3600, username, password) as server:
        server.RequestHandlerClass = RosterHandler
        origin = f"http://127.0.0.1:{server.server_port}"
        config = SiteConfig(environment="排班测试环境", login_url=origin + login_path,
                            protected_url=origin + protected_path, post_paths=(submit_path,),
                            username_selector='[name="badge"]', password_selector='[name="passphrase"]',
                            submit_selector=".enter-roster", account_selector="[data-current-operator]",
                            account_text=display_name)
        state = {}
        profile = SimpleNamespace(config=config, username=username, password=password,
                                  load_state=lambda: state.get("snapshot"),
                                  save_state=lambda snapshot: state.update(snapshot=snapshot))
        thread = Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
        thread.start()
        try:
            assert "首次登录" in execute_login(profile, no_challenge)
            assert [path for method, path in requests if method == "POST"] == [submit_path]
            requests.clear()
            assert "复用" in execute_login(profile, no_challenge)
            assert requests == [("GET", protected_path)]
        finally:
            server.shutdown()
            thread.join()


@pytest.mark.parametrize("mode,expected", [
    ("invalid", "invalid_credentials"), ("forbidden", "permission_denied"), ("wrong_user", "wrong_account"),
])
def test_login_denial_preserves_secret_and_budget(site, mode, expected):
    """错密、缺权和账号不一致分别报告；不保存会话，也不重复提交或回显输入。"""
    profile, server, posts = site
    if mode == "invalid":
        profile.password = "合成的错误口令"
    elif mode == "forbidden":
        server.mode = "forbidden"
    else:
        profile.config = replace(profile.config, account_text="合成的另一账号")
    with pytest.raises(PageFailure) as error:
        execute_login(profile, no_challenge)
    assert error.value.code == expected
    assert posts == ["/login"]
    assert profile.load_state() is None
    assert profile.password not in str(error.value) and DEMO_ACCOUNT not in str(error.value)


def test_human_challenge_resumes_same_window(site, monkeypatch):
    """模拟本人点击浏览器里的挑战按钮，验证等待提示及原运行继续核对账号。"""
    profile, server, posts = site
    server.mode = "challenge"
    waiting = []
    completed = []
    original_wait = Page.wait_for_timeout

    def complete_visible_challenge(page, timeout):
        """在等待状态验证站点的按钮可见，并通过页面真实表单确认。"""
        if waiting and not completed and page.locator("#challenge").is_visible():
            completed.append(page.url)
            page.locator("#challenge button").click()
        original_wait(page, timeout)

    monkeypatch.setattr(Page, "wait_for_timeout", complete_visible_challenge)
    assert "首次登录" in execute_login(profile, lambda: waiting.append(True))
    assert waiting == [True] and len(completed) == 1
    assert posts == ["/login", "/challenge"]


def test_wait_timeout_preserves_original_state(site, monkeypatch):
    """等待超时明确结束，不会把待人工确认的 Cookie 缓存为成功登录。"""
    profile, server, posts = site
    server.mode = "challenge"
    waiting = []
    monkeypatch.setattr(login_browser, "_MANUAL_SECONDS", 0)
    with pytest.raises(PageFailure) as error:
        execute_login(profile, lambda: waiting.append(True))
    assert error.value.code == "manual_timeout" and waiting == [True]
    assert posts == ["/login"] and profile.load_state() is None


def test_combined_challenge_form_is_filled_once(site, monkeypatch):
    """验证码与密码同表单时，先填账号密码，再由本人完成挑战和唯一一次提交。"""
    profile, _, posts = site
    waiting = []
    completed = []
    original_wait = Page.wait_for_timeout

    def combined_form(handler, invalid=False):
        """在合成站的登录表单中加入必填的人工输入框。"""
        handler.page("登录", '<form action="/login" method="post">'
                     '<input id="username" name="username"><input id="password" type="password" name="password">'
                     '<input id="challenge" name="answer" required><button type="submit">登录</button></form>')

    def complete_combined_form(page, timeout):
        """模拟本人只填写挑战答案并提交，在此之前用户名和密码应已由工具填写。"""
        if waiting and not completed and page.locator("#challenge").is_visible():
            completed.append(True)
            assert page.locator("#username").input_value() == DEMO_ACCOUNT
            assert page.locator("#password").input_value() == DEMO_PASSWORD
            page.locator("#challenge").fill("synthetic")
            page.locator("button[type=submit]").click()
        original_wait(page, timeout)

    monkeypatch.setattr(DemoHandler, "login_form", combined_form)
    monkeypatch.setattr(Page, "wait_for_timeout", complete_combined_form)
    assert "首次登录" in execute_login(profile, lambda: waiting.append(True))
    assert waiting == [True] and completed == [True] and posts == ["/login"]


def test_stops_unapproved_redirect_before_request(site, monkeypatch):
    """先经过允许的跳转，再由获准入口跳往越界路径，验证越界请求未到达服务端。"""
    profile, _, posts = site
    reads = []
    original_get = DemoHandler.do_GET

    def redirect_login(handler):
        """让正常登录页再跳一次，保留收到的真实路径用于验证。"""
        reads.append(handler.path)
        if handler.path == "/login":
            handler.respond(302, location="/outside")
        else:
            original_get(handler)

    monkeypatch.setattr(DemoHandler, "do_GET", redirect_login)
    with pytest.raises(PageFailure) as error:
        execute_login(profile, no_challenge)
    assert error.value.code == "out_of_scope"
    assert "/app" in reads and "/login" in reads and "/outside" not in reads and not posts


def test_get_form_never_sends_password(site, monkeypatch):
    """表单拼接查询字符串时，站点只收到原始页面访问，密码没有出现在请求地址。"""
    profile, _, posts = site
    received = []
    original_get = DemoHandler.do_GET

    def get_form(handler):
        """返回可实际填写的 GET 表单并记录收到的测试请求。"""
        received.append(handler.path)
        if handler.path == "/login":
            handler.page("登录", '<form method="get" action="/login"><input id="username" name="u">'
                         '<input type="password" id="password" name="p"><button type="submit">登录</button></form>')
        else:
            original_get(handler)

    monkeypatch.setattr(DemoHandler, "do_GET", get_form)
    with pytest.raises(PageFailure) as error:
        execute_login(profile, no_challenge)
    assert error.value.code in {"out_of_scope", "unsupported_login"}
    assert not posts and all("?" not in path for path in received)
    assert profile.password not in str(error.value)


def test_anonymous_account_marker_is_not_authentication(site, monkeypatch):
    """匿名用户可读的账号文字不能作为本次登录成功的证明。"""
    profile, _, posts = site

    def public_checkpoint(handler):
        """模拟选错了公开页面，匿名首访就展示完全匹配的账号文本。"""
        handler.page("公开页", f'<p id="account">{DEMO_ACCOUNT}</p>')

    monkeypatch.setattr(DemoHandler, "do_GET", public_checkpoint)
    with pytest.raises(PageFailure) as error:
        execute_login(profile, no_challenge)
    assert error.value.code == "public_checkpoint" and not posts
    assert profile.load_state() is None


def test_different_query_cannot_pass_checkpoint(site, monkeypatch):
    """同路径的预览页可用独立查询参数装载，但不能充当指定的保护页。"""
    profile, _, posts = site
    original_get = DemoHandler.do_GET

    def preview(handler):
        """将真正的保护页跳往同路径预览并在预览中展示指定用户。"""
        if handler.path == "/app":
            handler.respond(302, location="/app?preview=yes")
        elif handler.path == "/app?preview=yes":
            handler.page("预览", f'<p id="account">{DEMO_ACCOUNT}</p>')
        else:
            original_get(handler)

    monkeypatch.setattr(DemoHandler, "do_GET", preview)
    monkeypatch.setattr(login_browser, "_ACTION_MS", 1500)
    with pytest.raises(PageFailure) as error:
        execute_login(profile, no_challenge)
    assert error.value.code == "checkpoint_missing"
    assert posts == ["/login"] and profile.load_state() is None
