"""通过 DSH 的 MCP 入口读取用户指定的网站、执行已配置的登录及查询运行。"""

import asyncio
import os
import re
import sqlite3
from pathlib import Path
from uuid import UUID

from mcp.server.fastmcp import FastMCP

from .browser import PageFailure, read_page
from .landing import canonical_entry, read_landing
from .login import start_login
from .store import SiteSelectionError, Store


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


def environment_label(value: str | None) -> str | None:
    """规范化已指明的环境名，保留缺省状态以便从已保存的站点查找候选。"""
    label = (value or "").strip()
    if not label:
        return None
    if not re.fullmatch(r"[\w\- ]{1,40}", label):
        raise ValueError("环境名称请使用简短的文字、数字、空格、下划线或连字符。")
    return label + "环境" if label in {"测试", "生产", "预发", "开发"} else label


def checked_site_name(value: str) -> str:
    """保留用户给的站点名称，拒绝空白名称和疑似网址或凭据的特殊分隔符。"""
    name = value.strip()
    if not re.fullmatch(r"[\w][\w .\-]{0,63}", name):
        raise ValueError("站点名请使用 1 到 64 个文字、数字、空格、点、下划线或连字符。")
    return name


def run_open_site(url: str | None, environment: str | None, site_name: str | None = None) -> dict:
    """绑定或解析当前部署后领取读取任务，按实际运行时的入口保存页面证据。"""
    try:
        label = environment_label(environment)
        entry = canonical_entry(url) if url is not None else None
        store = open_store()
        if site_name is not None:
            site_name = checked_site_name(site_name)
            run_id, execute, entry = store.prepare_named_read(site_name, label, entry)
        elif entry is not None:
            run_id, execute = store.begin_site_read(entry, label or "未指定")
        else:
            return {"status": "blocked", "reason": "请给出网址，或已记住的站点名。"}
    except SiteSelectionError as exc:
        return exc.result
    except (PageFailure, ValueError) as exc:
        return {"status": "blocked", "reason": str(exc)}
    if execute:
        try:
            page = read_landing(entry)
        except PageFailure as exc:
            store.finish(run_id, "error" if exc.code == "browser_error" else "blocked", str(exc))
        except Exception as exc:
            # 驱动或解析器的意外错误也要结束本次运行，防止下次重试复用卡住的记录或回显页面原文。
            store.finish(run_id, "error", f"页面读取意外中断（{type(exc).__name__}）。请重新发起读取。")
        else:
            detail = f"Chromium 已打开入口页，HTTP {page['http_status']}。"
            if page["title"]:
                detail += f" 页面标题「{page['title']}」。"
            if page["marker"]:
                detail += f" 可见标题「{page['marker']}」。"
            if page["password_field"]:
                detail += " 页面上有可见的密码输入框。"
            store.finish(run_id, "read", detail, title=page["title"], marker=page["marker"])
    result = store.get(run_id)
    if result is None:
        raise sqlite3.DatabaseError("无法查回刚登记的运行。")
    return result


@mcp.tool()
async def open_site(url: str | None = None, environment: str | None = None, site_name: str | None = None) -> dict:
    """按网址匿名打开首页。用户给站点名时一并记住；按已知名称打开时仅传 site_name，可附 environment。"""
    try:
        return await asyncio.to_thread(run_open_site, url, environment, site_name)
    except (sqlite3.Error, OSError):
        return {"status": "error", "reason": "站点记录读写失败。请核对本机运行库、目录权限及剩余空间。"}


@mcp.tool()
def update_site_address(site_name: str, url: str, environment: str | None = None) -> dict:
    """用户明确要求改地址时更新已记住的公开入口，返回原地址与新地址。未点明环境时省略 environment。"""
    try:
        name = checked_site_name(site_name)
        label = environment_label(environment)
        entry = canonical_entry(url)
        return open_store().update_site_address(name, label, entry)
    except SiteSelectionError as exc:
        return exc.result
    except (PageFailure, ValueError) as exc:
        return {"status": "blocked", "reason": str(exc)}
    except (sqlite3.Error, OSError):
        return {"status": "error", "reason": "站点地址更新失败。请核对本机运行库、目录权限及剩余空间。"}


@mcp.tool()
async def login_site() -> dict:
    """登录本机配置的测试站并验证账号。人工挑战在 Chromium 中完成，按返回的运行 ID 查询进度。"""
    try:
        store = open_store()
        return await asyncio.to_thread(start_login, store)
    except (sqlite3.Error, OSError, RuntimeError):
        return {"status": "error", "reason": "无法启动登录任务。请核对本机环境、运行库与目录权限后重试。"}


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
