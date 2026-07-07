from dataclasses import dataclass, field
from typing import Optional


@dataclass(frozen=True)
class CaptureRequest:
    duration_s: float
    fps: int = 30
    width: int = 720
    height: int = 480
    output_uri: str = ""
    label: str = ""
    # headless 렌더 데시메이션: sim은 fps(60Hz)로 전진하되 렌더·인코딩은 이 fps로만.
    # None = fps와 동일(데시메이션 없음). fps의 약수여야 한다.
    render_fps: Optional[int] = None


@dataclass(frozen=True)
class CaptureResult:
    success: bool
    output_uri: str
    wall_clock_s: float
    output_size_bytes: int
    sim_fps_avg: Optional[float] = None
    dropped_frames: int = 0
    error: Optional[str] = None
    metadata: dict = field(default_factory=dict)
