"""
VLM Object Detection 결과 비교 스크립트

클립 단위 평가(LoRA 데이터셋)의 STRICT/RELAXED 지표를 계산합니다.

사용법:
    python -m utils.compare_results --clips-gt test_gt.json --clips-pred preds.json --label base

    # 도움말 보기
    python compare_results.py -h

출력:
    - 콘솔: STRICT(HH:MM:SS별 객체ID 집합 완전일치) / RELAXED(클립당 충돌 유무 이진) 지표
    - 파일: compare_outputs/clips_{label}__comparison_result.json
"""

import json
import re
import argparse
from typing import Dict, Set, List, Tuple
from pathlib import Path


def calculate_metrics(ground_truth: Dict[str, Set[int]],
                     predictions: Dict[str, List[Set[int]]]) -> Tuple[float, float, float, Dict]:
    """
    Precision, Recall, F1 Score 계산
    완전 일치만 True Positive로 판정
    
    Args:
        ground_truth: 정답 데이터 {timestamp: set of object ids}
        predictions: 예측 데이터 {timestamp: list of sets of object ids}
    
    Returns:
        (precision, recall, f1, details)
    """
    true_positives = 0
    false_positives = 0
    false_negatives = 0
    
    details = {
        'correct': [],
        'missing_timestamps': [],
        'extra_timestamps': [],
        'incorrect_predictions': []
    }
    
    all_timestamps = set(ground_truth.keys()) | set(predictions.keys())
    
    for timestamp in sorted(all_timestamps):
        gt_objects = ground_truth.get(timestamp, set())
        pred_list = predictions.get(timestamp, [])
        
        if timestamp not in ground_truth:
            # 예측했지만 정답에 없는 타임스탬프 - 모두 FP
            for pred_objects in pred_list:
                details['extra_timestamps'].append({
                    'timestamp': timestamp,
                    'predicted': sorted(pred_objects)
                })
                false_positives += 1
                
        elif timestamp not in predictions or len(pred_list) == 0:
            # 정답에 있지만 예측하지 못한 타임스탬프 - FN
            details['missing_timestamps'].append({
                'timestamp': timestamp,
                'ground_truth': sorted(gt_objects)
            })
            false_negatives += 1
            
        else:
            # 둘 다 있는 경우 - 각 예측을 개별 판정
            for pred_objects in pred_list:
                if pred_objects == gt_objects:
                    # 완전 일치 - TP
                    true_positives += 1
                    details['correct'].append({
                        'timestamp': timestamp,
                        'objects': sorted(gt_objects)
                    })
                else:
                    # 불일치 - FP (틀린 예측)
                    false_positives += 1
                    details['incorrect_predictions'].append({
                        'timestamp': timestamp,
                        'ground_truth': sorted(gt_objects),
                        'predicted': sorted(pred_objects)
                    })
            
            # FN은 제거: 예측을 했으면 FN이 아님
    
    # Precision, Recall, F1 계산
    precision = true_positives / (true_positives + false_positives) if (true_positives + false_positives) > 0 else 0
    recall = true_positives / (true_positives + false_negatives) if (true_positives + false_negatives) > 0 else 0
    f1 = 2 * (precision * recall) / (precision + recall) if (precision + recall) > 0 else 0
    
    return precision, recall, f1, details


def parse_pred_entries(value) -> List[Set[int]]:
    """Parse one clip's model output into a list of {timestamp: set(ids)} entries.

    Accepts either an already-parsed list (from test_gt.json / parsed predictions)
    or a raw model string (optionally wrapped in a ```json code block).
    Returns a list of (timestamp, set) handled by the caller; here we return the
    raw list of dicts normalized to {ts: set}.
    """
    items = value
    if isinstance(value, str):
        text = value.strip()
        m = re.search(r'```json\s*(\[.*?\])\s*```', text, re.DOTALL)
        if m:
            text = m.group(1)
        if not (text.startswith('[') and text.endswith(']')):
            return []
        try:
            items = json.loads(text)
        except json.JSONDecodeError:
            return []
    out = []
    for item in items or []:
        if isinstance(item, dict):
            out.append({ts: set(ids) for ts, ids in item.items()})
    return out


def _flatten_clip(entries: List[Dict[str, Set[int]]]) -> Dict[str, Set[int]]:
    """Merge a clip's entries into {timestamp: set(ids)} (union on duplicate ts)."""
    merged: Dict[str, Set[int]] = {}
    for entry in entries:
        for ts, ids in entry.items():
            merged.setdefault(ts, set()).update(ids)
    return merged


