"""객체 재생성의 objid 합집합·prim 인덱스 규칙 — frag '투명 충돌' 회귀 방지."""
from gist.netai.time_travel_summarization.app.object_service import _prim_index_for
from gist.netai.time_travel_summarization.playback.trajectory_repository import (
    TrajectoryRepository,
)


def _repo_with(rows):
    repo = TrajectoryRepository()
    repo._data = {}
    for ts, objid, pos in rows:
        repo._data.setdefault(ts, {})[objid] = pos
    repo._timestamps = sorted(repo._data)
    return repo


def test_get_object_ids_includes_late_appearing_tracks():
    """시작 시각에 없는 objid(중간 등장 트랙)도 합집합에 포함 — frag의 obj005."""
    rows = [("2026-07-22 12:00:00.000", "obj001", (0, 0, 0)),
            ("2026-07-22 12:00:00.000", "obj002", (1, 1, 1)),
            ("2026-07-22 12:00:10.000", "obj005", (2, 2, 2))]
    repo = _repo_with(rows)
    assert repo.get_object_ids() == ["obj001", "obj002", "obj005"]


def test_prim_index_follows_objid_digits():
    """라벨(프림 이름 끝 3자리)이 데이터 ID를 따라가야 한다 — 불연속 objid 포함."""
    used: set = set()
    assert _prim_index_for("obj001", fallback=1, used=used) == 1
    used.add(1)
    # 불연속: obj003이 두 번째여도 인덱스 3 (enumerate 순번 2가 아니라)
    assert _prim_index_for("obj003", fallback=2, used=used) == 3
    used.add(3)
    assert _prim_index_for("obj005", fallback=3, used=used) == 5


def test_prim_index_fallback_for_nonstandard_ids():
    used = {1, 2}
    # 숫자 아님 -> 폴백, 사용 중이면 다음 빈 번호
    assert _prim_index_for("cart_A", fallback=1, used=used) == 3
    # 숫자 충돌 -> 폴백
    assert _prim_index_for("obj001", fallback=2, used={1}) == 2
