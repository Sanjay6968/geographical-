# Gemini Session 01 — Pipeline Design & NCC Implementation

**Tool:** Gemini (Antigravity)
**Date:** June 2026
**Focus:** Initial architecture, FFT-based NCC, diagnostic tools

## Topics Covered

- Inspected starter-kit APIs (`load`, `patch_for_plot`, `score`, `write_predictions`)
- Designed two-track correction pipeline:
  - Track A: Global median shift (village-wide)
  - Track B: Global shift + local NCC refinement
- Implemented `ncc_search` using `scipy.signal.fftconvolve` (~15× speedup over `correlate2d`)
- Built `diagnose_truths.py` to evaluate NCC gate on the 6 example truths
- Built `report_metrics.py` for confidence distribution stats and AUC
- Validated geometry: median IoU improved from 0.612 → 0.713 (+0.112) on vadnerbhairav

## Key Decisions

- NCC trust gate: `peak ≥ 0.08 AND sharpness ≥ 1.20 AND not at search boundary`
- Flagging: `area_mismatch_extreme` (<10% area score) or moderate mismatch with no NCC signal
- NCC did not improve any of the 6 example truths → geometry frozen after global shift

## Outcome

- Geometry pipeline frozen
- 100% accuracy on public truths (IoU ≥ 0.5)
- Runtime ~70s per village
- Flag rate 6.1%