def evaluate_clip_level(gt_path: str, pred_path: str) -> Dict:
    """Clip-level evaluation: STRICT (exact id-set per HH:MM:SS) + RELAXED (binary).

    gt_path:   test_gt.json  -> {clip: [{"HH:MM:SS": [ids]}, ...]}
    pred_path: predictions   -> {clip: <model output: list or raw string>}

    STRICT reuses calculate_metrics by namespacing timestamps with the clip id
    ("clip|HH:MM:SS") so the exact same TP/FP/FN logic runs across all clips.
    RELAXED scores each clip as collision-present (1) vs none (0).
    """
    with open(gt_path, encoding='utf-8') as f:
        gt_raw = json.load(f)
    with open(pred_path, encoding='utf-8') as f:
        pred_raw = json.load(f)

    gt_by_clip = {c: _flatten_clip(parse_pred_entries(v)) for c, v in gt_raw.items()}
    pred_by_clip = {c: _flatten_clip(parse_pred_entries(v)) for c, v in pred_raw.items()}

    # --- STRICT: namespaced timestamps, reuse calculate_metrics ---------------
    gt_ns: Dict[str, Set[int]] = {}
    pred_ns: Dict[str, List[Set[int]]] = {}
    for clip in gt_by_clip:  # GT defines the clip universe
        for ts, ids in gt_by_clip[clip].items():
            gt_ns[f"{clip}|{ts}"] = ids
        for ts, ids in pred_by_clip.get(clip, {}).items():
            pred_ns.setdefault(f"{clip}|{ts}", []).append(ids)
    precision, recall, f1, details = calculate_metrics(gt_ns, pred_ns)

    # --- RELAXED: per-clip binary collision presence --------------------------
    tp = fp = fn = tn = 0
    for clip in gt_by_clip:
        gt_pos = bool(gt_by_clip[clip])
        pred_pos = bool(pred_by_clip.get(clip))
        if gt_pos and pred_pos:
            tp += 1
        elif pred_pos and not gt_pos:
            fp += 1
        elif gt_pos and not pred_pos:
            fn += 1
        else:
            tn += 1
    r_prec = tp / (tp + fp) if (tp + fp) else 0.0
    r_rec = tp / (tp + fn) if (tp + fn) else 0.0
    r_f1 = 2 * r_prec * r_rec / (r_prec + r_rec) if (r_prec + r_rec) else 0.0
    r_acc = (tp + tn) / (tp + fp + fn + tn) if (tp + fp + fn + tn) else 0.0

    return {
        'num_clips': len(gt_by_clip),
        'metrics': {  # 'metrics' key kept for calculate_average_metrics.py compatibility
            'precision': round(precision, 4),
            'recall': round(recall, 4),
            'f1_score': round(f1, 4),
        },
        'relaxed_metrics': {
            'precision': round(r_prec, 4),
            'recall': round(r_rec, 4),
            'f1_score': round(r_f1, 4),
            'accuracy': round(r_acc, 4),
            'tp': tp, 'fp': fp, 'fn': fn, 'tn': tn,
        },
        'details': details,
    }


def main():
    parser = argparse.ArgumentParser(
        description="클립 단위 평가(LoRA 데이터셋)의 STRICT/RELAXED 지표를 계산합니다."
    )
    parser.add_argument('--clips-gt', type=str, required=True,
                        help='test_gt.json 경로 (clip -> [{HH:MM:SS:[ids]}]).')
    parser.add_argument('--clips-pred', type=str, required=True,
                        help='예측 json 경로 (clip -> model output).')
    parser.add_argument('--label', type=str, default='model',
                        help='클립 평가 결과 파일 라벨 (예: base, lora).')

    args = parser.parse_args()

    result = evaluate_clip_level(args.clips_gt, args.clips_pred)
    m, r = result['metrics'], result['relaxed_metrics']
    print("=" * 80)
    print(f"클립 단위 평가 [{args.label}]  (clips={result['num_clips']})")
    print("=" * 80)
    print("STRICT  (HH:MM:SS별 객체ID 집합 완전일치):")
    print(f"  P {m['precision']:.4f}  R {m['recall']:.4f}  F1 {m['f1_score']:.4f}")
    print("RELAXED (클립당 충돌 유무 이진):")
    print(f"  P {r['precision']:.4f}  R {r['recall']:.4f}  F1 {r['f1_score']:.4f}  "
          f"Acc {r['accuracy']:.4f}  (tp{r['tp']} fp{r['fp']} fn{r['fn']} tn{r['tn']})")
    print("=" * 80)
    out_dir = Path(__file__).parent.parent / "compare_outputs"
    out_dir.mkdir(exist_ok=True)
    out_file = out_dir / f"clips_{args.label}__comparison_result.json"
    result['source_file'] = f"{args.label} ({Path(args.clips_pred).name})"
    out_file.write_text(json.dumps(result, indent=2, ensure_ascii=False), encoding='utf-8')
    print(f"결과 저장: {out_file}")


if __name__ == "__main__":
    main()
