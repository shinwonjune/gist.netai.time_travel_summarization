"""잡 스토어 — SQLite/Postgres 이중 백엔드 (제어면 잡 상태·큐의 영속 저장소).

설계: docs/잡스토어_설계.md. job_api는 이 스토어를 **이미 채택**했다 — 잡 상태와
대기 큐의 원본은 여기(기본 SQLite `artifacts/jobs/jobs.db`)이고, 데몬이 재시작해도
잡이 보존된다. 단일 노드(SQLite)와 다중 노드(Postgres)를 같은 인터페이스로 지원한다.

운영 주의: job_api가 여전히 쓰는 잡 디렉토리의 `status` 파일은 **사람이 읽는 사본**일
뿐 원본이 아니다. 잡을 취소하려면 이 스토어의 `jobs` 행을 고쳐야 하고, status 파일만
고치면 데몬 재시작 때 스토어 기준으로 다시 큐잉된다(실측 사고 — minIO일지 §39 (e-5)).

핵심 계약:
  - claim_next(gpu)의 원자성 — 두 워커/노드가 같은 잡을 집지 않음(설계의 심장).
    SQLite는 BEGIN IMMEDIATE(단일 쓰기 잠금=상호배제), Postgres는
    SELECT ... FOR UPDATE SKIP LOCKED(잠긴 행 건너뛰기)로 각각 보장한다.
  - JobSpec은 remote_generation의 것을 그대로 쓴다(직렬화는 spec_json).
  - Postgres 의존성(psycopg)은 선택적 — import 실패가 데몬을 죽이지 않고, 사용
    시점(생성자)에만 RuntimeError로 안내(storage/minio_adapter와 동일 패턴).

kit 비의존 순수 파이썬 — WSL stdlib에서 임포트·self-test 가능(이 파일 직접 실행).
"""
from __future__ import annotations

import json
import sqlite3
import sys
from dataclasses import asdict, fields
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable, Optional

# 확장 루트: .../EXT_ROOT/gist/netai/time_travel_summarization/VLM_server/l40/job_store.py
EXT_ROOT = Path(__file__).resolve().parents[5]
if str(EXT_ROOT) not in sys.path:
    sys.path.insert(0, str(EXT_ROOT))
from gist.netai.time_travel_summarization.automation.remote_generation import (  # noqa: E402
    JOBS_REL, JobSpec,
)

JOBS_DIR = EXT_ROOT / JOBS_REL
_TERMINAL = ("done", "failed")  # 재큐잉 제외 상태

# Postgres 선택적 의존성 가드 — import 실패가 데몬/확장을 죽이지 않는다.
# (minio_adapter와 동일: 없으면 사용 시점에 RuntimeError)
try:
    import psycopg
except Exception:  # pragma: no cover - optional dependency
    psycopg = None


class JobExists(Exception):
    """job_id 중복 register — job_api가 HTTP 409로 매핑."""


def _now() -> datetime:
    # created_at/updated_at는 러너/서버 런타임(kit 스크립트 아님)이라 datetime.now 허용.
    return datetime.now(timezone.utc)


def _spec_to_json(spec: JobSpec) -> str:
    return json.dumps(asdict(spec))


_SPEC_FIELDS = frozenset(f.name for f in fields(JobSpec))  # 스키마 진화 내성용


def _spec_from_raw(raw) -> JobSpec:
    """spec_json → JobSpec. SQLite는 TEXT(str), Postgres JSONB는 dict로 돌려준다.

    미지 키는 무시한다 — JobSpec에서 필드가 제거돼도 구 행 복원이 깨지지 않게
    (필드 추가는 dataclass 기본값이 흡수: 양방향 스키마 내성)."""
    d = json.loads(raw) if isinstance(raw, (str, bytes)) else dict(raw)
    return JobSpec(**{k: v for k, v in d.items() if k in _SPEC_FIELDS})


def _clean_row(row) -> dict:
    """DB 행 → REST용 dict. spec_json 제거(응답 누출 방지), 타임스탬프는 ISO 문자열 통일."""
    d = dict(row)
    d.pop("spec_json", None)
    for k in ("created_at", "updated_at"):
        v = d.get(k)
        if hasattr(v, "isoformat"):  # Postgres는 datetime, SQLite는 이미 str
            d[k] = v.isoformat()
    if "gpu" in d and d["gpu"] is not None:
        d["gpu"] = int(d["gpu"])
    return d


