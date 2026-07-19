import json
from pathlib import Path
from urllib.parse import urlparse
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

    def load_events_from_event_list(self, event_list_uri: Optional[str] = None) -> List[str]:
        if event_list_uri:
            # minIO/S3 prefix는 디렉터리 경계를 위해 trailing slash가 필요. 없으면
            # recursive=False가 폴더 내부 파일을 못 찾고 빈 결과를 낸다(로컬은 무해).
            prefix = event_list_uri if event_list_uri.endswith("/") else event_list_uri + "/"
            adapter = from_uri(prefix)
            eventlist_files = [
                info
                for info in adapter.list_prefix(prefix, recursive=False)
                if info.uri.rstrip("/").rsplit("/", 1)[-1].endswith("_eventlist.jsonl")
            ]
            if not eventlist_files:
                return []

            with_modified = [info for info in eventlist_files if info.last_modified]
            if with_modified:
                latest_file = max(with_modified, key=lambda info: info.last_modified)
            else:
                latest_file = max(eventlist_files, key=lambda info: info.uri)

            with adapter.open_read(latest_file.uri) as file:
                text = file.read().decode("utf-8")
            return self._parse_eventlist_text(text)

        eventlist_files = list(self._paths.event_list_dir.glob("*_eventlist.jsonl"))
        if not eventlist_files:
            legacy_dir = self._module_dir / "event_list"
            if legacy_dir.exists():
                eventlist_files = list(legacy_dir.glob("*_eventlist.jsonl"))
        if not eventlist_files:
            return []

        latest_file = max(eventlist_files, key=lambda path: path.stat().st_mtime)
        return self._parse_eventlist_text(latest_file.read_text(encoding="utf-8"))

    def _parse_eventlist_text(self, text: str) -> List[str]:
        event_timestamps: List[str] = []
        event_positions: Dict[str, Tuple[float, float, float]] = {}

        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
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
        from .core import consolidate_events

        source_uri, source_name = self._normalize_input_uri(json_path)
        if not source_uri:
            return False

        vlm_data = self._load_json_from_uri(source_uri)
        if vlm_data is None:
            return False

        events = consolidate_events(vlm_data, base_date=self._resolve_base_date(vlm_data))

        output_jsonl_uri = self._resolve_output_uri(
            "intermediate_results",
            f"{source_name}_intermediate.jsonl",
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
            f"{source_name}_eventlist.jsonl",
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

    def _resolve_base_date(self, vlm_data) -> str:
        """이벤트 HH:MM:SS에 붙일 날짜 복원. 오버레이 시계엔 날짜가 없다.

        ① 결과 JSON의 video_source → 사이드카 앵커(capture_start)의 날짜
        ② 로드된 궤적 데이터의 시작 날짜 (보고 있는 데이터를 처리한다는 전제)
        ③ 레거시 고정값 — video_source 없는 옛 JSON + 데이터 미로드일 때만
        """
        src = (vlm_data or {}).get("video_source") or ""
        if src:
            try:
                from .event_index import sidecar_anchor
                anchor = sidecar_anchor(src)
                if anchor:
                    return anchor.date().isoformat()
            except Exception:
                pass
        start = getattr(self._repository, "data_start_time", None)
        if start:
            return start.date().isoformat()
        return "2025-01-01"

    def _normalize_input_uri(self, json_path: str) -> Tuple[Optional[str], Optional[str]]:
        candidate = (json_path or "").strip()
        if not candidate:
            return None, None

        parsed = urlparse(candidate)
        if parsed.scheme in ("s3", "minio", "file"):
            return candidate, Path(parsed.path or candidate).stem

        source_path = Path(candidate)
        if not source_path.exists():
            return None, None
        return source_path.resolve().as_uri(), source_path.stem

    def _load_json_from_uri(self, source_uri: str):
        adapter = from_uri(source_uri)
        if not adapter.exists(source_uri):
            return None
        with adapter.open_read(source_uri) as file:
            raw = file.read()
        if isinstance(raw, bytes):
            text = raw.decode("utf-8")
        else:
            text = str(raw)
        return json.loads(text)

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
                try:
                    import carb
                    carb.log_warn(
                        f"[Events] objid={first_objid} not in trajectory at {timestamp}, skipped"
                    )
                except Exception:
                    pass
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
