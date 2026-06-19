"""Convert a `swift infer` result jsonl into the preds.json that compare_results expects.

`swift infer --val_dataset test.jsonl --result_path infer.jsonl` writes one entry per
sample with the model's reply. `utils.compare_results --clips-pred` wants:

    {clip_id: <model output: raw string or list>}

where clip_id == the clip filename used as the key in test_gt.json
(e.g. "ep0003_clip0007.mp4"). We recover clip_id from each sample's video path and
the reply text from whatever field the installed ms-swift version uses, falling back
to positional alignment with the input test.jsonl when the result omits the video.

Usage:
    python -m utils.swift_infer_to_preds \
        --test-jsonl artifacts/dataset/test.jsonl \
        --infer-result artifacts/eval/infer_lora.jsonl \
        --out artifacts/eval/preds_lora.json
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any, List, Optional


def _read_jsonl(path: Path) -> List[dict]:
    rows = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                rows.append(json.loads(line))
    return rows


def _clip_id_from_videos(entry: dict) -> Optional[str]:
    """Return the clip filename (e.g. 'ep0_clip0001.mp4') from a sample's video field."""
    for key in ("videos", "video", "images"):
        val = entry.get(key)
        if isinstance(val, list) and val:
            val = val[0]
        if isinstance(val, str) and val:
            return Path(val).name
    return None


def _extract_response(entry: dict) -> Optional[str]:
    """Pull the model's text reply across ms-swift version field-name differences."""
    if isinstance(entry.get("response"), str):
        return entry["response"]
    for key in ("generated", "prediction", "pred", "output"):
        if isinstance(entry.get(key), str):
            return entry[key]
    msgs = entry.get("messages") or entry.get("conversation")
    if isinstance(msgs, list):
        for m in reversed(msgs):
            if isinstance(m, dict) and m.get("role") in ("assistant", "gpt"):
                content = m.get("content")
                if isinstance(content, str):
                    return content
    return None


def main() -> None:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--test-jsonl", required=True, type=Path,
                    help="the test.jsonl fed to `swift infer` (for clip ids / order)")
    ap.add_argument("--infer-result", required=True, type=Path,
                    help="swift infer --result_path output (jsonl)")
    ap.add_argument("--out", required=True, type=Path, help="preds.json to write")
    args = ap.parse_args()

    test_rows = _read_jsonl(args.test_jsonl)
    result_rows = _read_jsonl(args.infer_result)
    test_ids = [_clip_id_from_videos(r) for r in test_rows]

    if len(result_rows) != len(test_rows):
        print(f"⚠️  result has {len(result_rows)} entries but test has {len(test_rows)}; "
              "falling back to per-entry video matching where possible.")

    preds: dict[str, Any] = {}
    missing = 0
    for i, entry in enumerate(result_rows):
        clip_id = _clip_id_from_videos(entry)
        if clip_id is None and i < len(test_ids):
            clip_id = test_ids[i]  # positional fallback (swift preserves order)
        if clip_id is None:
            missing += 1
            continue
        resp = _extract_response(entry)
        if resp is None:
            missing += 1
            continue
        preds[clip_id] = resp

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(preds, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Wrote {len(preds)} predictions -> {args.out}"
          + (f"  ({missing} entries skipped)" if missing else ""))


if __name__ == "__main__":
    main()
