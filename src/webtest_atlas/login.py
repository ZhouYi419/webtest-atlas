"""在独立进程执行站点登录，DSH 可在人工验证期间返回并查询同一运行。"""

import os
import sqlite3
import subprocess
import sys
from pathlib import Path
from time import monotonic

from .browser import PageFailure
from .login_browser import execute_login
from .site_config import SiteConfigError, load_profile
from .store import Store


def _execute(store: Store, run_id: str) -> None:
    """执行已登记的登录，等待状态和最终结论只写入原运行。"""
    try:
        detail = execute_login(load_profile(), lambda: store.wait_for_user(run_id))
    except SiteConfigError as exc:
        store.finish(run_id, "blocked", str(exc))
    except PageFailure as exc:
        status = "error" if exc.code == "browser_error" else "blocked"
        store.finish(run_id, status, str(exc))
    except Exception:
        # 第三方异常可能含地址、表单值或响应头，仅归档固定诊断。
        store.finish(run_id, "error", "登录执行异常中断。请核对本机浏览器和站点配置后重试。")
    else:
        store.finish(run_id, "authenticated", detail)


def start_login(store: Store) -> dict:
    """为独立执行者登记运行，只向成功领取本次任务的子进程发送 ID。"""
    # 子进程有专用输入，浏览器驱动和输出均与 MCP 的 stdio 协议完全隔离。
    worker = subprocess.Popen(
        [sys.executable, "-m", "webtest_atlas.login"], stdin=subprocess.PIPE, bufsize=0,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        env={**os.environ, "WEBTEST_ATLAS_DB": str(store.db_path.resolve())},
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    with worker.stdin as task_input:
        run_id, created = store.begin_login(worker.pid)
        if created:
            try:
                task_input.write((run_id + "\n").encode("ascii"))
            except OSError:
                store.finish(run_id, "error", "登录进程启动中断。请检查本机环境后重试。")
    # 等待短任务完成；人工接管或慢站点应及时获得可回查的 ID。
    deadline = monotonic() + 15
    while True:
        try:
            record = store.get(run_id)
        except (sqlite3.Error, OSError):
            record = None
        if record is None:
            return {"run_id": run_id, "status": "error", "reason": "无法读取登录记录。请检查运行库后按此 ID 查询。"}
        if not created or record["status"] != "running" or monotonic() >= deadline:
            return record
        try:
            worker.wait(timeout=0.25)
        except subprocess.TimeoutExpired:
            continue


if __name__ == "__main__":
    # 发起端完成登记才发送 ID；并发复用或发起端提前关闭时，不执行站点操作。
    task_id = sys.stdin.buffer.readline(64).decode("ascii").strip()
    if task_id:
        _execute(Store(Path(os.environ["WEBTEST_ATLAS_DB"])), task_id)
