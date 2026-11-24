"""核对运行结果的原子写入、并发重试和跨进程中断恢复。"""

import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from pathlib import Path

from webtest_atlas.store import Store


class StoreTests(unittest.TestCase):
    """使用临时 SQLite 文件隔离每个运行记录场景。"""

    def setUp(self):
        """为本次用例创建独立的数据目录。"""
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.path = Path(temporary.name) / "records" / "atlas.sqlite3"
        self.store = Store(self.path)

    def test_history_survives_reopen_and_finish_retries(self):
        """不同实例查到相同结果，晚到的回调无法覆写已有证据。"""
        self.assertIsNone(self.store.get("unknown"))
        run_id, _ = self.store.begin_read(None)
        self.assertEqual(self.store.get(run_id)["status"], "running")
        self.store.finish(run_id, "read", "已读取。", "示例页", "fixture")
        reopened = Store(self.path)
        first = reopened.get(run_id)
        reopened.finish(run_id, "error", "晚到的异常。")
        self.assertEqual(first, reopened.get(run_id))
        self.assertEqual((first["title"], first["marker"]), ("示例页", "fixture"))
        self.assertEqual([step["status"] for step in first["steps"]], ["running", "read"])
        self.assertEqual(first["steps"][1]["recorded_at"], first["finished_at"])
        self.assertNotIn("owner_pid", first)

    def test_failed_step_rolls_back_terminal_result(self):
        """写入结束证据出错时终态一并回滚，恢复写入后可重试。"""
        run_id, _ = self.store.begin_read(None)
        with closing(sqlite3.connect(self.path)) as db:
            db.execute("""
                CREATE TRIGGER reject_finish BEFORE INSERT ON steps WHEN NEW.seq = 2
                BEGIN SELECT RAISE(ABORT, 'fixture'); END
            """)
        with self.assertRaises(sqlite3.IntegrityError):
            self.store.finish(run_id, "blocked", "示例站不可达。")
        self.assertEqual(self.store.get(run_id)["status"], "running")
        with closing(sqlite3.connect(self.path)) as db:
            db.execute("DROP TRIGGER reject_finish")
        self.store.finish(run_id, "blocked", "示例站不可达。")
        self.assertEqual(len(self.store.get(run_id)["steps"]), 2)

    def test_concurrent_writers_preserve_first_terminal_evidence(self):
        """同时结束同一运行只写一条结束步骤，其他运行可并发创建。"""
        with ThreadPoolExecutor(max_workers=4) as pool:
            ids = [run_id for run_id, _ in pool.map(self.store.begin_read, [None] * 6)]
            futures = [
                pool.submit(self.store.finish, ids[0], "error", f"读取失败 {i}")
                for i in range(6)
            ]
            for future in futures:
                future.result()
        self.assertEqual(len(set(ids)), 6)
        result = self.store.get(ids[0])
        self.assertEqual(len(result["steps"]), 2)
        self.assertEqual(result["reason"], result["steps"][1]["detail"])
        self.assertEqual(self.store.get(ids[-1])["status"], "running")

    def test_simultaneous_reads_reuse_only_the_configured_active_target(self):
        """同一首页并发只分配一次实际执行，已结束、异端口及未知目标单独登记。"""
        target = "http://127.0.0.1:18765/"
        with ThreadPoolExecutor(max_workers=4) as pool:
            starts = list(pool.map(self.store.begin_read, [target] * 8))
        run_id = starts[0][0]
        self.assertEqual({key for key, _ in starts}, {run_id})
        self.assertEqual(sum(should_run for _, should_run in starts), 1)
        others = [self.store.begin_read(key) for key in (None, None, "http://127.0.0.1:18766/")]
        self.assertTrue(all(should_run for _, should_run in others))
        self.assertEqual(len({run_id, *(key for key, _ in others)}), 4)
        self.store.finish(run_id, "read", "已读取。")
        next_run, should_run = self.store.begin_read(target)
        self.assertTrue(should_run)
        self.assertNotEqual(next_run, run_id)

    def test_open_requires_current_schema(self):
        """缺少目标键时打开失败，验证初始化不会改写已有表结构。"""
        with closing(sqlite3.connect(self.path)) as db:
            db.execute("DROP INDEX runs_active_target")
            db.execute("ALTER TABLE runs DROP COLUMN target_key")
        with self.assertRaisesRegex(sqlite3.OperationalError, "target_key"):
            Store(self.path)
        with closing(sqlite3.connect(self.path)) as db:
            columns = [row[1] for row in db.execute("PRAGMA table_info(runs)")]
            self.assertNotIn("target_key", columns)

    def test_only_recovers_after_owner_process_has_exited(self):
        """历史查询和同目标重试都保留活进程，进程退出后各自补记中断。"""
        target = "http://127.0.0.1:18765/"
        child_code = (
            "import sys\n"
            "from pathlib import Path\n"
            "from webtest_atlas.store import Store\n"
            "store = Store(Path(sys.argv[1]))\n"
            "print(store.begin_read(None)[0], flush=True)\n"
            "print(store.begin_read(sys.argv[2])[0], flush=True)\n"
            "sys.stdin.read(1)\n"
        )
        env = os.environ.copy()
        env["PYTHONPATH"] = str(Path(__file__).resolve().parents[1] / "src")
        with subprocess.Popen(
            [sys.executable, "-c", child_code, str(self.path), target], text=True,
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env,
        ) as child:
            try:
                run_id = child.stdout.readline().strip()
                active_target = child.stdout.readline().strip()
                self.assertTrue(run_id, "子进程未登记运行。")
                self.assertEqual(self.store.get(run_id)["status"], "running")
                self.assertEqual(self.store.begin_read(target), (active_target, False))
            finally:
                _, error = child.communicate(input="x", timeout=10)
        self.assertEqual(child.returncode, 0, error)
        result = self.store.get(run_id)
        self.assertEqual(result["status"], "error")
        self.assertIn("中断", result["reason"])
        self.assertEqual(Store(self.path).get(run_id), result)
        retry_id, should_run = self.store.begin_read(target)
        self.assertTrue(should_run)
        self.assertNotEqual(retry_id, active_target)
        self.assertEqual(self.store.get(active_target)["status"], "error")
