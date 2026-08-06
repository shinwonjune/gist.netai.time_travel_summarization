"""GUI E2E 재생 계측(레이크성능_실험설계.md §2-C) — env TTS_LAKE_PROBE=1 또는 GUI 토글로 활성.

facade.update(dt)가 매 앱 프레임 record()를 호출한다. 기본(env 미설정·GUI 체크 해제)
에는 인스턴스 자체가 만들어지지 않아 계측 코드가 실행되지 않는다(무부하).

프레임당 기록(perf_counter 2회 + append 수준 — 오버헤드 무시 가능):
  wall_ts            perf_counter 기준 시각(초)
  twin_time          트윈 시계(포맷 문자열, 없으면 None)
  frame_interval_ms  직전 update와의 벽시계 간격 — hitch 판정의 원료
  tick_ms            controller.update 소요(게이트로 스킵된 프레임은 ~0)
  d_sync             repo stats.sync_loads 증분 = 이 프레임의 stall 여부
  d_hit              repo stats.cache_hits 증분
  playing            재생 중 플래그(후처리에서 재생/스크럽 구간 분리용)

링버퍼 상한 36,000 프레임(10분@60fps). 덤프 트리거: 재생 정지(playing→not)
전이, 상한 도달, 또는 GUI의 Dump 버튼(수동) → artifacts/benchmarks/
gui_probe_<YYYYMMDD-HHMMSS>[_<scenario>].json. 스크럽은 정지 전이가 없어
수동 덤프가 유일한 경계이고, 구간 시작은 reset()(GUI Start)이 잡는다.
후처리(지표 표)는 utils/gui_probe_report.py. 로그 문자열은 ASCII만 사용.
"""
from __future__ import annotations

import datetime
import json
import time
from pathlib import Path
from typing import Optional

try:
    import carb
except ImportError:  # pragma: no cover - headless 테스트(Kit 밖)
    class _CarbFallback:
        @staticmethod
        def log_warn(*_args, **_kwargs):
            pass

    carb = _CarbFallback()

MAX_FRAMES = 36000  # 10분 @ 60fps
FPS_WINDOW = 60     # 최근 fps 산출 창(프레임) — O(1) 인덱싱만 쓴다
SCENARIO_MAX_LEN = 32


def sanitize_scenario(name) -> str:
    """시나리오 라벨을 파일명 안전 문자(ASCII 영숫자·대시·언더스코어)로 정규화.

    나머지 문자는 대시로 치환하고 **뒤쪽** 대시만 떨어낸다. 파일명에 그대로 들어가므로
    한글·공백·경로 구분자가 섞여도 안전해야 한다.

    앞쪽 대시는 남긴다 — 역방향 재생 라벨 "-1x"의 부호가 실험 구분의 핵심이라
    떼어내면 "1x"와 같아져 시나리오가 뒤섞인다(실측 검토 2026-08-06). 파일명은
    항상 `gui_probe_<시각>_<라벨>.json` 꼴이라 라벨이 맨 앞에 오지 않으므로,
    선행 대시가 CLI 옵션으로 오해될 여지도 없다.
    """
    if not name:
        return ""
    out = [c if (c.isascii() and (c.isalnum() or c in "-_")) else "-" for c in str(name)]
    return "".join(out).rstrip("-")[:SCENARIO_MAX_LEN].rstrip("-")


