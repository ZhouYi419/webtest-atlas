"""从 stdio 进程入口验证工具清单、错误归档及新会话回查。"""

import asyncio
import json
import os
import sys
from contextlib import asynccontextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Event, Thread
from uuid import uuid4

import pytest

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


@asynccontextmanager
async def atlas_session(db):
    """启动一个独立 MCP 子进程并连接指定测试库。"""
    env = {**os.environ, "WEBTEST_ATLAS_DB": str(db), "WEBTEST_ATLAS_SITE_HOME": str(db.parent / "site"),
           "PYTHONPATH": str(Path(__file__).resolve().parents[1] / "src")}
    params = StdioServerParameters(command=sys.executable, args=["-m", "webtest_atlas"], env=env)
    async with stdio_client(params) as (reader, writer), ClientSession(reader, writer) as session:
        await session.initialize()
        yield session


async def call(session, name, arguments):
    """读取 DSH 也可消费的 MCP 文本内容，检查协议状态。"""
    result = await session.call_tool(name, arguments)
    assert not result.isError
    return json.loads(result.content[0].text)


def test_stdio_history(tmp_path):
    """验证工具发现、越界拦截、输入脱敏，以及新进程读取同一原始记录。"""
    async def exercise():
        """从两个独立的协议会话执行和查询同一条测试运行。"""
        db = tmp_path / "history.sqlite3"
        async with atlas_session(db) as first:
            tools = await first.list_tools()
            assert {t.name for t in tools.tools} == {
                "read_demo_page", "login_site", "get_run", "open_site", "update_site_address",
            }
            login = await call(first, "login_site", {})
            assert login["kind"] == "login" and login["status"] == "blocked" and "配置" in login["reason"]
            record = await call(first, "read_demo_page", {"target": "http://127.0.0.1/private?secret=fixture"})
            assert record["status"] == "blocked" and len(record["steps"]) == 2
            assert b"secret=fixture" not in db.read_bytes()
        async with atlas_session(db) as second:
            assert await call(second, "get_run", {"run_id": record["run_id"]}) == record
            assert await call(second, "get_run", {"run_id": login["run_id"]}) == login
            assert (await call(second, "get_run", {"run_id": str(uuid4())}))["status"] == "not_found"
            assert (await call(second, "get_run", {"run_id": "demo"}))["status"] == "invalid_id"
    asyncio.run(exercise())


def test_unexpected_landing_failure_is_terminal_and_retryable(tmp_path, monkeypatch):
    """读取器意外异常时记录脱敏的终态，并允许稍后再次领取同一部署的运行。"""
    from webtest_atlas import __main__ as api

    def interrupted(url):
        """模拟第三方驱动把页面中的私密文字带进异常。"""
        raise ValueError("private fixture data")

    monkeypatch.setenv("WEBTEST_ATLAS_DB", str(tmp_path / "history.sqlite3"))
    monkeypatch.setattr(api, "read_landing", interrupted)
    url = "https://qa.example.org/"
    failed = api.run_open_site(url, "测试")
    assert failed["status"] == "error" and failed["finished_at"]
    assert "private fixture data" not in str(failed)
    retry_id, owner = api.open_store().begin_site_read(url, "测试环境")
    assert owner and retry_id != failed["run_id"]


def test_open_site_reads_live_page_and_preserves_first_result(tmp_path):
    """从两个实际 MCP 进程读取一个新站点，换标题后旧运行仍保留此前的观察。"""
    class Site(BaseHTTPRequestHandler):
        """每次请求读取当前标题，站点的内容变化不依赖工具或模型输入。"""

        title = "库存看板"

        def do_GET(self):
            """返回本次请求时的页面标题和可见一级标题。"""
            body = f"<title>{self.title}</title><h1>待盘点</h1>".encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(body)

    with ThreadingHTTPServer(("127.0.0.1", 0), Site) as server:
        thread = Thread(target=server.serve_forever, daemon=True)
        thread.start()
        url = f"http://127.0.0.1:{server.server_port}/"

        async def exercise():
            """两次从工具入口提交同一目标，并在新的工具进程按旧 ID 取回原结果。"""
            db = tmp_path / "records.sqlite3"
            async with atlas_session(db) as first:
                old = await call(first, "open_site", {"url": url, "environment": "测试"})
                assert old["status"] == "read", old
                assert (old["title"], old["marker"]) == ("库存看板", "待盘点")
                assert old["deployment"]["environment"] == "测试环境"
            Site.title = "新版库存看板"
            async with atlas_session(db) as second:
                new = await call(second, "open_site", {"url": url, "environment": "测试环境"})
                assert new["title"] == Site.title and new["run_id"] != old["run_id"]
                assert new["deployment"] == old["deployment"]
                assert await call(second, "get_run", {"run_id": old["run_id"]}) == old
                declined = await call(second, "open_site", {"url": url + "?token=sample_secret"})
                assert declined["status"] == "blocked"
                assert b"sample_secret" not in db.read_bytes()
                named = await call(second, "open_site", {"site_name": "仓库", "environment": "测试", "url": url})
                assert named["deployment"] == old["deployment"]
            async with atlas_session(db) as third:
                by_name = await call(third, "open_site", {"site_name": "仓库"})
                assert by_name["deployment"] == old["deployment"] and by_name["title"] == Site.title
                revised_url = url + "v2"
                updated = await call(third, "update_site_address", {"site_name": "仓库", "url": revised_url})
                assert updated == {"status": "updated", "site_name": "仓库", "environment": "测试环境",
                                   "previous_url": url, "entry_url": revised_url}
                reopened = await call(third, "open_site", {"site_name": "仓库"})
                assert reopened["status"] == "read" and reopened["deployment"]["entry_url"] == revised_url
                assert await call(third, "get_run", {"run_id": named["run_id"]}) == named
        try:
            asyncio.run(exercise())
        finally:
            server.shutdown()
            thread.join()


