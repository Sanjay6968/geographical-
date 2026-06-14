# BhuMe Boundary Take-Home: Starter Kit

The official plot outlines in Maharashtra's land records sit metres off the real fields (an
artifact of how old paper maps were georeferenced onto satellite imagery). **Your job: for each
plot, return your best estimate of its true on-the-ground boundary, plus a confidence, and flag
the ones you can't place.**

Read the problem in full at the site's **Understand** and **The task** pages first. This kit just
removes the plumbing so you start at the interesting part.

## What this kit does (and doesn't)

It hands you the geospatial plumbing we are **not** assessing, so your hours go to the actual
problem. Each piece, and why it's here:

- **`load(village)`** — plots, imagery, boundary hints and example truths as one object, CRS sorted
  out. *Why: so you're not wiring up a GeoTIFF reader, a GeoJSON reader, and CRS handling before you
  can even look at a plot.*
- **`patch_for_plot(src, geom)`** — the RGB pixels under a plot. *Why: cropping a georeferenced
  raster to a polygon (the window + affine-transform math) is fiddly and isn't what we're testing.*
- **`lonlat_to_pixel` / `pixel_to_lonlat`** — convert between map coordinates and image pixels.
  *Why: the plots are lon/lat (EPSG:4326) but the imagery is web-mercator (EPSG:3857); mixing them
  up silently misaligns everything, and debugging that is a time sink, not a signal.*
- **`score(preds, village)`** — the exact accuracy + calibration + restraint metrics we grade on,
  run against the public example truths. *Why: a real feedback loop, you iterate against the same
  numbers we'll compute.*
- **`write_predictions(path, gdf)`** — emit a contract-valid `predictions.geojson`. *Why: so a
  schema slip never sinks an otherwise-good submission.*
- **`global_median_shift(village)`** — a deliberately naive baseline and a worked load→score loop.
  *Why: a floor to beat, and ~15 lines showing the whole cycle so you start at the interesting part.*

What it deliberately does **not** do: correct a plot for you. There's no align/snap/solve. The
method (how you find the true boundary, how you decide your confidence) is the whole point.

**Use any AI tools you like.** We expect it. We're assessing how you direct them, not whether you
typed every line. The plumbing above is exactly the kind of thing to let an LLM handle; the
judgment (which edge is right, what your confidence should mean, which records to trust) is not.

## Setup

