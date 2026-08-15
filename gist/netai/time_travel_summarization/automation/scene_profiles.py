"""씬 프로파일 로더 — 생성 잡의 씬 정의(아레나 범위·스테이지·카메라)를 이름으로 해석.

생성 파이프라인이 궤적 CSV에 암묵 의존하던 것(objid 풀 + coord_range)을 명시
파라미터로 승격한 것이다(2026-08-15). 레지스트리는 확장 루트의
``scene_profiles.json``(추적 파일, 머신 중립)이고, 새 공간 추가 = 항목 추가,
잡 제출 = 이름 지정으로 끝난다. 순수 stdlib — Kit 없이 테스트 가능.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Dict, Optional, Tuple

_REQUIRED = ("stage", "camera", "coord_min", "coord_max")
_EXT_ROOT_MARKER = "scene_profiles.json"


def registry_path(ext_pkg_dir: Optional[Path] = None) -> Path:
    """레지스트리 파일 경로. 기본 = 이 모듈의 패키지 루트(확장 파이썬 루트)."""
    root = ext_pkg_dir or Path(__file__).resolve().parent.parent
    return root / _EXT_ROOT_MARKER


def load_profile(name: str, ext_pkg_dir: Optional[Path] = None) -> Dict:
    """이름으로 프로파일 로드 + 필수 키 검증. 없는 이름이면 목록을 담아 에러."""
    path = registry_path(ext_pkg_dir)
    raw = json.loads(path.read_text(encoding="utf-8"))
    profiles = {k: v for k, v in raw.items() if not k.startswith("_")}
    if name not in profiles:
        raise KeyError(
            f"scene profile {name!r} not in {path} (available: {sorted(profiles)})")
    prof = dict(profiles[name])
    missing = [k for k in _REQUIRED if not prof.get(k)]
    if missing:
        raise ValueError(f"scene profile {name!r} missing keys: {missing}")
    for key in ("coord_min", "coord_max"):
        vals = prof[key]
        if len(vals) != 3:
            raise ValueError(f"scene profile {name!r}: {key} must be [x, y, z]")
        prof[key] = tuple(float(v) for v in vals)
    return prof


def coord_range_of(prof: Dict) -> Tuple[Tuple[float, float, float],
                                        Tuple[float, float, float]]:
    """physics_service가 소비하는 (mins, maxs) 형태로 반환."""
    return prof["coord_min"], prof["coord_max"]
