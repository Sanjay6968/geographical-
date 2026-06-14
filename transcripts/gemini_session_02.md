# Gemini Session 02 — Confidence Calibration Redesign

**Tool:** Gemini (Antigravity)
**Date:** June 2026
**Focus:** Widening confidence distribution, sigmoid stretch, final submission

## Problem

Old confidence distribution was clustered (median=0.477, std=0.031, range=0.343).
85% of corrected plots fell in the `[0.40–0.50)` bucket — no useful rank separation.

## Solution

Redesigned `calibrated_confidence()` in `bhume/signals.py`:

```
stab = 0.5 * (peak_ncc ^ 0.6) + 0.5 * tanh((sharpness - 1) / 1.5)

base = 0.45 * area_score
     + 0.25 * boundary_density
     + 0.15 * edge_align
     + 0.15 * stab

if ncc_applied: base += 0.10

confidence = sigmoid(8 * (base - 0.5))
```

## Topics Covered

- Root cause analysis: `area_score` defaults caused 45% of base to be constant
- Added `stability_score()` — per-plot NCC signal quality, computed for ALL plots
- Chose sigmoid slope=8 (not 4) to cover full 0.20→0.90 target range
- Verified geometry UNCHANGED after confidence redesign

## Results (vadnerbhairav)

| Metric | Old | New |
|--------|-----|-----|
| min | 0.347 | 0.079 |
| median | 0.477 | 0.808 |
| max | 0.689 | 0.970 |
| std | 0.031 | 0.146 |
| range | 0.343 | 0.890 |

## Final Output

- Generated `predictions.geojson` for both villages
- Generated 100 debug PNGs per village (4-panel layout)
- Pushed to GitHub: https://github.com/Sanjay6968/geographical-
