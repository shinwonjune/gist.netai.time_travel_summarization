"""
Event Post-Processing Script
Processes VLM output JSON files to extract and consolidate event information.
Converts timestamps and object IDs to match core.py in-memory format.

CLI(입력 JSON -> JSONL 변환)는 utils/inspect_events.py로 이동했다. 이 모듈은
consolidate_events() 등 라이브러리 함수만 남긴다(summary_service.py가 import).
"""

import ast
import json
import re
from typing import List, Dict, Any
from collections import defaultdict


def load_json(file_path: str) -> Dict[str, Any]:
    """Load JSON file."""
    with open(file_path, 'r', encoding='utf-8') as f:
        return json.load(f)


# 정상 응답 형식: [{"HH:MM:SS": [obj_id, ...]}, ...]
# 예시: [{"00:00:03": [1, 3]}, {"00:00:06": [1, 2, 3, 4]}]
_JSON_LIST_PATTERN = re.compile(r'\[\s*\{.*?\}\s*(?:,\s*\{.*?\}\s*)*\]', re.DOTALL)


def _strip_markdown_fence(text: str) -> str:
    """```json\n[...]\n``` → [...]. fence가 없으면 원본 반환."""
    text = text.strip()
    if not text.startswith("```"):
        return text
    lines = text.split('\n')
    if len(lines) >= 3:
        return '\n'.join(lines[1:-1]).strip()
    return text


def _extract_list_with_regex(text: str) -> str:
    """자연어 prefix가 섞여 있어도 첫 번째 list-of-dicts 패턴을 추출."""
    match = _JSON_LIST_PATTERN.search(text)
    return match.group(0) if match else text


def parse_content(content_str: str) -> List[Dict[str, List[int]]]:
    """Parse VLM content string into list of {timestamp: [obj_ids]} dicts.

    정상 형식: '[{"HH:MM:SS": [1, 2]}, ...]' 또는 markdown wrap.
    Fallback 순서:
      1) markdown fence 제거
      2) json.loads
      3) ast.literal_eval (single-quote / trailing comma 등 관대)
      4) regex로 list 패턴 추출 후 재시도
      5) 모두 실패 → log + []
    """
    raw = (content_str or "").strip()
    if not raw or raw == "[]":
        return []

    candidate = _strip_markdown_fence(raw)

    for attempt_label, candidate_str in (
        ("strict-json", candidate),
        ("regex-extracted", _extract_list_with_regex(candidate)),
    ):
        try:
            parsed = json.loads(candidate_str)
        except json.JSONDecodeError:
            try:
                parsed = ast.literal_eval(candidate_str)
            except (ValueError, SyntaxError):
                continue
        if isinstance(parsed, list):
            return parsed
        if isinstance(parsed, dict):
            # 일부 모델이 {"events": [...]} 또는 단일 dict로 응답 — 정규화
            if "events" in parsed and isinstance(parsed["events"], list):
                return parsed["events"]
            return [parsed]

    _log_parse_failure(content_str)
    return []


def _log_parse_failure(content_str: str) -> None:
    """parse 실패 시 raw 응답 일부 로깅. carb 사용 가능 시 우선, 아니면 print."""
    snippet = (content_str or "")[:200].replace("\n", "\\n")
    msg = f"[Events] Failed to parse VLM response (first 200 chars): {snippet}"
    try:
        import carb
        carb.log_warn(msg)
    except Exception:
        print(f"Warning: {msg}")


def format_timestamp_for_core(time_str: str, base_date: str = "2025-01-01") -> str:
    """
    Convert video timestamp (HH:MM:SS) to core.py CSV format (YYYY-MM-DD HH:MM:SS.000).
    
    Args:
        time_str: Time in "HH:MM:SS" format (e.g., "00:00:28")
        base_date: Base date to prepend (default: "2025-01-01")
    
    Returns:
        Timestamp in core.py CSV format: "2025-01-01 00:00:28.000"
    """
    return f"{base_date} {time_str}.000"


def format_objid_for_core(obj_num: int) -> str:
    """
    Convert object number to core.py objid format.
    
    Args:
        obj_num: Object number (e.g., 1, 2, 3)
    
    Returns:
        Object ID in core.py format: "obj001", "obj002", etc.
    """
    return f"obj{obj_num:03d}"


def consolidate_events(data: Dict[str, Any], base_date: str = "2025-01-01") -> Dict[str, List[List[str]]]:
    """
    VLM 의 chunk_responses 에서 모든 이벤트를 통합하여 정돈된 json 포맷으로 변환합니다.
    Consolidate all events from chunk_responses and convert to organized JSON format.
    
    Args:
        data: The loaded JSON data
        base_date: Base date for timestamp conversion
    
    Returns:
        Dictionary mapping timestamp to list of object ID groups
        Example: {"2025-01-01 00:00:28.000": [["obj001", "obj004"]], "2025-01-01 00:00:30.000": [["obj002", "obj004"]]}
    """
    consolidated = defaultdict(list)
    
    chunk_responses = data.get("chunk_responses", [])
    
    for chunk in chunk_responses:
        content = chunk.get("content", "")
        events = parse_content(content)
        
        for event in events:
            # Each event is like {"00:00:28": [1, 4]}
            for timestamp, obj_list in event.items():
                if obj_list:  # Only add non-empty lists
                    # Convert timestamp to core.py format
                    formatted_timestamp = format_timestamp_for_core(timestamp, base_date)
                    # Convert object IDs to core.py format
                    formatted_objids = [format_objid_for_core(obj_num) for obj_num in obj_list]
                    consolidated[formatted_timestamp].append(formatted_objids)
    
    return dict(consolidated)
