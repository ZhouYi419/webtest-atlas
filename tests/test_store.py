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

from webtest_atlas.store import SiteSelectionError, Store


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

    def test_site_reads_keep_their_deployment_and_evidence(self):
        """同名环境、同址不同环境和同一部署的历史证据在重开数据库后均能区分。"""
        targets = [("https://a.example/", "测试环境"), ("https://b.example/", "测试环境"),
                   ("https://a.example/", "生产环境")]
        run_ids = []
        for target in targets:
            run_id, owner = self.store.begin_site_read(*target)
            self.assertTrue(owner)
            self.assertEqual(self.store.begin_site_read(*target), (run_id, False))
            self.store.finish(run_id, "read", "已打开页面。", "第一版", "申请")
            run_ids.append(run_id)
        reopened = Store(self.path)
        deployments = [reopened.get(run_id)["deployment"] for run_id in run_ids]
        self.assertEqual(len({d["deployment_id"] for d in deployments}), 3)
        self.assertEqual([(d["entry_url"], d["environment"]) for d in deployments], targets)
        latest_id, owner = reopened.begin_site_read(*targets[0])
        self.assertTrue(owner)
        reopened.finish(latest_id, "read", "已打开新版。", "第二版", "公告")
        self.assertEqual(reopened.get(latest_id)["deployment"], deployments[0])
        self.assertEqual(reopened.get(run_ids[0])["title"], "第一版")

    def test_name_reuses_old_deployment_and_changes_only_future_reads(self):
        """给此前的匿名部署命名，换址后不同进程仍可复查原运行并按名访问新地址。"""
        old_url, new_url = "https://first.example/", "https://second.example/portal"
        env = "测试环境"
        first, _ = self.store.begin_site_read(old_url, env)
        self.store.finish(first, "read", "首页已读取。", "原首页", "新闻")
        history = self.store.get(first)
        # 模拟此前版本生成的数据库，其中有部署和历史，但还没有命名配置表。
        with closing(sqlite3.connect(self.path)) as db:
            db.execute("DROP TABLE site_entries")
        session = Store(self.path)
        anonymous, _ = session.begin_site_read(old_url, env)
        self.assertEqual(session.prepare_named_read("Aweb", env, old_url), (anonymous, False, old_url))
        self.assertEqual(session.prepare_named_read("aWEB"), (anonymous, False, old_url))
        updated = Store(self.path).update_site_address("aWEB", None, new_url)
        self.assertEqual(updated, {"status": "updated", "site_name": "Aweb", "environment": env,
                                   "previous_url": old_url, "entry_url": new_url})
        self.assertEqual(session.update_site_address("Aweb", env, new_url), {**updated, "previous_url": new_url})
        later, execute, address = session.prepare_named_read("Aweb")
        self.assertEqual((execute, address), (True, new_url))
        self.assertEqual(session.begin_site_read(new_url, env), (later, False))
        self.assertEqual(session.get(later)["deployment"]["entry_url"], new_url)
        session.finish(anonymous, "read", "原地址的运行已完成。")
        self.assertEqual(session.get(anonymous)["deployment"], history["deployment"])
        self.assertEqual(Store(self.path).get(first), history)
        session.update_site_address("Aweb", env, old_url)
        back, _, _ = session.prepare_named_read("Aweb")
        self.assertEqual(session.get(back)["deployment"], history["deployment"])

    def test_named_sites_isolate_environments_and_reject_conflicts(self):
        """同环境的站点互相隔离；多环境同名请求只在能唯一定位时生效。"""
        test, prod = "测试环境", "生产环境"
        a_test, a_prod, b_test = "https://test.a.example/", "https://a.example/", "https://test.b.example/"
        test_run = self.store.prepare_named_read("Aweb", test, a_test)[0]
        prod_run = self.store.prepare_named_read("Aweb", prod, a_prod)[0]
        other_run = self.store.prepare_named_read("Bweb", test, b_test)[0]
        self.assertEqual(self.store.prepare_named_read("aweb", None, a_test), (test_run, False, a_test))
        self.assertEqual(self.store.prepare_named_read("BWEB"), (other_run, False, b_test))
        with self.assertRaises(SiteSelectionError) as ambiguous:
            self.store.prepare_named_read("Aweb")
        self.assertEqual(ambiguous.exception.result["status"], "ambiguous")
        self.assertCountEqual(ambiguous.exception.result["environments"], [test, prod])
        with self.assertRaises(SiteSelectionError) as ambiguous_update:
            self.store.update_site_address("Aweb", None, a_prod)
        self.assertEqual(ambiguous_update.exception.result, ambiguous.exception.result)
        for operation, args, status in (
            (self.store.prepare_named_read, ("Aweb", test, "https://new.example/"), "conflict"),
            (self.store.prepare_named_read, ("Other", test, a_test), "conflict"),
            (self.store.update_site_address, ("Aweb", test, b_test), "conflict"),
            (self.store.prepare_named_read, ("Aweb", "预发布"), "not_found"),
            (self.store.update_site_address, ("Aweb", "预发布", a_test), "not_found"),
            (self.store.update_site_address, ("Other", None, a_test), "not_found"),
        ):
            with self.subTest(operation=operation.__name__, args=args), self.assertRaises(SiteSelectionError) as error:
                operation(*args)
            self.assertEqual(error.exception.result["status"], status)
            self.assertEqual(error.exception.result["site_name"], args[0])
        self.assertEqual(self.store.prepare_named_read("Aweb", test), (test_run, False, a_test))
        self.assertEqual(self.store.prepare_named_read("Aweb", prod), (prod_run, False, a_prod))
        self.assertEqual(self.store.prepare_named_read("Bweb", test), (other_run, False, b_test))

    def test_concurrent_site_binding_and_run_claim_are_atomic(self):
        """首次并发登记仅领取一次读取，多个名称争用同一部署也只会绑定一次。"""
        first, second = "https://one.example/", "https://two.example/"
        with ThreadPoolExecutor(max_workers=4) as pool:
            claims = [pool.submit(self.store.prepare_named_read, "A", None, first) for _ in range(6)]
            runs = [claim.result() for claim in claims]
            rivals = [pool.submit(self.store.prepare_named_read, name, None, second) for name in ("B", "C")]
            results = [rival.exception() for rival in rivals]
        self.assertEqual(sum(execute for _, execute, _ in runs), 1)
        self.assertEqual(len({run_id for run_id, _, _ in runs}), 1)
        self.assertEqual(self.store.get(runs[0][0])["deployment"]["environment"], "未指定")
        errors = [result for result in results if result is not None]
        self.assertEqual(len(errors), 1)
        self.assertIsInstance(errors[0], SiteSelectionError)
        self.assertEqual(errors[0].result["status"], "conflict")
        with closing(sqlite3.connect(self.path)) as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM site_entries").fetchone()[0], 2)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM runs").fetchone()[0], 2)

    def test_failed_initial_receipt_rolls_back_new_site_binding(self):
        """运行开始步骤不能写入时，配置和部署一同回滚，仍可用原始请求重试。"""
        url = "https://retry.example/"
        with closing(sqlite3.connect(self.path)) as db:
            db.execute("""CREATE TRIGGER reject_start BEFORE INSERT ON steps
                          BEGIN SELECT RAISE(ABORT, 'fixture'); END""")
        with self.assertRaises(sqlite3.IntegrityError):
            self.store.prepare_named_read("A", None, url)
        with self.assertRaises(SiteSelectionError) as missing:
            self.store.prepare_named_read("A")
        self.assertEqual(missing.exception.result["status"], "not_found")
        with closing(sqlite3.connect(self.path)) as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM deployments").fetchone()[0], 0)
            db.execute("DROP TRIGGER reject_start")
        self.assertTrue(self.store.prepare_named_read("A", None, url)[1])

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

    def test_waiting_login_deduplicates_and_finishes_in_same_run(self):
        """多次发起或查询等待人工的任务只产生一次登录，并保留所有阶段。"""
        run_id, created = self.store.begin_login()
        self.assertTrue(created)
        self.store.wait_for_user(run_id)
        self.store.wait_for_user(run_id)
        with ThreadPoolExecutor(max_workers=4) as pool:
            pending = list(pool.map(lambda _: self.store.begin_login(), range(4)))
        self.assertEqual(pending, [(run_id, False)] * 4)
        self.assertIsNone(self.store.get(run_id)["finished_at"])
        self.store.finish(run_id, "authenticated", "受保护页面和账号已核对。")
        record = Store(self.path).get(run_id)
        self.assertEqual([step["status"] for step in record["steps"]], ["running", "waiting", "authenticated"])
        self.assertTrue(self.store.begin_login()[1])

    def test_upgrade_preserves_read_history_and_step_sequence(self):
        """打开此前交付的两步数据结构，原编号和证据保留，升级后可写登录。"""
        path = self.path.parent / "t001.sqlite3"
        with closing(sqlite3.connect(path)) as db:
            db.executescript("""
                CREATE TABLE runs (run_id TEXT PRIMARY KEY, status TEXT, reason TEXT, title TEXT,
                    marker TEXT, started_at TEXT, finished_at TEXT, owner_pid INTEGER,
                    owner_stamp TEXT, target_key TEXT);
                CREATE UNIQUE INDEX runs_active_target ON runs (target_key) WHERE status = 'running';
                CREATE TABLE steps (run_id TEXT REFERENCES runs(run_id), seq INTEGER CHECK (seq IN (1,2)),
                    status TEXT, detail TEXT, recorded_at TEXT, PRIMARY KEY(run_id,seq));
                INSERT INTO runs VALUES ('fixture','read','已读取','标题','标志','t1','t2',1,NULL,NULL);
                INSERT INTO steps VALUES ('fixture',1,'running','原开始证据','t1'),('fixture',2,'read','原结果','t2');
            """)
        upgraded = Store(path)
        original = upgraded.get("fixture")
        self.assertEqual((original["marker"], original["finished_at"]), ("标志", "t2"))
        self.assertEqual([s["detail"] for s in original["steps"]], ["原开始证据", "原结果"])
        run_id, _ = upgraded.begin_login()
        upgraded.wait_for_user(run_id)
        upgraded.finish(run_id, "authenticated", "已核对。")
        self.assertEqual(Store(path).get("fixture"), original)