# ---------------------------------------------------------------------------
# SQLite (기본, 단일 노드)
# ---------------------------------------------------------------------------
_SQLITE_TABLE = """
CREATE TABLE IF NOT EXISTS jobs (
  job_id     TEXT PRIMARY KEY,
  job_type   TEXT NOT NULL,
  gpu        INTEGER NOT NULL,
  state      TEXT NOT NULL,
  priority   INTEGER NOT NULL DEFAULT 0,
  attempts   INTEGER NOT NULL DEFAULT 0,
  spec_json  TEXT NOT NULL,
  note       TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
)
"""
_SQLITE_INDEX = (
    "CREATE INDEX IF NOT EXISTS idx_jobs_claim "
    "ON jobs(state, gpu, priority, created_at)"
)


class SqliteJobStore:
    """stdlib sqlite3 백엔드. WAL 모드 + BEGIN IMMEDIATE로 claim_next 원자성 보장.

    스레드마다 연결을 새로 열어(sqlite 연결은 가볍다) 스레드 안전. WAL은 동시 읽기 +
    단일 쓰기를 허용하고, claim_next의 BEGIN IMMEDIATE가 두 워커의 동시 집기를
    직렬화한다(SQLite는 단일 쓰기라 SKIP LOCKED 불필요 — 잠금이 곧 상호배제).
    """

    def __init__(self, path: str):
        self.path = path
        if path != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        con = self._connect()
        try:
            con.execute("PRAGMA journal_mode=WAL")  # 동시 읽기 + 단일 쓰기
            con.execute(_SQLITE_TABLE)
            con.execute(_SQLITE_INDEX)
        finally:
            con.close()

    def _connect(self) -> sqlite3.Connection:
        # isolation_level=None → 수동 트랜잭션(BEGIN IMMEDIATE) 제어. busy_timeout으로
        # 경합 시 대기(즉시 SQLITE_BUSY 방지).
        con = sqlite3.connect(self.path, isolation_level=None, timeout=30)
        con.row_factory = sqlite3.Row
        con.execute("PRAGMA busy_timeout=30000")
        return con

    def register(self, spec: JobSpec) -> None:
        now = _now().isoformat()
        con = self._connect()
        try:
            con.execute(
                "INSERT INTO jobs(job_id, job_type, gpu, state, priority, attempts,"
                " spec_json, note, created_at, updated_at)"
                " VALUES(?,?,?,?,?,?,?,?,?,?)",
                (spec.job_id, spec.job_type, int(spec.gpu), "queued", 0, 0,
                 _spec_to_json(spec), "", now, now),
            )
        except sqlite3.IntegrityError as exc:  # PK 위반 = 중복 잡
            raise JobExists(spec.job_id) from exc
        finally:
            con.close()

    def claim_next(self, gpu: int) -> Optional[JobSpec]:
        con = self._connect()
        try:
            con.execute("BEGIN IMMEDIATE")  # 쓰기 잠금 즉시 획득 → 다른 워커 대기
            row = con.execute(
                "SELECT job_id, spec_json FROM jobs"
                " WHERE state='queued' AND gpu=?"
                " ORDER BY priority DESC, created_at ASC LIMIT 1",
                (int(gpu),),
            ).fetchone()
            if row is None:
                con.execute("COMMIT")
                return None
            con.execute(
                "UPDATE jobs SET state='running', attempts=attempts+1, updated_at=?"
                " WHERE job_id=?",
                (_now().isoformat(), row["job_id"]),
            )
            con.execute("COMMIT")
            return _spec_from_raw(row["spec_json"])
        except Exception:
            con.execute("ROLLBACK")
            raise
        finally:
            con.close()

    def mark(self, job_id: str, state: str, note: str = "",
             extra: Optional[dict] = None) -> None:
        # extra(진행 필드 등)는 DB에 두지 않는다 — 진행은 러너 status 파일이 소스(설계 §3).
        con = self._connect()
        try:
            con.execute(
                "UPDATE jobs SET state=?, note=?, updated_at=? WHERE job_id=?",
                (state, note or "", _now().isoformat(), job_id),
            )
        finally:
            con.close()

    def get(self, job_id: str) -> Optional[dict]:
        con = self._connect()
        try:
            row = con.execute("SELECT * FROM jobs WHERE job_id=?", (job_id,)).fetchone()
            return _clean_row(row) if row else None
        finally:
            con.close()

    def list(self, states: Optional[Iterable[str]] = None) -> list[dict]:
        con = self._connect()
        try:
            if states:
                states = list(states)
                qs = ",".join("?" * len(states))
                rows = con.execute(
                    f"SELECT * FROM jobs WHERE state IN ({qs})"
                    " ORDER BY created_at ASC", states,
                ).fetchall()
            else:
                rows = con.execute(
                    "SELECT * FROM jobs ORDER BY created_at ASC").fetchall()
            return [_clean_row(r) for r in rows]
        finally:
            con.close()

    def requeue_stale(self, older_than_seconds: int = 0) -> int:
        """비종료(queued 아닌 running/starting 등) 잡을 queued로 복원. 되돌린 수 반환.

        older_than_seconds: updated_at이 이 초 이상 오래된 잡만 대상. 0이면 전부 복원
        (단일 노드 데몬 재시작 시 안전 — 살아 있는 다른 노드가 없다). 다중 노드
        Postgres에서 0을 쓰면 **다른 살아있는 노드의 running 잡까지 되돌리므로**,
        그 경우 워커 리스/하트비트가 필요하다(범위 밖, 설계 §9 참고).
        """
        cutoff = (_now() - timedelta(seconds=older_than_seconds)).isoformat()
        con = self._connect()
        try:
            con.execute("BEGIN IMMEDIATE")
            cur = con.execute(
                "UPDATE jobs SET state='queued', updated_at=?"
                " WHERE state NOT IN ('done','failed') AND updated_at<=?",
                (_now().isoformat(), cutoff),
            )
            n = cur.rowcount
            con.execute("COMMIT")
            return n
        except Exception:
            con.execute("ROLLBACK")
            raise
        finally:
            con.close()

    def counts_by_gpu(self) -> dict[int, int]:
        con = self._connect()
        try:
            rows = con.execute(
                "SELECT gpu, COUNT(*) AS c FROM jobs WHERE state='queued'"
                " GROUP BY gpu").fetchall()
            return {int(r["gpu"]): int(r["c"]) for r in rows}
        finally:
            con.close()


