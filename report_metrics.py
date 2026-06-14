#!/usr/bin/env python3
"""
report_metrics.py
=================
Post-run analysis: reads predictions.geojson and prints a full metrics report.

Usage:
    python report_metrics.py data/vadnerbhairav
    python report_metrics.py data/Malatavadi
"""
from __future__ import annotations

import json
import sys
import statistics
from pathlib import Path
from collections import Counter

import geopandas as gpd
import numpy as np
from scipy.stats import spearmanr

from bhume import load, score


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def iou(a, b) -> float:
    if a is None or b is None or a.is_empty or b.is_empty:
        return 0.0
    u = a.union(b).area
    return float(a.intersection(b).area / u) if u > 0 else 0.0


def auc_roc(scores, labels) -> float | None:
    pos = [s for s, y in zip(scores, labels) if y]
    neg = [s for s, y in zip(scores, labels) if not y]
    if not pos or not neg:
        return None
    wins = sum((p > n) + 0.5 * (p == n) for p in pos for n in neg)
    return round(wins / (len(pos) * len(neg)), 4)


def confidence_histogram(confs: list[float], bins: int = 10) -> str:
    """ASCII bar chart of confidence distribution."""
    if not confs:
        return '  (no corrected plots)'
    edges = [i / bins for i in range(bins + 1)]
    counts = [0] * bins
    for c in confs:
        idx = min(int(c * bins), bins - 1)
        counts[idx] += 1
    max_count = max(counts) or 1
    bar_width  = 30
    lines = []
    for i, cnt in enumerate(counts):
        lo = edges[i];  hi = edges[i + 1]
        bar = '█' * int(cnt / max_count * bar_width)
        lines.append(f'  [{lo:.1f}-{hi:.1f})  {bar:<{bar_width}}  {cnt:>5}')
    return '\n'.join(lines)


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------

def main(village_dir: str) -> None:
    vpath = Path(village_dir)
    pred_path = vpath / 'predictions.geojson'
    if not pred_path.exists():
        print(f'ERROR: {pred_path} not found — run solve.py first.')
        sys.exit(1)

    village = load(vpath)
    preds   = gpd.read_file(pred_path)
    preds['plot_number'] = preds['plot_number'].astype(str)
    preds = preds.set_index('plot_number', drop=False)

    n_total     = len(preds)
    corrected   = preds[preds['status'] == 'corrected']
    flagged     = preds[preds['status'] == 'flagged']
    n_corrected = len(corrected)
    n_flagged   = len(flagged)

    confs = corrected['confidence'].dropna().tolist()
    confs = [float(c) for c in confs]

    # ── Method note breakdown ──────────────────────────────────────────
    # Extract track type from method_note
    n_global_only = sum(1 for n in corrected['method_note'].fillna('')
                        if 'global_only' in str(n))
    n_ncc_applied = sum(1 for n in corrected['method_note'].fillna('')
                        if 'global+NCC' in str(n))

    # ── Flagging reasons ──────────────────────────────────────────────
    flag_reasons = Counter()
    for note in flagged['method_note'].fillna(''):
        key = str(note).split('(')[0].strip()
        flag_reasons[key] += 1

    # ── Score against example truths ──────────────────────────────────
    print(f'\n{"="*65}')
    print(f'  METRICS REPORT — {village.slug}')
    print(f'{"="*65}')

    print(f'\n── Coverage ──────────────────────────────────────────────────')
    print(f'  Total predictions : {n_total:,}')
    print(f'  Corrected         : {n_corrected:,}  ({100*n_corrected/n_total:.1f}%)')
    print(f'    Track A (global only) : {n_global_only:,}')
    print(f'    Track B (global+NCC)  : {n_ncc_applied:,}')
    print(f'  Flagged           : {n_flagged:,}  ({100*n_flagged/n_total:.1f}%)')
    if flag_reasons:
        for reason, cnt in flag_reasons.most_common():
            print(f'    {reason:<45} : {cnt}')

    print(f'\n── Confidence distribution (corrected plots only) ────────────')
    if confs:
        p = lambda q: round(float(np.percentile(confs, q)), 4)
        print(f'  min    = {min(confs):.4f}')
        print(f'  p10    = {p(10):.4f}')
        print(f'  p25    = {p(25):.4f}')
        print(f'  median = {statistics.median(confs):.4f}')
        print(f'  p75    = {p(75):.4f}')
        print(f'  p90    = {p(90):.4f}')
        print(f'  max    = {max(confs):.4f}')
        print(f'  range  = {max(confs) - min(confs):.4f}  (wider = better AUC potential)')
        print(f'  std    = {statistics.stdev(confs):.4f}')
        print()
        # Full 10-bucket histogram 0.0-1.0
        buckets = [0] * 10
        for c in confs:
            buckets[min(9, int(c * 10))] += 1
        max_b = max(buckets) or 1
        W = 35
        for i, cnt in enumerate(buckets):
            bar   = '█' * int(cnt / max_b * W)
            label = f'[{i/10:.1f}-{(i+1)/10:.1f})'
            print(f'  {label}  {bar:<{W}}  {cnt:>5}')
    else:
        print('  (no corrected plots with confidence values)')

    et = village.example_truths
    n_et = 0 if et is None else len(et)
    print(f'\n── Scored vs example truths ({n_et} plots) ───────────────────────')
    if village.example_truths is not None and len(village.example_truths) > 0:
        sc = score(preds, village)
        print(f'  corrected in truths  : {sc.n_corrected}')
        print(f'  flagged in truths    : {sc.n_flagged}')
        print(f'  median IoU (pred)    : {sc.median_iou_pred}')
        print(f'  median IoU (official): {sc.median_iou_official}')
        print(f'  improvement          : {sc.median_improvement}')
        print(f'  accurate (IoU>=0.5)  : {sc.accurate_rate}')
        print(f'  centroid err (m)     : {sc.median_centroid_err_m}')
        print(f'  Spearman(conf, IoU)  : {sc.spearman_conf_vs_iou}')
        print(f'  AUC                  : {sc.auc_accurate_vs_conf}')
        if sc.violations:
            print(f'  Violations: {sc.violations}')
    else:
        print('  No example truths available.')

    print(f'\n{"="*65}\n')


if __name__ == '__main__':
    main(sys.argv[1] if len(sys.argv) > 1 else 'data/vadnerbhairav')