def test_stdio_waiting_and_external_confirmation(tmp_path, monkeypatch):
    """等待站点的合成人工确认时，另一 MCP 进程能查原 ID，重复发起也只执行一次。"""
    if sys.platform != "win32":
        pytest.skip("本机测试配置依赖 Windows DPAPI。")
    from examples.demo_server import DEMO_ACCOUNT, DEMO_PASSWORD, DemoHandler, DemoServer
    from webtest_atlas.site_config import demo_config, save_profile

    approved = Event()
    original_get = DemoHandler.do_GET

    def show_approval(handler):
        """模拟站点轮询本人在另一设备上的确认，批准后由站点交换有效会话。"""
        token = handler.cookie_token()
        if handler.path != "/challenge" or handler.server.session_state(token) != "pending":
            return original_get(handler)
        if approved.is_set():
            active = handler.server.issue_session("active", token, confirm=True)
            handler.respond(303, location="/app", token=active)
        else:
            handler.page("等待确认", '<meta http-equiv="refresh" content="1"><p id="challenge">等待本人确认</p>')

    monkeypatch.setattr(DemoHandler, "do_GET", show_approval)
    monkeypatch.setenv("WEBTEST_ATLAS_SITE_HOME", str(tmp_path / "site"))
    monkeypatch.setenv("WEBTEST_ATLAS_HEADED", "0")
    db_path = tmp_path / "history.sqlite3"

    async def exercise():
        """原窗口等待时从第二个工具进程回查，完成后从全新进程读取同一份历史。"""
        async with atlas_session(db_path) as first, atlas_session(db_path) as second:
            initial = await call(first, "login_site", {})
            run_id = initial["run_id"]
            if initial["status"] == "running":
                initial = await wait_for_status(first, run_id, {"waiting"})
            assert initial["status"] == "waiting", initial["reason"]
            assert (await call(second, "login_site", {}))["run_id"] == run_id
            assert (await call(second, "get_run", {"run_id": run_id}))["status"] == "waiting"
            approved.set()
            done = await wait_for_status(second, run_id, {"authenticated"})
            assert [s["status"] for s in done["steps"]] == ["running", "waiting", "authenticated"]
        async with atlas_session(db_path) as reopened:
            assert await call(reopened, "get_run", {"run_id": run_id}) == done

    async def wait_for_status(session, run_id, expected):
        """有界等待原任务的进度，在错误终态时立即报告真实原因。"""
        async with asyncio.timeout(30):
            while True:
                result = await call(session, "get_run", {"run_id": run_id})
                if result["status"] in expected:
                    return result
                assert result["status"] in {"running", "waiting"}, result["reason"]
                await asyncio.sleep(0.2)

    with DemoServer(("127.0.0.1", 0), "challenge", 300, DEMO_ACCOUNT, DEMO_PASSWORD) as site:
        server_thread = Thread(target=site.serve_forever, daemon=True)
        server_thread.start()
        try:
            monkeypatch.setenv("WEBTEST_ATLAS_DEMO_PORT", str(site.server_port))
            save_profile(demo_config(DEMO_ACCOUNT), DEMO_ACCOUNT, DEMO_PASSWORD)
            asyncio.run(exercise())
        finally:
            approved.set()
            site.shutdown()
            server_thread.join()