# ---------------------------------------------------------------------------
# Postgres (다중 노드) — psycopg3, FOR UPDATE SKIP LOCKED
# ---------------------------------------------------------------------------
_PG_TABLE = """
CREATE TABLE IF NOT EXISTS jobs (
  job_id     VARCHAR PRIMARY KEY,
  job_type   TEXT NOT NULL,
  gpu        INTEGER NOT NULL,
  state      TEXT NOT NULL,
  priority   INTEGER NOT NULL DEFAULT 0,
  attempts   INTEGER NOT NULL DEFAULT 0,
  spec_json  JSONB NOT NULL,
  note       TEXT NOT NULL DEFAULT '',
  created_at TIMESTAMPTZ NOT NULL,
  updated_at TIMESTAMPTZ NOT NULL
)
"""
_PG_INDEX = (
    "CREATE INDEX IF NOT EXISTS idx_jobs_claim "
    "ON jobs(state, gpu, priority, created_at)"
)


class PostgresJobStore:
    """psycopg3 백엔드. 다중 노드가 같은 DB를 보고 SELECT ... FOR UPDATE SKIP LOCKED로
    서로 다른 잡을 논블로킹 병렬 집기(SQLite의 단일 쓰기와 대비)."""

    def __init__(self, dsn: str):
        if psycopg is None:  # 사용 시점에만 실패(import는 무해)
            raise RuntimeError(
                "psycopg package not installed; pip install 'psycopg[binary]'")
        self.dsn = dsn
        with psycopg.connect(self.dsn) as con:
            con.execute(_PG_TABLE)
            con.execute(_PG_INDEX)

    def _dict_cursor(self, con):
        from psycopg.rows import dict_row
        return con.cursor(row_factory=dict_row)

    def register(self, spec: JobSpec) -> None:
        now = _now()
        try:
            with psycopg.connect(self.dsn) as con:
                con.execute(
                    "INSERT INTO jobs(job_id, job_type, gpu, state, priority, attempts,"
                    " spec_json, note, created_at, updated_at)"
                    " VALUES(%s,%s,%s,%s,%s,%s,%s::jsonb,%s,%s,%s)",
                    (spec.job_id, spec.job_type, int(spec.gpu), "queued", 0, 0,
                     _spec_to_json(spec), "", now, now),
                )
        except psycopg.errors.UniqueViolation as exc:
            raise JobExists(spec.job_id) from exc

    def claim_next(self, gpu: int) -> Optional[JobSpec]:
        with psycopg.connect(self.dsn) as con:
            with con.cursor() as cur:
                cur.execute(
                    "SELECT job_id, spec_json FROM jobs"
                    " WHERE state='queued' AND gpu=%s"
                    " ORDER BY priority DESC, created_at ASC"
                    " LIMIT 1 FOR UPDATE SKIP LOCKED",  # 잠긴 행 건너뜀 → 노드끼리 안 겹침
                    (int(gpu),),
                )
                row = cur.fetchone()
                if row is None:
                    return None
                job_id, spec_json = row
                cur.execute(
                    "UPDATE jobs SET state='running', attempts=attempts+1,"
                    " updated_at=%s WHERE job_id=%s",
                    (_now(), job_id),
                )
        return _spec_from_raw(spec_json)

    def mark(self, job_id: str, state: str, note: str = "",
             extra: Optional[dict] = None) -> None:
        with psycopg.connect(self.dsn) as con:
            con.execute(
                "UPDATE jobs SET state=%s, note=%s, updated_at=%s WHERE job_id=%s",
                (state, note or "", _now(), job_id),
            )

    def get(self, job_id: str) -> Optional[dict]:
        with psycopg.connect(self.dsn) as con:
            with self._dict_cursor(con) as cur:
                cur.execute("SELECT * FROM jobs WHERE job_id=%s", (job_id,))
                row = cur.fetchone()
                return _clean_row(row) if row else None

    def list(self, states: Optional[Iterable[str]] = None) -> list[dict]:
        with psycopg.connect(self.dsn) as con:
            with self._dict_cursor(con) as cur:
                if states:
                    cur.execute(
                        "SELECT * FROM jobs WHERE state = ANY(%s)"
                        " ORDER BY created_at ASC", (list(states),))
                else:
                    cur.execute("SELECT * FROM jobs ORDER BY created_at ASC")
                return [_clean_row(r) for r in cur.fetchall()]

    def requeue_stale(self, older_than_seconds: int = 0) -> int:
        """비종료 잡을 queued로 복원. SqliteJobStore.requeue_stale와 동일 의미론.
        다중 노드에서 older_than_seconds=0은 살아있는 다른 노드의 잡까지 되돌리므로
        주의(워커 리스/하트비트 필요 — 범위 밖, 설계 §9)."""
        cutoff = _now() - timedelta(seconds=older_than_seconds)
        with psycopg.connect(self.dsn) as con:
            with con.cursor() as cur:
                cur.execute(
                    "UPDATE jobs SET state='queued', updated_at=%s"
                    " WHERE state NOT IN ('done','failed') AND updated_at<=%s",
                    (_now(), cutoff),
                )
                return cur.rowcount

    def counts_by_gpu(self) -> dict[int, int]:
        with psycopg.connect(self.dsn) as con:
            with con.cursor() as cur:
                cur.execute(
                    "SELECT gpu, COUNT(*) FROM jobs WHERE state='queued' GROUP BY gpu")
                return {int(g): int(c) for g, c in cur.fetchall()}


