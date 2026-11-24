"""以 MCP stdio 提供示例页读取和跨会话历史查询。"""

import asyncio
import os
import sqlite3
from pathlib import Path
from uuid import UUID

from mcp.server.fastmcp import FastMCP

from .browser import PageFailure, read_page
from .store import Store


mcp = FastMCP("atlas")


def open_store() -> Store:
    """打开本项目运行库，使不同 DSH 聊天可按同一个 ID 查询。"""
    return Store(Path(os.environ.get("WEBTEST_ATLAS_DB", ".local/atlas.sqlite3")))


def demo_url() -> str:
    """将运维配置的端口转换为本轮唯一获准的回环首页。"""
    port = int(os.environ.get("WEBTEST_ATLAS_DEMO_PORT", "18765"))
    if not 1 <= port <= 65535:
        raise ValueError("示例站端口无效")
    return f"http://127.0.0.1:{port}/"


def run_read(target: str | None) -> dict:
    """登记运行并执行获准的读取；目标相同的并发请求复用执行中的运行。"""
    store = open_store()
    try:
        allowed_url = demo_url()
    except ValueError:
        allowed_url = None
    # 仅将项目自有地址作为去重键。用户输入的越界目标不进入库和步骤记录。
    key = allowed_url if target is None or target == allowed_url else None
    run_id, should_execute = store.begin_read(key)
    try:
        if should_execute:
            finish_read(store, run_id, target, allowed_url)
        record = store.get(run_id)
        if record is None:
            return {"run_id": run_id, "status": "error", "reason": "原运行记录已不可用。请核对运行库。"}
        return record
    except (sqlite3.Error, OSError):
        return {"run_id": run_id, "status": "error",
                "reason": "运行库读写失败。请检查空间与权限，重启工具后用此 ID 核对记录。"}


def finish_read(store: Store, run_id: str, target: str | None, allowed_url: str | None) -> None:
    """读取主文档并记录脱敏的具体诊断，仅成功结果携带允许输出的实际文本。"""
    if allowed_url is None:
        store.finish(run_id, "error", "示例站端口配置无效。请使用 1 至 65535 的整数端口后重试。")
        return
    try:
        observed = read_page(allowed_url if target is None else target, allowed_url)
    except PageFailure as exc:
        status = "error" if exc.code == "browser_error" else "blocked"
        store.finish(run_id, status, str(exc))
    except Exception as exc:
        # 避免把含请求地址、进程参数或页面内容的异常文本发给模型。
        store.finish(run_id, "error", f"读取器异常中断（{type(exc).__name__}）。请核对本机环境后重试。")
    else:
        detail = f"Chromium 读到标题「{observed['title']}」及可见标志「{observed['marker']}」。"
        store.finish(run_id, "read", detail, title=observed["title"], marker=observed["marker"])


@mcp.tool()
async def read_demo_page(target: str | None = None) -> dict:
    """用 Chromium 只读打开已配置的本地示例后台首页，返回运行 ID、实读结果及步骤。默认无需填写地址。"""
    try:
        # Playwright 同步驱动须在 DSH 所用的异步事件循环之外运行。
        return await asyncio.to_thread(run_read, target)
    except (sqlite3.Error, OSError):
        return {"status": "error", "reason": "无法读写运行库。请核对数据库结构、目录权限与剩余空间后重试。"}


@mcp.tool()
def get_run(run_id: str) -> dict:
    """按完整运行 ID 查询已保存的结果及步骤，可在新的对话中使用。"""
    try:
        key = str(UUID(run_id.strip()))
    except ValueError:
        return {"status": "invalid_id", "reason": "运行 ID 格式有误，请复制读取结果中的完整 ID。"}
    try:
        record = open_store().get(key)
    except (sqlite3.Error, OSError):
        return {"status": "error", "reason": "无法查询运行库。请核对数据库结构、目录权限与剩余空间后重试。"}
    if record is None:
        return {"status": "not_found", "reason": "未找到这次运行。请核对 ID 与项目数据目录。"}
    return record


if __name__ == "__main__":
    mcp.run(transport="stdio")
