"""从 stdio 进程入口验证工具清单、错误归档及新会话回查。"""

import asyncio
import json
import os
import sys
from contextlib import asynccontextmanager
from pathlib import Path
from uuid import uuid4

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


@asynccontextmanager
async def atlas_session(db):
    """启动一个独立 MCP 子进程并连接指定测试库。"""
    env = {**os.environ, "WEBTEST_ATLAS_DB": str(db),
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
            assert {t.name for t in tools.tools} == {"read_demo_page", "get_run"}
            record = await call(first, "read_demo_page", {"target": "http://127.0.0.1/private?secret=fixture"})
            assert record["status"] == "blocked" and len(record["steps"]) == 2
            assert b"secret=fixture" not in db.read_bytes()
        async with atlas_session(db) as second:
            assert await call(second, "get_run", {"run_id": record["run_id"]}) == record
            assert (await call(second, "get_run", {"run_id": str(uuid4())}))["status"] == "not_found"
            assert (await call(second, "get_run", {"run_id": "demo"}))["status"] == "invalid_id"
    asyncio.run(exercise())