This kit uses [uv](https://docs.astral.sh/uv/). Install it once
([instructions](https://docs.astral.sh/uv/getting-started/installation/)), then:

```bash
uv sync
```

That reads `pyproject.toml` / `uv.lock`, picks Python 3.12, and installs everything (geopandas,
rasterio, shapely, numpy, scipy, pillow) into a local `.venv`. The rasterio and geopandas wheels
bundle GDAL, so there's no system GDAL to install. Prefix commands with `uv run` (below) and you
never have to activate the venv yourself.

## Get the data

Download a village bundle from the site's **Get started** page and unzip it into `data/`:

```
data/
  34855_vadnerbhairav_chandavad_nashik/
    input.geojson         # the plots you transform (official, shifted)
    imagery.tif           # georeferenced satellite mosaic, your primary signal
    boundaries.tif        # rough, optional auto-detected field hints
    example_truths.geojson# a few hand-aligned truths, for self-scoring
```

## Run the worked example

```bash
uv run quickstart.py data/34855_vadnerbhairav_chandavad_nashik
```

You'll see the baseline's score, e.g.:

```
accuracy:    median IoU pred=0.71 vs official=0.61  (improvement=+0.11, improved 1.00)
calibration: Spearman(conf,IoU)=— · AUC=—   (flat confidence → no signal; this is the bar to clear)
```

Then make it better. A few directions (yours to choose, ignore, or replace):

- The error is mostly a coherent offset, but not entirely. What's left after a global shift?
- The imagery shows the real field edges. The boundary hints pre-detect some of them (roughly,
  and only where they're visible). How do you use the image where the hints are thin?
- Your confidence is scored. What makes a plot's correction trustworthy vs. a guess?
- Some plots can't be placed. Flagging them is a correct answer.

## Scoring notes

`score()` mirrors the objective (L1) half of grading: IoU vs the truth, improvement over the
official position, confidence calibration (does high confidence mean high accuracy?), and restraint
(don't move already-correct plots). It runs over the **public example truths only** — a handful — so
treat its output as a **rough directional check, not a grade**. Calibration in particular needs more
plots than this to mean much (and restraint shows nothing here: the public sample has no
already-correct control plots), so reason about what your confidence *should* represent rather than
maximizing the number on this sample. Your real grade uses a larger hidden set, so don't overfit to
these few. The contract spec is in `CONTRACT.md`.

---

## Solution: Local Cross-Correlation Geospatial Registration (LCCGR)

### Method overview

Rather than training a model, this solution treats boundary correction as a **geospatial
registration problem**:

1. **Global seed shift** — estimate a village-wide median offset (dx, dy in UTM metres) from the
   handful of example truths. Apply this shift to every plot as the baseline correction.
2. **Local NCC refinement (Track B)** — run FFT-based Normalised Cross-Correlation between the
   rasterised plot polygon and the boundary-hint raster. Only apply the local offset when the NCC
   peak is strong (≥ 0.08), sharp (sharpness ≥ 1.20), and not saturated at the search boundary.
3. **Confidence calibration** — score every plot on four signals (area consistency, boundary
   density, edge alignment, NCC stability), combine with a sigmoid stretch to produce a
   wide-variance confidence in [0.07, 0.97].
4. **Selective flagging** — flag only plots with extreme or moderate area mismatch where no NCC
   signal is available. Never flag a plot that can safely receive the global shift.

### Pipeline stages

| Stage | Detail |
|-------|--------|
| Global shift | Median (dx, dy) over example truths; applied to all plots |
| Patch extraction | ±30 m padding around seed position; imagery + boundary rasters |
| FFT NCC search | `scipy.signal.fftconvolve` — O(N² log N), ~25 ms/plot |
| NCC trust gate | peak ≥ 0.08 **and** sharpness ≥ 1.20 **and** not at search boundary |
| Geometry output | Global shift (Track A) or global + NCC offset (Track B) |
| Confidence | `sigmoid(8 × (0.45·area + 0.25·bden + 0.15·edge + 0.15·stability))` |
| Flagging | area_mismatch_extreme (<10 % score) or moderate mismatch + no NCC |

---

## Results

### vadnerbhairav

| Metric | Value |
|--------|-------|
| Total plots | 2,457 |
| Corrected | **2,306 (93.9 %)** |
| Flagged | 151 (6.1 %) |
| Global shift | dx = −4.40 m, dy = +11.35 m |
| Median IoU (pred vs truth) | **0.713** |
| Median IoU (official vs truth) | 0.612 |
| **Improvement** | **+0.112** |
| Accurate (IoU ≥ 0.5) | **100 %** (6/6 truths) |
| Centroid error | 8.8 m |
| Confidence range | 0.079 – 0.970 (std = 0.146) |
| Runtime | ~60 s · 24 ms/plot |

**Confidence distribution:**
```
[0.0-0.1)                                        3
[0.1-0.2)                                       18
[0.2-0.3)                                       19
[0.3-0.4)  █                                    44
[0.4-0.5)  ██                                   66
[0.5-0.6)  ████                                133
[0.6-0.7)  ████████                            245
[0.7-0.8)  ███████████████████                 563
[0.8-0.9)  ███████████████████████████████████ 1022
[0.9-1.0)  ██████                              193
```

#### Sample debug images — vadnerbhairav

Each image shows four panels: **Official (original) → Global Shift (seed) → Local NCC (refined) →
Confidence panel**.  Red = official boundary, orange = global-shifted, green = final prediction.

> Debug images are generated locally by running `solve.py --debug --debug-max 100`.
> They are excluded from the repo (`outputs/` is gitignored) to keep the repo lightweight.
> Sample panels (Official → Global Shift → Local NCC → Confidence):
>
> | Plot | Village | Status | Confidence |
> |------|---------|--------|------------|
> | 1022 | vadnerbhairav | CORRECTED | 0.805 |
> | 1007 | vadnerbhairav | CORRECTED | 0.871 |
> | 1013 | vadnerbhairav | CORRECTED | 0.878 |
> | 1050 | vadnerbhairav | CORRECTED | 0.845 |

---

### Malatavadi

| Metric | Value |
|--------|-------|
| Total plots | 2,508 |
| Corrected | **2,392 (95.4 %)** |
| Flagged | 116 (4.6 %) |
| Global shift | dx = +9.57 m, dy = +0.05 m |
| Median IoU (pred vs truth) | **0.588** |
| Median IoU (official vs truth) | 0.510 |
| **Improvement** | **+0.090** |
| Accurate (IoU ≥ 0.5) | **66.7 %** (2/3 truths) |
| Centroid error | 7.9 m |
| Confidence range | 0.067 – 0.927 (std = 0.159) |
| Runtime | ~56 s · 22 ms/plot |

**Confidence distribution:**
```
[0.0-0.1)                                        3
[0.1-0.2)  ██                                   56
[0.2-0.3)  ████                                 86
[0.3-0.4)  ████                                 86
[0.4-0.5)  █████████                           198
[0.5-0.6)  ████████████████████                424
[0.6-0.7)  ███████████████████████████████████ 712
[0.7-0.8)  ██████████████████████████████      614
[0.8-0.9)  █████████                           195
[0.9-1.0)                                       18
```

#### Sample debug images — Malatavadi

> | Plot | Village | Status | Confidence |
> |------|---------|--------|------------|
> | 1000 | Malatavadi | CORRECTED | 0.686 |
> | 100 | Malatavadi | CORRECTED | 0.755 |
> | 1001 | Malatavadi | CORRECTED | 0.555 |

---

## Output files

### Predictions — final GeoJSON submissions

| Village | File | Plots | Size |
|---------|------|------:|-----:|
| vadnerbhairav | [data/vadnerbhairav/predictions.geojson](https://github.com/Sanjay6968/geographical-/blob/main/data/vadnerbhairav/predictions.geojson) | 2,457 | 2.2 MB |
| Malatavadi | [data/Malatavadi/predictions.geojson](https://github.com/Sanjay6968/geographical-/blob/main/data/Malatavadi/predictions.geojson) | 2,508 | 1.5 MB |


Each `predictions.geojson` row has:

| Column | Type | Description |
|--------|------|-------------|
| `plot_number` | string | matches input `plot_number` exactly |
| `status` | string | `"corrected"` or `"flagged"` |
| `confidence` | float / null | calibrated confidence in (0, 1]; `null` for flagged plots |
| `method_note` | string | human-readable correction path (track, shift values, NCC stats) |
| `geometry` | Polygon | corrected boundary in EPSG:4326 |

### Debug images

| Village | Folder | Count |
|---------|--------|------:|
| vadnerbhairav | `outputs/debug/vadnerbhairav/` (local only) | 100 PNGs |
| Malatavadi | `outputs/debug/Malatavadi/` (local only) | 100 PNGs |


Each PNG is named `<plot_number>.png` and contains a 4-panel layout:
**Official → Global Shift → Local NCC → Confidence**

### Input data (unchanged)

| File | Description |
|------|-------------|
| `data/vadnerbhairav/input.geojson` | Official plot boundaries (input) |
| `data/vadnerbhairav/imagery.tif` | Satellite imagery GeoTIFF |
| `data/vadnerbhairav/boundaries.tif` | Auto-detected field boundary hints |
| `data/vadnerbhairav/example_truths.geojson` | 6 hand-aligned ground truth plots |
| `data/Malatavadi/input.geojson` | Official plot boundaries (input) |
| `data/Malatavadi/imagery.tif` | Satellite imagery GeoTIFF |
| `data/Malatavadi/boundaries.tif` | Auto-detected field boundary hints |
| `data/Malatavadi/example_truths.geojson` | 3 hand-aligned ground truth plots |
