"""持久化示例页读取的运行、步骤证据及中断结果。"""

import os
import sqlite3
import sys
import uuid
from collections.abc import Iterator
from contextlib import closing, contextmanager
from datetime import datetime, timezone
from pathlib import Path


def _utc_now() -> str:
    """为同一次运行和步骤生成可跨进程比较的 UTC 时间。"""
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def _windows_process_stamp(pid: int) -> str | None:
    """读取 Windows 活进程的创建时间，进程已结束时抛出 ProcessLookupError。"""
    import ctypes
    from ctypes import wintypes

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.WaitForSingleObject.argtypes = (wintypes.HANDLE, wintypes.DWORD)
    kernel.GetProcessTimes.argtypes = (wintypes.HANDLE,) + (ctypes.POINTER(wintypes.FILETIME),) * 4
    kernel.CloseHandle.argtypes = (wintypes.HANDLE,)
    # SYNCHRONIZE 检查退出状态，PROCESS_QUERY_LIMITED_INFORMATION 读取创建时间。
    handle = kernel.OpenProcess(0x00100000 | 0x1000, False, pid)
    if not handle:
        if ctypes.get_last_error() == 87:  # ERROR_INVALID_PARAMETER 表示此 PID 无对应进程。
            raise ProcessLookupError(pid)
        return None
    try:
        if kernel.WaitForSingleObject(handle, 0) == 0:  # 已退出的进程对象进入 signaled 状态。
            raise ProcessLookupError(pid)
        created, exited, system_time, user_time = (wintypes.FILETIME() for _ in range(4))
        if kernel.GetProcessTimes(handle, created, exited, system_time, user_time):
            return f"{created.dwHighDateTime:08x}{created.dwLowDateTime:08x}"
        return None
    finally:
        kernel.CloseHandle(handle)


def _process_stamp(pid: int) -> str | None:
    """返回可识别 PID 复用的标识，已退出时抛出异常，无法核实时返回空。"""
    if sys.platform == "win32":
        return _windows_process_stamp(pid)
    try:
        if sys.platform.startswith("linux"):
            # stat 的 comm 字段可含空格和右括号。尾括号之后是第 3 字段状态。
            stat = Path(f"/proc/{pid}/stat").read_bytes().rsplit(b") ", 1)[1].split()
            if stat[0] in (b"Z", b"X"):
                raise ProcessLookupError(pid)
            boot = Path("/proc/sys/kernel/random/boot_id").read_text().strip()
            return f"{boot}:{int(stat[19])}"
        # 在没有受支持的创建时间接口的平台，只以已不存在的 PID 判定中断。
        os.kill(pid, 0)
    except FileNotFoundError:
        # /proc 本身也可能不可用，二次确认 PID，避免把未知状态误报为中断。
        try:
            os.kill(pid, 0)
        except PermissionError:
            return None
    except PermissionError:
        return None
    return None


def _owner_exited(pid: int, original_stamp: str | None) -> bool:
    """仅在证实原进程消失或 PID 已复用时允许结束其运行。"""
    try:
        current_stamp = _process_stamp(pid)
    except ProcessLookupError:
        return True
    return bool(original_stamp and current_stamp and current_stamp != original_stamp)


