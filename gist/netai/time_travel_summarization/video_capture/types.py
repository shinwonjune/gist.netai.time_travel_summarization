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
