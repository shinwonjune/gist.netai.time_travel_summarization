"""
Event Post-Processing CLI
Processes VLM output JSON files (events/core.py 형식) into JSONL/summary files.

이 CLI는 원래 events/core.py에 있었다(2026-09 events/core.py 정리로 이동) —
events/core.py는 summary_service.py가 import하는 consolidate_events() 등
라이브러리 함수만 남기고, 여기서 그 함수를 가져다 쓴다.

Usage:
    python -m utils.inspect_events <events>.json [-o out.jsonl] [--summary] [--date 2025-01-01]
"""

import argparse
import json
from pathlib import Path
from typing import Any, Dict, List

from gist.netai.time_travel_summarization.events.core import consolidate_events, load_json


def save_jsonl(events: Dict[str, List[List[str]]], output_path: str):
    """
    Save consolidated events to JSONL format with core.py compatible format.
    Each line: {"2025-01-01 00:00:28.000": [["obj001", "obj004"]]}

    Args:
        events: Consolidated events dictionary
        output_path: Output file path
    """
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    with open(output_path, 'w', encoding='utf-8') as f:
        for timestamp in sorted(events.keys()):
            obj_groups = events[timestamp]
            line = {timestamp: obj_groups}
            f.write(json.dumps(line, ensure_ascii=False) + '\n')

    print(f"Saved consolidated events to: {output_path}")


def save_summary_json(events: Dict[str, List[List[str]]], output_path: str, original_data: Dict[str, Any]):
    """
    Save a summary JSON file with metadata and consolidated events.

    Args:
        events: Consolidated events dictionary
        output_path: Output file path
        original_data: Original JSON data for metadata
    """
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    summary = {
        "source_id": original_data.get("id", ""),
        "model": original_data.get("model", ""),
        "execution_time": original_data.get("execution_time", 0),
        "total_events": len(events),
        "total_timestamps": len(events),
        "events": events
    }

    with open(output_path, 'w', encoding='utf-8') as f:
        json.dump(summary, f, indent=2, ensure_ascii=False)

    print(f"Saved summary JSON to: {output_path}")


def print_statistics(events: Dict[str, List[List[str]]]):
    """Print statistics about the processed events."""
    total_timestamps = len(events)
    total_event_groups = sum(len(groups) for groups in events.values())

    print("\n=== Event Statistics ===")
    print(f"Total unique timestamps: {total_timestamps}")
    print(f"Total event groups: {total_event_groups}")

    if events:
        print(f"\nFirst event: {list(events.keys())[0]}")
        print(f"Last event: {list(events.keys())[-1]}")

        # Count object involvement
        all_objects = set()
        for groups in events.values():
            for group in groups:
                all_objects.update(group)
        print(f"Unique objects involved: {sorted(all_objects)}")


def main():
    parser = argparse.ArgumentParser(
        description="Process VLM event detection output JSON files"
    )
    parser.add_argument(
        "input_file",
        type=str,
        help="Input JSON file (e.g., <events>.json)"
    )
    parser.add_argument(
        "-o", "--output",
        type=str,
        default=None,
        help="Output JSONL file path (default: input_name_processed.jsonl)"
    )
    parser.add_argument(
        "--summary",
        action="store_true",
        help="Also save a summary JSON file"
    )
    parser.add_argument(
        "--date",
        type=str,
        default="2025-01-01",
        help="Base date for timestamp conversion (default: 2025-01-01)"
    )

    args = parser.parse_args()

    # Load input JSON
    input_path = Path(args.input_file)
    if not input_path.exists():
        print(f"Error: Input file not found: {input_path}")
        return

    print(f"Loading: {input_path}")
    data = load_json(str(input_path))

    # Process events
    print(f"Processing events with base date: {args.date}")
    events = consolidate_events(data, base_date=args.date)

    # Print statistics
    print_statistics(events)

    # Determine output path
    if args.output:
        output_path = args.output
    else:
        # Create intermediate_results directory at the same level as outputs
        output_dir = input_path.parent.parent / "intermediate_results"
        output_dir.mkdir(exist_ok=True)
        output_path = str(output_dir / f"{input_path.stem}_intermediate.jsonl")

    # Save JSONL
    save_jsonl(events, output_path)

    # Optionally save summary JSON
    if args.summary:
        summary_path = str(Path(output_path).with_suffix('.json'))
        save_summary_json(events, summary_path, data)

    print("\nOK Processing complete!")


if __name__ == "__main__":
    main()
