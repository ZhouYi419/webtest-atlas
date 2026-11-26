"""持久化读取和登录的运行、步骤证据及中断结果。"""

import os
import sqlite3
import sys
import uuid
from collections.abc import Iterator
from contextlib import closing, contextmanager
from datetime import datetime, timezone
from pathlib import Path


class SiteSelectionError(Exception):
    """保存可返回对话的站点定位结果，让用户补齐环境或明确改址。"""

    def __init__(self, status: str, site_name: str, reason: str, environments: list[str] | None = None):
        """只返回名称、原因和必要的环境选项，供对话选择下一步。"""
        super().__init__(reason)
        self.result: dict[str, str | list[str]] = {"status": status, "site_name": site_name, "reason": reason}
        if environments is not None:
            self.result["environments"] = environments


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
        """创建当前配置表和运行表，在同一事务内保留已有部署、运行和步骤。"""
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._transaction() as db:
            version = db.execute("PRAGMA user_version").fetchone()[0]
            if version not in (0, 1):
                raise sqlite3.DatabaseError("无法识别运行库版本。")
            old = version == 0 and db.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'runs'"
            ).fetchone() is not None
            if old:
                db.execute("ALTER TABLE steps RENAME TO steps_t001")
                db.execute("ALTER TABLE runs RENAME TO runs_t001")
            db.execute("""
                CREATE TABLE IF NOT EXISTS deployments (
                    deployment_id TEXT NOT NULL PRIMARY KEY,
                    entry_url TEXT NOT NULL,
                    environment TEXT NOT NULL,
                    UNIQUE (entry_url, environment)
                )
            """)
            db.execute("""
                CREATE TABLE IF NOT EXISTS site_entries (
                    site_name TEXT NOT NULL COLLATE NOCASE,
                    environment TEXT NOT NULL,
                    deployment_id TEXT NOT NULL UNIQUE REFERENCES deployments(deployment_id),
                    PRIMARY KEY (site_name, environment)
                )
            """)
            db.execute("""
                CREATE TABLE IF NOT EXISTS runs (
                    run_id TEXT PRIMARY KEY NOT NULL,
                    kind TEXT NOT NULL DEFAULT 'read' CHECK (kind IN ('read','login')),
                    status TEXT NOT NULL CHECK (status IN
                        ('running','waiting','read','authenticated','blocked','error')),
                    reason TEXT NOT NULL,
                    title TEXT,
                    marker TEXT,
                    started_at TEXT NOT NULL,
                    finished_at TEXT,
                    owner_pid INTEGER NOT NULL CHECK (owner_pid > 0),
                    owner_stamp TEXT,
                    target_key TEXT,
                    CHECK ((status IN ('running','waiting')) = (finished_at IS NULL)),
                    CHECK (kind = 'login' OR status NOT IN ('waiting','authenticated')),
                    CHECK (kind = 'read' OR status != 'read'),
                    CHECK (status = 'read' OR (title IS NULL AND marker IS NULL))
                )
            """)
            db.execute("""
                CREATE TABLE IF NOT EXISTS steps (
                    run_id TEXT NOT NULL REFERENCES runs(run_id),
                    seq INTEGER NOT NULL CHECK (seq >= 1),
                    status TEXT NOT NULL CHECK (status IN
                        ('running','waiting','read','authenticated','blocked','error')),
                    detail TEXT NOT NULL,
                    recorded_at TEXT NOT NULL,
                    PRIMARY KEY (run_id, seq),
                    CHECK ((seq = 1) = (status = 'running'))
                )
            """)
            if old:
                db.execute("""
                    INSERT INTO runs (run_id, status, reason, title, marker, started_at,
                                      finished_at, owner_pid, owner_stamp, target_key)
                    SELECT run_id, status, reason, title, marker, started_at,
                           finished_at, owner_pid, owner_stamp, target_key FROM runs_t001
                """)
                db.execute("""
                    INSERT INTO steps (run_id, seq, status, detail, recorded_at)
                    SELECT run_id, seq, status, detail, recorded_at FROM steps_t001
                """)
                db.execute("DROP TABLE steps_t001")
                db.execute("DROP TABLE runs_t001")
            db.execute("""
                CREATE UNIQUE INDEX IF NOT EXISTS runs_active_target ON runs (target_key)
                WHERE status IN ('running','waiting') AND target_key IS NOT NULL
            """)
            if version == 0:
                db.execute("PRAGMA user_version = 1")

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
        with self._transaction() as db:
            return self._begin(db, "read", target_key, "已收到示例页读取请求。")

    def begin_login(self, owner_pid: int | None = None) -> tuple[str, bool]:
        """单环境内复用正在执行或等待人工的登录，避免重复提交密码。"""
        with self._transaction() as db:
            return self._begin(db, "login", "login_site", "已收到登录请求，正在核对配置与受保护页面。", owner_pid)

    def begin_site_read(self, entry_url: str, environment: str) -> tuple[str, bool]:
        """在登记运行的同一事务中识别部署，同一地址的不同环境分别保留历史。"""
        with self._transaction() as db:
            return self._begin_site(db, self._deployment_id(db, entry_url, environment))

    def prepare_named_read(
        self, site_name: str, environment: str | None = None, entry_url: str | None = None,
    ) -> tuple[str, bool, str]:
        """定位或首次登记名称和环境，在同一事务中领取实际地址的读取运行。"""
        with self._transaction() as db:
            current = self._find_site(db, site_name, environment, entry_url)
            if current is None:
                if entry_url is None:
                    raise SiteSelectionError("not_found", site_name, "没有找到这个站点配置，请提供站点地址和环境。")
                deployment_id = self._bind_site(db, site_name, environment or "未指定", entry_url)
            else:
                if entry_url is not None and entry_url != current["entry_url"]:
                    raise SiteSelectionError("conflict", site_name, "此站点环境已配置其他地址。如需改址，请明确要求更新。")
                entry_url = current["entry_url"]
                deployment_id = current["deployment_id"]
            run_id, execute = self._begin_site(db, deployment_id)
            return run_id, execute, entry_url

    def update_site_address(self, site_name: str, environment: str | None, entry_url: str) -> dict:
        """只修改选定站点的当前部署引用，旧运行和执行中的浏览器继续使用原地址。"""
        with self._transaction() as db:
            current = self._find_site(db, site_name, environment)
            if current is None:
                raise SiteSelectionError("not_found", site_name, "没有找到可更新的站点配置，请核对名称和环境。")
            if current["entry_url"] != entry_url:
                self._bind_site(db, current["site_name"], current["environment"], entry_url)
            return {"status": "updated", "site_name": current["site_name"], "environment": current["environment"],
                    "previous_url": current["entry_url"], "entry_url": entry_url}

    @staticmethod
    def _find_site(
        db: sqlite3.Connection, site_name: str, environment: str | None, known_url: str | None = None,
    ) -> sqlite3.Row | None:
        """按名称和可选环境查配置；省略环境时允许用已给出的读取地址匹配唯一候选。"""
        candidates = db.execute("""
            SELECT s.site_name, s.environment, s.deployment_id, d.entry_url
            FROM site_entries s JOIN deployments d ON d.deployment_id = s.deployment_id
            WHERE s.site_name = ? AND (? IS NULL OR s.environment = ?) ORDER BY s.environment
        """, (site_name, environment, environment)).fetchall()
        if len(candidates) <= 1:
            return candidates[0] if candidates else None
        matches = [row for row in candidates if row["entry_url"] == known_url]
        if len(matches) == 1:
            return matches[0]
        raise SiteSelectionError("ambiguous", site_name, "这个站点有多个环境，请选择本次使用的环境。",
                                 [row["environment"] for row in candidates])

    @staticmethod
    def _deployment_id(db: sqlite3.Connection, entry_url: str, environment: str) -> str:
        """复用地址和环境对应的部署编号；已存在的部署永远保留原地址。"""
        db.execute("""
            INSERT INTO deployments (deployment_id, entry_url, environment) VALUES (?, ?, ?)
            ON CONFLICT (entry_url, environment) DO NOTHING
        """, (str(uuid.uuid4()), entry_url, environment))
        return db.execute("""
            SELECT deployment_id FROM deployments WHERE entry_url = ? AND environment = ?
        """, (entry_url, environment)).fetchone()["deployment_id"]

    def _bind_site(self, db: sqlite3.Connection, site_name: str, environment: str, entry_url: str) -> str:
        """绑定当前配置，若目标部署已分配给另一名称则整笔写入回滚。"""
        deployment_id = self._deployment_id(db, entry_url, environment)
        occupied = db.execute("""
            SELECT 1 FROM site_entries WHERE deployment_id = ? AND site_name != ?
        """, (deployment_id, site_name)).fetchone()
        if occupied is not None:
            raise SiteSelectionError("conflict", site_name, "此地址和环境已绑定到另一个站点名称，请核对地址。")
        db.execute("""
            INSERT INTO site_entries (site_name, environment, deployment_id) VALUES (?, ?, ?)
            ON CONFLICT (site_name, environment) DO UPDATE SET deployment_id = excluded.deployment_id
        """, (site_name, environment, deployment_id))
        return deployment_id

    def _begin_site(self, db: sqlite3.Connection, deployment_id: str) -> tuple[str, bool]:
        """按实际部署领取站点读取，使名称调用和直接给地址的调用共享同一活跃运行。"""
        return self._begin(db, "read", deployment_id, "已收到站点入口，正在用 Chromium 打开页面。")

    def _begin(
        self, db: sqlite3.Connection, kind: str, target_key: str | None, detail: str, owner_pid: int | None = None,
    ) -> tuple[str, bool]:
        """在调用方事务中检查已有执行者，并一并写入运行和开始步骤。"""
        if target_key is not None:
            owner = db.execute(
                """SELECT run_id, kind, owner_pid, owner_stamp FROM runs
                   WHERE target_key = ? AND status IN ('running','waiting')""",
                (target_key,),
            ).fetchone()
            if owner is not None and not self._recover_interruption(db, owner):
                return owner["run_id"], False
        run_id = str(uuid.uuid4())
        pid = owner_pid or os.getpid()
        now = _utc_now()
        db.execute(
            """INSERT INTO runs
                   (run_id, kind, status, reason, started_at, owner_pid, owner_stamp, target_key)
               VALUES (?, ?, 'running', ?, ?, ?, ?, ?)""",
            (run_id, kind, detail, now, pid, _process_stamp(pid), target_key),
        )
        db.execute(
            """INSERT INTO steps (run_id, seq, status, detail, recorded_at)
               VALUES (?, 1, 'running', ?, ?)""",
            (run_id, detail, now),
        )
        return run_id, True

    def wait_for_user(self, run_id: str) -> None:
        """将本次登录标为等待人工，同步写入用户在 Chromium 中继续的提示。"""
        detail = "请在已打开的 Chromium 窗口完成站点验证，完成后会自动继续。可用此运行 ID 查询结果。"
        now = _utc_now()
        with self._transaction() as db:
            result = db.execute(
                """UPDATE runs SET status = 'waiting', reason = ?
                   WHERE run_id = ? AND kind = 'login' AND status = 'running'""", (detail, run_id),
            )
            if result.rowcount:
                self._step(db, run_id, "waiting", detail, now)

    @staticmethod
    def _step(db: sqlite3.Connection, run_id: str, status: str, detail: str, now: str) -> None:
        """在持有写事务时按运行分配连续序号，让进度与可见证据同步。"""
        db.execute("""
            INSERT INTO steps (run_id, seq, status, detail, recorded_at)
            SELECT ?, COALESCE(MAX(seq), 0) + 1, ?, ?, ? FROM steps WHERE run_id = ?
        """, (run_id, status, detail, now, run_id))

    @staticmethod
    def _finish(
        db: sqlite3.Connection, run_id: str, status: str, reason: str,
        title: str | None, marker: str | None,
    ) -> None:
        """在同一事务中补齐首次终态和结束步骤，重复回调保留原结果。"""
        now = _utc_now()
        result = db.execute(
            """UPDATE runs SET status = ?, reason = ?, title = ?, marker = ?, finished_at = ?
               WHERE run_id = ? AND status IN ('running','waiting')""",
            (status, reason, title, marker, now, run_id),
        )
        if result.rowcount:
            Store._step(db, run_id, status, reason, now)
        elif db.execute("SELECT 1 FROM runs WHERE run_id = ?", (run_id,)).fetchone() is None:
            raise KeyError(run_id)

    def finish(
        self, run_id: str, status: str, reason: str,
        title: str | None = None, marker: str | None = None,
    ) -> None:
        """结束指定运行，只接收业务层给出的结论、脱敏原因与页面观察值。"""
        if status not in {"read", "authenticated", "blocked", "error"}:
            raise ValueError("无效的运行终态。")
        with self._transaction() as db:
            self._finish(db, run_id, status, reason, title, marker)

    def _recover_interruption(self, db: sqlite3.Connection, owner: sqlite3.Row) -> bool:
        """在当前事务中核实并结清已中断的运行，保留仍存活进程的记录。"""
        if not _owner_exited(owner["owner_pid"], owner["owner_stamp"]):
            return False
        action = "登录" if owner["kind"] == "login" else "读取"
        self._finish(db, owner["run_id"], "error", f"{action}进程已中断。请重新发起{action}。", None, None)
        return True

    def get(self, run_id: str) -> dict | None:
        """按 ID 读取结果和有序证据，对已证实的进程中断补记终态。"""
        with self._transaction() as db:
            owner = db.execute(
                "SELECT run_id, kind, status, owner_pid, owner_stamp FROM runs WHERE run_id = ?", (run_id,),
            ).fetchone()
            if owner is None:
                return None
            if owner["status"] in {"running", "waiting"}:
                self._recover_interruption(db, owner)
            row = db.execute(
                """SELECT r.run_id, r.kind, r.status, r.reason, r.title, r.marker, r.started_at, r.finished_at,
                          d.deployment_id, d.entry_url, d.environment
                   FROM runs r LEFT JOIN deployments d ON d.deployment_id = r.target_key
                   WHERE r.run_id = ?""",
                (run_id,),
            ).fetchone()
            steps = db.execute(
                "SELECT seq, status, detail, recorded_at FROM steps WHERE run_id = ? ORDER BY seq",
                (run_id,),
            ).fetchall()
        record = dict(row)
        deployment = {key: record.pop(key) for key in ("deployment_id", "entry_url", "environment")}
        if deployment["deployment_id"] is not None:
            record["deployment"] = deployment
        return {**record, "steps": [dict(step) for step in steps]}
