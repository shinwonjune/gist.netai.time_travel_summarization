import json
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from ..app.paths import ExtensionPaths
from ..storage import from_uri


class EventSummaryService:
    def __init__(self, module_dir: Path, repository, output_root_uri: Optional[str] = None):
        self._module_dir = module_dir
        self._repository = repository
        self._event_positions: Dict[str, Tuple[float, float, float]] = {}
        self._paths = ExtensionPaths(module_dir)
        self._output_root_uri = output_root_uri

    def get_event_position(self, timestamp: str):
        return self._event_positions.get(timestamp)

    def load_events_from_event_list(self) -> List[str]:
        eventlist_files = list(self._paths.event_list_dir.glob("*_eventlist.jsonl"))
        if not eventlist_files:
            legacy_dir = self._module_dir / "event_list"
            if legacy_dir.exists():
                eventlist_files = list(legacy_dir.glob("*_eventlist.jsonl"))
        if not eventlist_files:
            return []

        latest_file = max(eventlist_files, key=lambda path: path.stat().st_mtime)
        event_timestamps: List[str] = []
        event_positions: Dict[str, Tuple[float, float, float]] = {}

        with open(latest_file, "r", encoding="utf-8") as file:
            for line in file:
                entry = json.loads(line)
                timestamp = entry.get("timestamp")
                position = entry.get("position")
                if not timestamp or not position:
                    continue

                event_timestamps.append(timestamp)
                event_positions[timestamp] = (
                    position.get("x", 0),
                    position.get("y", 0),
                    position.get("z", 0),
                )

        self._event_positions = event_positions
        return event_timestamps

    def process_event_json(self, json_path: str) -> bool:
        from .core import consolidate_events, load_json, save_jsonl

        source_path = Path(json_path)
        if not source_path.exists():
            return False

        vlm_data = load_json(str(source_path))
        events = consolidate_events(vlm_data, base_date="2025-01-01")

        output_jsonl_uri = self._resolve_output_uri(
            "intermediate_results",
            f"{source_path.stem}_intermediate.jsonl",
        )
        serialized_events = "".join(
            json.dumps({timestamp: events[timestamp]}, ensure_ascii=False) + "\n"
            for timestamp in sorted(events.keys())
        )
        from_uri(output_jsonl_uri).put_bytes(
            output_jsonl_uri,
            serialized_events.encode("utf-8"),
            content_type="application/x-ndjson",
        )

        event_list = self._generate_event_list(events)
        if not event_list:
            return False

        output_eventlist_uri = self._resolve_output_uri(
            "event_list",
            f"{source_path.stem}_eventlist.jsonl",
        )
        serialized_event_list = "".join(
            json.dumps(entry, ensure_ascii=False) + "\n"
            for entry in event_list
        )
        from_uri(output_eventlist_uri).put_bytes(
            output_eventlist_uri,
            serialized_event_list.encode("utf-8"),
            content_type="application/x-ndjson",
        )

        self._event_positions = {
            entry["timestamp"]: (
                entry["position"]["x"],
                entry["position"]["y"],
                entry["position"]["z"],
            )
            for entry in event_list
        }
        return True

    def _resolve_output_uri(self, subdir: str, filename: str) -> str:
        """Return URI for writing an artifact. Falls back to local Path's file:// URI."""
        if self._output_root_uri:
            root = self._output_root_uri.rstrip("/")
            return f"{root}/{subdir}/{filename}"
        if subdir == "intermediate_results":
            base = self._paths.intermediate_results_dir
        elif subdir == "event_list":
            base = self._paths.event_list_dir
        else:
            base = self._paths.artifacts_dir / subdir
        return (base / filename).resolve().as_uri()

    def _generate_event_list(self, events: Dict[str, List[List[str]]]) -> List[Dict]:
        position_data = []
        for timestamp, obj_pairs in events.items():
            if not obj_pairs or not obj_pairs[0]:
                continue

            first_objid = obj_pairs[0][0]
            time_obj = self._repository.parse_timestamp(timestamp)
            time_data = self._repository.get_data_at_time(time_obj)
            if first_objid not in time_data:
                continue

            x, y, z = time_data[first_objid]
            position_data.append(
                {
                    "timestamp": timestamp,
                    "objid": first_objid,
                    "position": {"x": x, "y": y, "z": z},
                }
            )

        return position_data