class Store:
    """按运行 ID 保存结果，保证状态和步骤在同一事务中可见。"""

    def __init__(self, db_path: Path):
        """打开指定文件并创建初始表，重复打开会保留既有历史。"""
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._transaction() as db:
            db.execute("""
                CREATE TABLE IF NOT EXISTS runs (
                    run_id TEXT PRIMARY KEY NOT NULL,
                    status TEXT NOT NULL CHECK (status IN ('running','read','blocked','error')),
                    reason TEXT NOT NULL,
                    title TEXT,
                    marker TEXT,
                    started_at TEXT NOT NULL,
                    finished_at TEXT,
                    owner_pid INTEGER NOT NULL CHECK (owner_pid > 0),
                    owner_stamp TEXT,
                    target_key TEXT,
                    CHECK ((status = 'running') = (finished_at IS NULL)),
                    CHECK (status = 'read' OR (title IS NULL AND marker IS NULL))
                )
            """)
            db.execute("""
                CREATE UNIQUE INDEX IF NOT EXISTS runs_active_target ON runs (target_key)
                WHERE status = 'running' AND target_key IS NOT NULL
            """)
            db.execute("""
                CREATE TABLE IF NOT EXISTS steps (
                    run_id TEXT NOT NULL REFERENCES runs(run_id),
                    seq INTEGER NOT NULL CHECK (seq IN (1, 2)),
                    status TEXT NOT NULL CHECK (status IN ('running','read','blocked','error')),
                    detail TEXT NOT NULL,
                    recorded_at TEXT NOT NULL,
                    PRIMARY KEY (run_id, seq),
                    CHECK ((seq = 1) = (status = 'running'))
                )
            """)

    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Connection]:
        """串行提交短事务，使多个工具进程可可靠地写入或核对同一运行。"""
        with closing(sqlite3.connect(self.db_path, timeout=10)) as db:
            db.row_factory = sqlite3.Row
            db.execute("PRAGMA foreign_keys = ON")
            with db:
                db.execute("BEGIN IMMEDIATE")
                yield db

    def begin_read(self, target_key: str | None) -> tuple[str, bool]:
        """复用已配置首页的进行中读取，返回运行 ID 和本次是否应实际执行。"""
        # 调用方从本地配置生成目标键。用户提交的其他地址仅以 None 独立登记。
        with self._transaction() as db:
            if target_key is not None:
                owner = db.execute(
                    """SELECT run_id, owner_pid, owner_stamp FROM runs
                       WHERE target_key = ? AND status = 'running'""",
                    (target_key,),
                ).fetchone()
                if owner is not None and not self._recover_interruption(db, owner):
                    return owner["run_id"], False
            run_id = str(uuid.uuid4())
            pid = os.getpid()
            now = _utc_now()
            detail = "已收到示例页读取请求。"
            db.execute(
                """INSERT INTO runs
                       (run_id, status, reason, started_at, owner_pid, owner_stamp, target_key)
                   VALUES (?, 'running', ?, ?, ?, ?, ?)""",
                (run_id, detail, now, pid, _process_stamp(pid), target_key),
            )
            db.execute(
                """INSERT INTO steps (run_id, seq, status, detail, recorded_at)
                   VALUES (?, 1, 'running', ?, ?)""",
                (run_id, detail, now),
            )
        return run_id, True

    @staticmethod
    def _finish(
        db: sqlite3.Connection, run_id: str, status: str, reason: str,
        title: str | None, marker: str | None,
    ) -> None:
        """在同一事务中补齐首次终态和结束步骤，重复回调保留原结果。"""
        now = _utc_now()
        result = db.execute(
            """UPDATE runs SET status = ?, reason = ?, title = ?, marker = ?, finished_at = ?
               WHERE run_id = ? AND status = 'running'""",
            (status, reason, title, marker, now, run_id),
        )
        if result.rowcount:
            db.execute(
                """INSERT INTO steps (run_id, seq, status, detail, recorded_at)
                   VALUES (?, 2, ?, ?, ?)""",
                (run_id, status, reason, now),
            )
        elif db.execute("SELECT 1 FROM runs WHERE run_id = ?", (run_id,)).fetchone() is None:
            raise KeyError(run_id)

    def finish(
        self, run_id: str, status: str, reason: str,
        title: str | None = None, marker: str | None = None,
    ) -> None:
        """结束指定运行，只接收业务层给出的结论、脱敏原因与页面观察值。"""
        if status not in {"read", "blocked", "error"}:
            raise ValueError("无效的运行终态。")
        with self._transaction() as db:
            self._finish(db, run_id, status, reason, title, marker)

    def _recover_interruption(self, db: sqlite3.Connection, owner: sqlite3.Row) -> bool:
        """在当前事务中核实并结清已中断的运行，保留仍存活进程的记录。"""
        if not _owner_exited(owner["owner_pid"], owner["owner_stamp"]):
            return False
        self._finish(db, owner["run_id"], "error", "读取进程已中断。请重新发起读取。", None, None)
        return True

    def get(self, run_id: str) -> dict | None:
        """按 ID 读取结果和有序证据，对已证实的进程中断补记终态。"""
        with self._transaction() as db:
            owner = db.execute(
                "SELECT run_id, status, owner_pid, owner_stamp FROM runs WHERE run_id = ?", (run_id,),
            ).fetchone()
            if owner is None:
                return None
            if owner["status"] == "running":
                self._recover_interruption(db, owner)
            row = db.execute(
                """SELECT run_id, status, reason, title, marker, started_at, finished_at
                   FROM runs WHERE run_id = ?""",
                (run_id,),
            ).fetchone()
            steps = db.execute(
                "SELECT seq, status, detail, recorded_at FROM steps WHERE run_id = ? ORDER BY seq",
                (run_id,),
            ).fetchall()
        return {**row, "steps": [dict(step) for step in steps]}