# ---------------------------------------------------------------------------
# 팩토리 (storage/factory.py 동형)
# ---------------------------------------------------------------------------
def store_from_url(url: Optional[str] = None):
    """URL로 백엔드 선택. 미지정 시 기본 sqlite:///<JOBS_DIR>/jobs.db.

    sqlite:///<abs_path>  → SqliteJobStore   (예: sqlite:////mnt/.../jobs.db)
    postgres[ql]://...    → PostgresJobStore
    """
    url = url or f"sqlite:///{JOBS_DIR / 'jobs.db'}"
    if url.startswith("sqlite:///"):
        return SqliteJobStore(url[len("sqlite:///"):])
    if url.startswith("sqlite:"):
        return SqliteJobStore(url[len("sqlite:"):])
    if url.startswith("postgres"):  # postgres:// | postgresql://
        return PostgresJobStore(url)
    raise ValueError(f"unsupported JOB_STORE_URL scheme: {url!r}")


# ---------------------------------------------------------------------------
# self-test (SQLite, WSL stdlib에서 실행 가능)
# ---------------------------------------------------------------------------
def _self_test() -> None:
    import tempfile
    import threading

    tmp = tempfile.mkdtemp(prefix="jobstore_")
    store = SqliteJobStore(str(Path(tmp) / "jobs.db"))

    # 1) register → claim → mark 라이프사이클
    spec = JobSpec(job_id="gen-1", gpu=1, episodes=3)
    store.register(spec)
    got = store.get("gen-1")
    assert got and got["state"] == "queued" and got["gpu"] == 1, got
    assert store.counts_by_gpu() == {1: 1}, store.counts_by_gpu()
    claimed = store.claim_next(1)
    assert claimed is not None and claimed.job_id == "gen-1", claimed
    assert isinstance(claimed, JobSpec) and claimed.episodes == 3
    assert store.get("gen-1")["state"] == "running"
    assert store.claim_next(1) is None, "이미 집힌 잡은 다시 안 나와야"
    store.mark("gen-1", "done", note="ok")
    assert store.get("gen-1")["state"] == "done"
    assert store.get("gen-1")["note"] == "ok"

    # 2) dedup — 중복 register는 JobExists
    try:
        store.register(JobSpec(job_id="gen-1", gpu=1))
        assert False, "중복 register가 예외를 던져야"
    except JobExists:
        pass

    # 2-1) 스키마 내성 — 구 행에 남은 미지 키(제거된 필드)는 무시하고 복원
    legacy = dict(asdict(spec), removed_field="x")
    assert _spec_from_raw(json.dumps(legacy)).job_id == "gen-1"

    # 3) requeue_stale — running 잔류 잡을 queued로 복원
    store.register(JobSpec(job_id="gen-2", gpu=1))
    assert store.claim_next(1).job_id == "gen-2"
    assert store.get("gen-2")["state"] == "running"
    n = store.requeue_stale(0)
    assert n == 1, f"복원 1건 기대, {n}"
    assert store.get("gen-2")["state"] == "queued"
    assert store.get("gen-1")["state"] == "done", "종료 잡은 재큐잉되지 않아야"

    # 4) gpu별 격리 + list/counts
    store.register(JobSpec(job_id="gen-3", gpu=2))
    assert store.claim_next(2).job_id == "gen-3"  # gpu2는 자기 잡만
    assert store.claim_next(2) is None
    states = {j["job_id"]: j["state"] for j in store.list()}
    assert states == {"gen-1": "done", "gen-2": "queued", "gen-3": "running"}, states
    assert [j["job_id"] for j in store.list(states=["queued"])] == ["gen-2"]

    # 5) 원자적 동시 claim — 스레드 2개가 같은 잡을 두 번 집지 않음
    store2 = SqliteJobStore(str(Path(tmp) / "concurrent.db"))
    N = 50
    for i in range(N):
        store2.register(JobSpec(job_id=f"c-{i:03d}", gpu=1))
    claimed_ids: list[str] = []
    lock = threading.Lock()

    def drain():
        while True:
            s = store2.claim_next(1)
            if s is None:
                return
            with lock:
                claimed_ids.append(s.job_id)

    threads = [threading.Thread(target=drain) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(claimed_ids) == N, f"{len(claimed_ids)} != {N}"
    assert len(set(claimed_ids)) == N, "같은 잡이 두 번 집혔다(원자성 위반)"

    # 6) store_from_url — sqlite 절대경로 파싱
    url_store = store_from_url(f"sqlite:///{Path(tmp) / 'viafactory.db'}")
    assert isinstance(url_store, SqliteJobStore)
    url_store.register(JobSpec(job_id="f-1", gpu=1))
    assert url_store.claim_next(1).job_id == "f-1"
    try:
        store_from_url("mysql://x")
        assert False, "미지원 스킴은 ValueError"
    except ValueError:
        pass

    print("job_store self-test OK")


if __name__ == "__main__":
    _self_test()
