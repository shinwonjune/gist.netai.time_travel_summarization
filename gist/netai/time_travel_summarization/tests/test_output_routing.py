import sys
import types
import datetime
from types import SimpleNamespace


def _install_carb_stub():
    carb = types.ModuleType("carb")
    carb.log_info = lambda *args, **kwargs: None
    carb.log_warn = lambda *args, **kwargs: None
    carb.log_error = lambda *args, **kwargs: None
    sys.modules["carb"] = carb

    stage_module = types.ModuleType(
        "gist.netai.time_travel_summarization.playback.stage_object_controller"
    )
    stage_module.StageObjectController = object
    sys.modules[
        "gist.netai.time_travel_summarization.playback.stage_object_controller"
    ] = stage_module


_install_carb_stub()

from gist.netai.time_travel_summarization.app import data_service  # noqa: E402
from gist.netai.time_travel_summarization.app import facade as facade_module  # noqa: E402
from gist.netai.time_travel_summarization.app.facade import TimeTravelCore  # noqa: E402


def _core_with_source(mode: str):
    core = TimeTravelCore.__new__(TimeTravelCore)
    core._data_source = mode
    core._config = SimpleNamespace(
        output_root_uri="s3://bucket/timetravel",
        event_list_uri="s3://bucket/timetravel/event_list",
        video_output_uri="s3://bucket/timetravel/video",
    )
    return core


def test_local_source_uses_local_artifact_fallbacks_for_configured_outputs():
    core = _core_with_source("local")

    assert core.get_output_root_uri_for_active_mode() is None
    assert core.get_event_list_uri_for_active_mode() is None
    assert core.get_video_output_uri_for_active_mode() is None


def test_lake_source_uses_configured_output_uris():
    core = _core_with_source("lake")

    assert core.get_output_root_uri_for_active_mode() == "s3://bucket/timetravel"
    assert core.get_event_list_uri_for_active_mode() == "s3://bucket/timetravel/event_list"
    assert core.get_video_output_uri_for_active_mode() == "s3://bucket/timetravel/video"


def test_lake_source_switch_regenerates_astronauts_even_when_auto_generate_is_off(monkeypatch):
    class DummyRepository:
        def __init__(self):
            self.timestamps = ["2025-01-01 00:00:00.000"]
            self.data_start_time = datetime.datetime(2025, 1, 1)
            self.data_end_time = datetime.datetime(2025, 1, 1, 0, 1)

        def load_from_uri(self, uri):
            self.loaded_uri = uri
            return True

        def clear(self):
            pass

        def get_object_ids(self):
            return ["obj001", "obj002"]

        def get_data_at_time(self, timestamp):
            return {"obj001": (1.0, 2.0, 3.0), "obj002": (4.0, 5.0, 6.0)}

    class DummyPlayback:
        def configure_data_range(self, start, end):
            self.current = start

        def set_current_time(self, current):
            self.current = current

        def get_current_time(self):
            return self.current

    class DummyStageObjects:
        def __init__(self):
            self.created = []

        def clear_timetravel_objects(self):
            pass

        def create_astronaut_prim(self, index, astronaut_usd):
            self.created.append((index, astronaut_usd))
            return f"/World/TimeTravel_Objects/Astronaut{index:03d}"

        def update_stage_objects(self, prim_map, data):
            self.updated = (dict(prim_map), dict(data))

        def hide_all_cameras(self):
            pass

    # 데이터 소스 활성화 로직은 data_service로 분해됨 → 패치 대상도 그쪽
    monkeypatch.setattr(data_service, "TrajectoryRepository", DummyRepository)
    monkeypatch.setattr(data_service, "EventSummaryService", lambda *args, **kwargs: object())

    stage_objects = DummyStageObjects()
    core = TimeTravelCore.__new__(TimeTravelCore)
    core._module_dir = facade_module.Path(__file__).resolve().parents[1]
    core._config = SimpleNamespace(
        auto_generate=False,
        astronaut_usd="",
        output_root_uri="",
        lake={"direct_data_uri": "s3://bucket/trajectory/sample.csv"},
    )
    core._repository = DummyRepository()
    core._events = object()
    core._playback = DummyPlayback()
    core._stage_objects = stage_objects
    core._wander = None
    core._prim_map = {}
    core._data_source = "local"
    core._last_data_load_error = ""

    assert core.set_data_source("lake") is True
    assert core.get_data_source() == "lake"
    assert [index for index, _ in stage_objects.created] == [1, 2]
    assert core._prim_map == {
        "obj001": "/World/TimeTravel_Objects/Astronaut001",
        "obj002": "/World/TimeTravel_Objects/Astronaut002",
    }
