import json
import sys
import tempfile
import types
from pathlib import Path


def _install_carb_stub():
    carb = types.ModuleType("carb")
    carb.log_info = lambda *args, **kwargs: None
    carb.log_warn = lambda *args, **kwargs: None
    carb.log_error = lambda *args, **kwargs: None
    sys.modules["carb"] = carb


_install_carb_stub()

from gist.netai.time_travel_summarization.event_processing.summary_service import (  # noqa: E402
    EventSummaryService,
)


def test_load_events_from_file_event_list_uri_preserves_order_and_positions():
    with tempfile.TemporaryDirectory() as tmpdir:
        root = Path(tmpdir)
        eventlist_path = root / "sample_eventlist.jsonl"
        rows = [
            {
                "timestamp": "2025-01-01 00:00:01",
                "objid": "1",
                "position": {"x": 1.0, "y": 2.0, "z": 3.0},
            },
            {
                "timestamp": "2025-01-01 00:00:02",
                "objid": "2",
                "position": {"x": 4.5, "y": 5.5, "z": 6.5},
            },
        ]
        eventlist_path.write_text(
            "".join(json.dumps(row) + "\n" for row in rows),
            encoding="utf-8",
        )

        service = EventSummaryService(module_dir=root, repository=None)

        timestamps = service.load_events_from_event_list(event_list_uri=root.as_uri())

        assert timestamps == ["2025-01-01 00:00:01", "2025-01-01 00:00:02"]
        assert service.get_event_position("2025-01-01 00:00:01") == (1.0, 2.0, 3.0)
        assert service.get_event_position("2025-01-01 00:00:02") == (4.5, 5.5, 6.5)


def test_resolve_output_uri_uses_lake_root_for_event_list():
    service = EventSummaryService(
        module_dir=Path.cwd(),
        repository=None,
        output_root_uri="s3://bucket/prefix",
    )

    assert (
        service._resolve_output_uri("event_list", "x_eventlist.jsonl")
        == "s3://bucket/prefix/event_list/x_eventlist.jsonl"
    )


def test_resolve_output_uri_falls_back_to_file_uri_for_backward_compatibility():
    service = EventSummaryService(
        module_dir=Path.cwd(),
        repository=None,
        output_root_uri=None,
    )

    uri = service._resolve_output_uri("event_list", "x_eventlist.jsonl")

    assert uri.startswith("file://")
    assert uri.endswith("/x_eventlist.jsonl")


def _run_test(name, func):
    func()
    print(f"PASS {name}")


if __name__ == "__main__":
    _run_test(
        "test_load_events_from_file_event_list_uri_preserves_order_and_positions",
        test_load_events_from_file_event_list_uri_preserves_order_and_positions,
    )
    _run_test(
        "test_resolve_output_uri_uses_lake_root_for_event_list",
        test_resolve_output_uri_uses_lake_root_for_event_list,
    )
    _run_test(
        "test_resolve_output_uri_falls_back_to_file_uri_for_backward_compatibility",
        test_resolve_output_uri_falls_back_to_file_uri_for_backward_compatibility,
    )
    print("ALL PASS")