class LakeProbe:
    def __init__(self, out_dir: Optional[Path] = None, max_frames: int = MAX_FRAMES,
                 scenario: str = ""):
        # EXT_ROOT/artifacts/benchmarks (app/ -> tts -> netai -> gist -> EXT_ROOT)
        self._out_dir = Path(out_dir) if out_dir else \
            Path(__file__).resolve().parents[4] / "artifacts" / "benchmarks"
        self._max_frames = int(max_frames)
        self._last_wall: Optional[float] = None
        self._last_sync = 0
        self._last_hit = 0
        self._was_playing = False
        self._scenario = sanitize_scenario(scenario)
        self._reset_buffer()

    def _reset_buffer(self):
        # 컬럼형 저장 — dict 리스트보다 JSON 크기·append 비용이 작다
        self._wall_ts = []
        self._twin = []
        self._interval_ms = []
        self._tick_ms = []
        self._d_sync = []
        self._d_hit = []
        self._playing = []
        self._stall_frames = 0  # 러닝 카운터 — live_stats가 버퍼를 훑지 않게

    def __len__(self) -> int:
        return len(self._wall_ts)

    # ---- 라벨 / 구간 경계 -------------------------------------------------

    def set_scenario(self, name) -> str:
        """시나리오 라벨(1x, 5x, -1x, scrub-fast ...) 설정. 정규화된 값을 반환."""
        self._scenario = sanitize_scenario(name)
        return self._scenario

    def get_scenario(self) -> str:
        return self._scenario

    def reset(self) -> int:
        """파일을 쓰지 않고 버퍼만 비운다(GUI Start = 측정 구간 시작점).

        반환값은 버려진 프레임 수.
        """
        n = len(self._wall_ts)
        self._reset_buffer()
        self._was_playing = False
        self._last_wall = None
        return n

    def live_stats(self) -> dict:
        """UI 표시용 경량 통계 — 버퍼 전체 스캔 없이 O(1)로 계산한다."""
        n = len(self._wall_ts)
        fps = 0.0
        if n >= 2:
            k = min(FPS_WINDOW, n - 1)
            span = self._wall_ts[-1] - self._wall_ts[-1 - k]
            if span > 0:
                fps = k / span
        return {"frames": n, "stalls": self._stall_frames, "fps": fps,
                "scenario": self._scenario}

    def record(self, tick_ms: float, twin_time, stats, is_playing: bool) -> None:
        """매 앱 프레임 호출. stats = repository.stats dict(레이크 아니면 None)."""
        now = time.perf_counter()
        interval_ms = (now - self._last_wall) * 1000 if self._last_wall is not None else 0.0
        self._last_wall = now

        sync = int(stats.get("sync_loads", 0)) if stats else 0
        hit = int(stats.get("cache_hits", 0)) if stats else 0
        d_sync = max(0, sync - self._last_sync)
        d_hit = max(0, hit - self._last_hit)
        self._last_sync, self._last_hit = sync, hit

        self._wall_ts.append(round(now, 4))
        self._twin.append(twin_time.strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
                          if twin_time is not None else None)
        self._interval_ms.append(round(interval_ms, 2))
        self._tick_ms.append(round(tick_ms, 3))
        self._d_sync.append(d_sync)
        self._d_hit.append(d_hit)
        self._playing.append(bool(is_playing))
        if d_sync:
            self._stall_frames += 1

        stopped = self._was_playing and not is_playing
        self._was_playing = is_playing
        if stopped or len(self._wall_ts) >= self._max_frames:
            self.dump(reason="stop" if stopped else "cap")

    def dump(self, reason: str = "manual") -> Optional[Path]:
        """버퍼를 JSON으로 저장하고 비운다. 빈 버퍼면 no-op."""
        if not self._wall_ts:
            return None
        self._out_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
        suffix = f"_{self._scenario}" if self._scenario else ""
        path = self._out_dir / f"gui_probe_{stamp}{suffix}.json"
        payload = {
            "version": 1,
            "reason": reason,
            "scenario": self._scenario,
            "n_frames": len(self._wall_ts),
            "frames": {
                "wall_ts": self._wall_ts,
                "twin_time": self._twin,
                "frame_interval_ms": self._interval_ms,
                "tick_ms": self._tick_ms,
                "d_sync": self._d_sync,
                "d_hit": self._d_hit,
                "playing": self._playing,
            },
        }
        path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        carb.log_warn(f"[TimeTravel] lake probe dump: {path.name} frames={len(self._wall_ts)} reason={reason}")
        self._reset_buffer()
        return path
