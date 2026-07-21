"""데이터 소스(config/local/lake) 활성화 — 상태는 core, 동작은 여기.

TrajectoryRepository/EventSummaryService는 모듈 레벨 이름으로 두어
테스트가 monkeypatch로 대체할 수 있게 유지한다.
"""
from pathlib import Path
from typing import Any, Callable, Optional

import carb

from .config import ExtensionConfig
from ..events.summary_service import EventSummaryService
from ..playback.trajectory_repository import TrajectoryRepository


def load_config(core, config_path: str) -> bool:
    try:
        path = Path(config_path)
        if not path.exists():
            core._last_data_load_error = f"Config file not found: {config_path}"
            carb.log_warn(f"[TimeTravel] {core._last_data_load_error}")
            return False

        core._config = ExtensionConfig.from_file(config_path)
        core._prim_map = dict(core._config.prim_map)
        core._playback.set_event_summary(core._config.event_summary)
        core._last_data_load_error = ""
        carb.log_info("[TimeTravel] Config loaded")
        return True
    except Exception as e:
        core._last_data_load_error = f"Failed to load config: {e}"
        carb.log_error(f"[TimeTravel] {core._last_data_load_error}")
        return False


def resolve_uri(core, value: str) -> Optional[str]:
    """'://'가 있으면 그대로, 아니면 config 디렉터리 기준 로컬 경로 -> file:// URI."""
    if not value:
        return None
    if "://" in value:
        return value
    path = Path(value)
    if not path.is_absolute():
        # Path 결합이 './' '../'를 정상 처리. lstrip 문자집합 제거 버그 회피.
        path = core._config.config_dir / value
    return path.resolve().as_uri()


def resolve_output_uri_for_mode(core, mode: str, value: str) -> Optional[str]:
    """Return configured output URI only when the active data source is Data Lake."""
    if mode != "lake":
        return None
    return resolve_uri(core, value)


def activate_data_source(core, mode: str) -> bool:
    try:
        if not core._config:
            core._last_data_load_error = "Config must be loaded before data"
            carb.log_error(f"[TimeTravel] {core._last_data_load_error}")
            return False

        lake_cfg = getattr(core._config, "lake", {}) or {}
        repo_factory: Callable[[], Any]
        if mode == "lake":
            direct_data_uri = resolve_uri(core, lake_cfg.get("direct_data_uri", ""))
            if direct_data_uri:
                repo_factory = TrajectoryRepository
                uri = direct_data_uri
                log_message = f"[TimeTravel] Lake mode test direct data URI: {uri}"
            else:
                from ..playback.lake_repository import LakeTrajectoryRepository

                manifest_uri = resolve_uri(core, lake_cfg.get("manifest_uri", ""))
                if not manifest_uri:
                    core._last_data_load_error = "Data Lake manifest URI is not configured"
                    carb.log_warn(f"[TimeTravel] {core._last_data_load_error}")
                    return False
                cache_chunks = int(lake_cfg.get("cache_chunks", 4))
                prefetch_ahead = int(lake_cfg.get("prefetch_ahead", 1))
                repo_factory = lambda: LakeTrajectoryRepository(
                    cache_chunks=cache_chunks,
                    prefetch_ahead=prefetch_ahead,
                )
                uri = manifest_uri
                log_message = f"[TimeTravel] Lake mode: manifest URI: {uri}"
        elif mode == "local":
            repo_factory = TrajectoryRepository
            uri = core._config.data_uri
            log_message = f"[TimeTravel] Looking for data at URI: {uri}"
        else:
            core._last_data_load_error = f"Invalid data source: {mode}"
            carb.log_warn(f"[TimeTravel] {core._last_data_load_error}")
            return False

        output_root_uri = resolve_output_uri_for_mode(
            core,
            mode,
            getattr(core._config, "output_root_uri", ""),
        )
        repo = repo_factory()
        carb.log_info(log_message)

        loaded = repo.load_from_uri(uri)
        if not loaded:
            core._last_data_load_error = f"Data load failed for URI: {uri}"
            carb.log_error(f"[TimeTravel] {core._last_data_load_error}")
            return False

        old_repository = getattr(core, "_repository", None)
        if old_repository and hasattr(old_repository, "clear"):
            old_repository.clear()

        core._repository = repo
        core._events = EventSummaryService(
            core._module_dir,
            core._repository,
            output_root_uri=output_root_uri,
        )
        core._playback.configure_data_range(
            core._repository.data_start_time,
            core._repository.data_end_time,
        )
        carb.log_info(
            f"[TimeTravel] Data loaded: {len(core._repository.timestamps)} timestamps, "
            f"{core._repository.data_start_time} to {core._repository.data_end_time}"
        )
        core._data_source = mode
        core._last_data_load_error = ""
        if mode == "lake":
            prim_map = core.regenerate_astronauts_from_loaded_data()
            if not prim_map:
                carb.log_warn("[TimeTravel] Data Lake data loaded, but no astronauts were generated")
            core.update_stage_objects()
            carb.log_warn(
                f"[TimeTravel] Data Lake activation complete: "
                f"timestamps={len(core._repository.timestamps)}, "
                f"objects={core.get_loaded_object_count()}, "
                f"astronauts={len(core._prim_map)}"
            )
        return True
    except Exception as e:
        core._last_data_load_error = f"Failed to load data: {e}"
        carb.log_error(f"[TimeTravel] {core._last_data_load_error}")
        import traceback

        carb.log_error(traceback.format_exc())
        return False


def load_data(core) -> bool:
    try:
        if not core._config:
            core._last_data_load_error = "Config must be loaded before data"
            carb.log_error(f"[TimeTravel] {core._last_data_load_error}")
            return False

        lake_cfg = getattr(core._config, "lake", {}) or {}
        initial = "lake" if lake_cfg.get("enabled") and lake_cfg.get("manifest_uri") else "local"
        return activate_data_source(core, initial)
    except Exception as e:
        core._last_data_load_error = f"Failed to load data: {e}"
        carb.log_error(f"[TimeTravel] {core._last_data_load_error}")
        return False


def set_data_source(core, mode: str) -> bool:
    if mode not in ("local", "lake"):
        core._last_data_load_error = f"Invalid data source: {mode}"
        carb.log_warn(f"[TimeTravel] {core._last_data_load_error}")
        return False
    if mode == "lake":
        lake_cfg = getattr(core._config, "lake", {}) or {}
        if not lake_cfg.get("direct_data_uri") and not lake_cfg.get("manifest_uri"):
            core._last_data_load_error = "Data Lake manifest URI is not configured"
            carb.log_warn(f"[TimeTravel] {core._last_data_load_error}")
            return False
    if activate_data_source(core, mode):
        core.set_to_earliest_time()
        return True
    return False
