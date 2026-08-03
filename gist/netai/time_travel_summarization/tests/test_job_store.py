"""잡 스토어 테스트 — SQLite(어디서나) + Postgres(env 있을 때만).

SQLite 시나리오는 WSL stdlib에서 그대로 돈다(pytest 불필요 — `python3 -m unittest`도 OK,
pytest도 unittest.TestCase를 수집한다). Postgres는 JOB_STORE_TEST_DSN(또는 DATABASE_URL)이
설정됐을 때만 실행하고, 없으면 skip(minio 테스트와 동일 관례).
"""
import os
import tempfile
import threading
import unittest
from pathlib import Path

from gist.netai.time_travel_summarization.automation.remote_generation import JobSpec
from gist.netai.time_travel_summarization.VLM_server.l40.job_store import (
    JobExists, PostgresJobStore, SqliteJobStore, store_from_url,
)


class _JobStoreContract:
    """두 백엔드 공통 시나리오. make_store()를 서브클래스가 제공."""

    def make_store(self):  # pragma: no cover - overridden
        raise NotImplementedError

    def test_lifecycle_register_claim_mark(self):
        s = self.make_store()
        s.register(JobSpec(job_id="j-1", gpu=1, episodes=7))
        got = s.get("j-1")
        self.assertEqual(got["state"], "queued")
        self.assertEqual(got["gpu"], 1)
        self.assertNotIn("spec_json", got)  # 내부 필드 누출 금지
        spec = s.claim_next(1)
        self.assertIsNotNone(spec)
        self.assertEqual(spec.job_id, "j-1")
        self.assertEqual(spec.episodes, 7)  # JobSpec 라운드트립
        self.assertEqual(s.get("j-1")["state"], "running")
        s.mark("j-1", "done", note="ok")
        self.assertEqual(s.get("j-1")["state"], "done")
        self.assertEqual(s.get("j-1")["note"], "ok")

    def test_dedup_raises(self):
        s = self.make_store()
        s.register(JobSpec(job_id="dup", gpu=1))
        with self.assertRaises(JobExists):
            s.register(JobSpec(job_id="dup", gpu=1))

    def test_claim_empty_returns_none(self):
        s = self.make_store()
        self.assertIsNone(s.claim_next(1))
        s.register(JobSpec(job_id="g2", gpu=2))
        self.assertIsNone(s.claim_next(1))  # 다른 gpu 잡은 안 집힘
        self.assertEqual(s.claim_next(2).job_id, "g2")

    def test_requeue_stale_restores_running(self):
        s = self.make_store()
        s.register(JobSpec(job_id="r-1", gpu=1))
        s.register(JobSpec(job_id="r-2", gpu=1))
        self.assertEqual(s.claim_next(1).job_id, "r-1")
        s.mark("r-1", "done")               # 종료 잡은 복원 대상 아님
        self.assertEqual(s.claim_next(1).job_id, "r-2")  # running 잔류
        n = s.requeue_stale(0)
        self.assertEqual(n, 1)
        self.assertEqual(s.get("r-1")["state"], "done")
        self.assertEqual(s.get("r-2")["state"], "queued")

    def test_list_and_counts(self):
        s = self.make_store()
        s.register(JobSpec(job_id="a", gpu=1))
        s.register(JobSpec(job_id="b", gpu=1))
        s.register(JobSpec(job_id="c", gpu=2))
        self.assertEqual(s.counts_by_gpu(), {1: 2, 2: 1})
        s.claim_next(1)
        self.assertEqual(s.counts_by_gpu(), {1: 1, 2: 1})
        ids = sorted(j["job_id"] for j in s.list())
        self.assertEqual(ids, ["a", "b", "c"])
        queued = [j["job_id"] for j in s.list(states=["queued"])]
        self.assertNotIn("a", queued)  # a는 running으로 집힘(순서 무관 확인)
        self.assertEqual(len(queued), 2)

    def test_get_unknown_returns_none(self):
        s = self.make_store()
        self.assertIsNone(s.get("nope"))

    def test_concurrent_claim_never_double(self):
        """스레드 2개가 동시에 claim_next 해도 같은 잡을 두 번 집지 않음(원자성)."""
        s = self.make_store()
        n = 40
        for i in range(n):
            s.register(JobSpec(job_id=f"c-{i:03d}", gpu=1))
        claimed: list[str] = []
        lock = threading.Lock()

        def drain():
            while True:
                spec = s.claim_next(1)
                if spec is None:
                    return
                with lock:
                    claimed.append(spec.job_id)

        threads = [threading.Thread(target=drain) for _ in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(len(claimed), n)
        self.assertEqual(len(set(claimed)), n, "같은 잡이 두 번 집혔다")


class SqliteJobStoreTest(_JobStoreContract, unittest.TestCase):
    def setUp(self):
        self._dir = tempfile.mkdtemp(prefix="jobstore_test_")
        self._n = 0

    def make_store(self):
        self._n += 1
        return SqliteJobStore(str(Path(self._dir) / f"jobs{self._n}.db"))

    def test_store_from_url_selects_sqlite(self):
        url = f"sqlite:///{Path(self._dir) / 'via.db'}"
        s = store_from_url(url)
        self.assertIsInstance(s, SqliteJobStore)
        s.register(JobSpec(job_id="u-1", gpu=1))
        self.assertEqual(s.claim_next(1).job_id, "u-1")

    def test_store_from_url_bad_scheme(self):
        with self.assertRaises(ValueError):
            store_from_url("redis://x")


_PG_DSN = os.environ.get("JOB_STORE_TEST_DSN") or os.environ.get("DATABASE_URL")


@unittest.skipUnless(_PG_DSN, "Postgres DSN not set (JOB_STORE_TEST_DSN/DATABASE_URL)")
class PostgresJobStoreTest(_JobStoreContract, unittest.TestCase):
    def setUp(self):
        try:
            import psycopg  # noqa: F401
        except Exception:
            self.skipTest("psycopg not installed")
        # 매 테스트 격리: jobs 테이블 비운다(같은 DSN 공유 전제).
        self._store = PostgresJobStore(_PG_DSN)
        import psycopg
        with psycopg.connect(_PG_DSN) as con:
            con.execute("TRUNCATE jobs")

    def make_store(self):
        return self._store


if __name__ == "__main__":
    unittest.main()
