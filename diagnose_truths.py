#!/usr/bin/env python3
"""
diagnose_truths.py
==================
Run the correction pipeline on ONLY the example truth plots and print
a per-plot breakdown comparing:

  Official IoU  |  Global-shift IoU  |  Global+NCC IoU  |  NCC effect

Usage:
    python diagnose_truths.py data/vadnerbhairav
    python diagnose_truths.py data/Malatavadi
"""

from __future__ import annotations

import sys
import statistics
from pathlib import Path

import numpy as np
import rasterio
from shapely.affinity import translate
from shapely.ops import transform as shp_transform
from pyproj import Transformer

from bhume import load
from bhume.geo import open_imagery
from bhume.corrector import (
    PlotCorrector,
    _utm_epsg, _transformer, _reproject,
    _read_patch_imagery, _rasterize_polygon, _resize_patch,
    PAD_M, MIN_MASK_PIXELS, NCC_APPLY_THRESHOLD, SHARPNESS_APPLY_THRESHOLD,
)
from bhume.signals import ncc_search, boundary_density_score


# ---------------------------------------------------------------------------
# IoU helper
# ---------------------------------------------------------------------------

def iou(a, b) -> float:
    if a is None or b is None or a.is_empty or b.is_empty:
        return 0.0
    union = a.union(b).area
    return float(a.intersection(b).area / union) if union > 0 else 0.0


# ---------------------------------------------------------------------------
# Compute global shift (same logic as solve.py)
# ---------------------------------------------------------------------------

def estimate_global_shift(village) -> tuple[float, float]:
    ref_lon = float(village.example_truths.geometry.iloc[0].centroid.x)
    utm     = _utm_epsg(village.example_truths.geometry.iloc[0])
    off_u   = village.plots.to_crs(utm)
    tru_u   = village.example_truths.to_crs(utm)
    dxs, dys = [], []
    for pn in village.example_truths.index:
        if pn in off_u.index:
            o = off_u.loc[pn, 'geometry'].centroid
            t = tru_u.loc[pn, 'geometry'].centroid
            dxs.append(t.x - o.x);  dys.append(t.y - o.y)
    return (statistics.median(dxs), statistics.median(dys)) if dxs else (0.0, 0.0)


# ---------------------------------------------------------------------------
# Per-plot NCC diagnostic (bypass the normal corrector to get both tracks)
# ---------------------------------------------------------------------------

def diagnose_plot(
    plot_number: str,
    official_geom,
    truth_geom,
    img_src,
    bnd_src,
    global_dx: float,
    global_dy: float,
) -> dict:
    """Return a dict with iou_official, iou_global, iou_ncc, ncc_applied, etc."""

    utm        = _utm_epsg(official_geom)
    tf_to_utm  = _transformer('EPSG:4326', utm)
    tf_to_4326 = _transformer(utm, 'EPSG:4326')
    tf_to_img  = _transformer('EPSG:4326', str(img_src.crs))
    tf_utm_img = _transformer(utm, str(img_src.crs))
    tf_img_utm = _transformer(str(img_src.crs), utm)

    # Project truth to UTM for IoU measurement
    truth_utm = _reproject(truth_geom, tf_to_utm)

    # Official geom in UTM
    geom_utm  = _reproject(official_geom, tf_to_utm)
    iou_off   = iou(geom_utm, truth_utm)

    # Apply global shift
    seed_utm  = translate(geom_utm, global_dx, global_dy)
    seed_4326 = _reproject(seed_utm, tf_to_4326)
    iou_glob  = iou(seed_utm, truth_utm)

    # Extract patch around seed
    img_result = _read_patch_imagery(img_src, seed_4326, pad_m=PAD_M, band_indices=[1,2,3])
    if img_result is None:
        return dict(plot_number=plot_number, iou_official=iou_off,
                    iou_global=iou_glob, iou_ncc=iou_glob,
                    ncc_applied=False, peak_ncc=0.0, sharpness=1.0,
                    local_dx=0.0, local_dy=0.0,
                    boundary_density=0.0, note='no_imagery_patch')

    img_data, img_win_tf = img_result
    H, W = img_data.shape[1], img_data.shape[2]

    seed_img_crs = _reproject(seed_4326, tf_to_img)
    pmask        = _rasterize_polygon(seed_img_crs, img_win_tf, H, W)

    # NCC on boundary raster
    peak_ncc = 0.0;  sharpness = 1.0
    local_dx = 0.0;  local_dy  = 0.0
    ncc_applied  = False
    boundary_density = 0.0
    note = ''

    if bnd_src is not None and pmask.sum() >= MIN_MASK_PIXELS:
        bnd_result = _read_patch_imagery(bnd_src, seed_4326, pad_m=PAD_M, band_indices=[1])
        if bnd_result is not None:
            bnd_patch = _resize_patch(bnd_result[0][0].astype(np.float32), H, W)
            boundary_density = boundary_density_score(bnd_patch)

            img_res_m = abs(img_win_tf.a)
            search_px = max(5, int(20.0 / img_res_m))
            ncc_res   = ncc_search(pmask, bnd_patch, search_range_px=search_px)

            peak_ncc  = ncc_res['peak_ncc']
            sharpness = ncc_res['sharpness']
            at_bound  = ncc_res['peak_at_boundary']

            ncc_strong   = peak_ncc  >= NCC_APPLY_THRESHOLD
            ncc_sharp    = sharpness >= SHARPNESS_APPLY_THRESHOLD
            ncc_in_range = not at_bound

            gate_details = (
                f"peak{'OK' if ncc_strong else 'LOW'}={peak_ncc:.3f} "
                f"sharp{'OK' if ncc_sharp else 'LOW'}={sharpness:.2f} "
                f"{'in_range' if ncc_in_range else 'AT_BOUNDARY'}"
            )
            note = gate_details

            if ncc_strong and ncc_sharp and ncc_in_range:
                dr_px  = ncc_res['offset_row'];  dc_px = ncc_res['offset_col']
                dx_img = img_win_tf.a * dc_px + img_win_tf.b * dr_px
                dy_img = img_win_tf.d * dc_px + img_win_tf.e * dr_px
                cx, cy = tf_utm_img.transform(seed_utm.centroid.x, seed_utm.centroid.y)
                x1, y1 = tf_img_utm.transform(cx, cy)
                x2, y2 = tf_img_utm.transform(cx + dx_img, cy + dy_img)
                local_dx = x2 - x1;  local_dy = y2 - y1
                ncc_applied = True
    else:
        note = f'mask_px={int(pmask.sum())} (< min={MIN_MASK_PIXELS})'

    # Apply NCC offset (even if not "trusted") to see its raw effect
    ncc_utm  = translate(geom_utm, global_dx + local_dx, global_dy + local_dy)
    iou_ncc  = iou(ncc_utm, truth_utm)

    return dict(
        plot_number=plot_number,
        iou_official=round(iou_off,  4),
        iou_global=  round(iou_glob, 4),
        iou_ncc=     round(iou_ncc,  4),
        ncc_applied=ncc_applied,
        peak_ncc=round(peak_ncc, 4),
        sharpness=round(sharpness, 3),
        local_dx=round(local_dx, 2),
        local_dy=round(local_dy, 2),
        boundary_density=round(boundary_density, 3),
        note=note,
    )


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main(village_dir: str) -> None:
    village   = load(village_dir)
    truths    = village.example_truths

    if truths is None or len(truths) == 0:
        print('No example truths found.')
        return

    global_dx, global_dy = estimate_global_shift(village)
    print(f'\nVillage : {village.slug}')
    print(f'Truths  : {len(truths)}')
    print(f'Global shift : dx={global_dx:+.2f} m  dy={global_dy:+.2f} m')
    print(f'NCC gate     : peak >= {NCC_APPLY_THRESHOLD}  sharpness >= {SHARPNESS_APPLY_THRESHOLD}')

    rows = []
    with open_imagery(village.imagery_path) as img_src:
        bnd_src = rasterio.open(village.boundaries_path) if village.boundaries_path else None
        try:
            for pn in truths.index:
                if pn not in village.plots.index:
                    print(f'  [SKIP] {pn} not in plots index')
                    continue
                official_geom = village.plots.loc[pn, 'geometry']
                truth_geom    = truths.loc[pn, 'geometry']
                row = diagnose_plot(
                    pn, official_geom, truth_geom,
                    img_src, bnd_src, global_dx, global_dy,
                )
                rows.append(row)
        finally:
            if bnd_src:
                bnd_src.close()

    # ── Print table ───────────────────────────────────────────────────
    COL = {
        'plot': 10, 'iou_off': 9, 'iou_glob': 10, 'iou_ncc': 9,
        'effect': 10, 'applied': 8, 'peak': 7, 'sharp': 7,
        'ldx': 8, 'ldy': 8, 'bden': 7,
    }

    THRESH_IMPROVE = 0.005   # must improve by at least this to count as "improved"

    print()
    header = (
        f"{'Plot':<{COL['plot']}} "
        f"{'IoU_off':>{COL['iou_off']}} "
        f"{'IoU_glob':>{COL['iou_glob']}} "
        f"{'IoU_ncc':>{COL['iou_ncc']}} "
        f"{'NCC_effect':<{COL['effect']}} "
        f"{'Trusted':<{COL['applied']}} "
        f"{'peak':>{COL['peak']}} "
        f"{'sharp':>{COL['sharp']}} "
        f"{'loc_dx':>{COL['ldx']}} "
        f"{'loc_dy':>{COL['ldy']}} "
        f"{'bden':>{COL['bden']}}"
    )
    sep = '-' * len(header)
    print(header)
    print(sep)

    n_improved = 0;  n_worsened = 0;  n_unchanged = 0

    for r in rows:
        delta    = r['iou_ncc'] - r['iou_global']
        glob_del = r['iou_global'] - r['iou_official']

        if delta > THRESH_IMPROVE:
            effect = 'IMPROVED'; n_improved += 1
        elif delta < -THRESH_IMPROVE:
            effect = 'WORSENED'; n_worsened += 1
        else:
            effect = 'unchanged'; n_unchanged += 1

        trusted = 'YES' if r['ncc_applied'] else 'no'

        print(
            f"{r['plot_number']:<{COL['plot']}} "
            f"{r['iou_official']:>{COL['iou_off']}.4f} "
            f"{r['iou_global']:>{COL['iou_glob']}.4f} "
            f"{r['iou_ncc']:>{COL['iou_ncc']}.4f} "
            f"{effect:<{COL['effect']}} "
            f"{trusted:<{COL['applied']}} "
            f"{r['peak_ncc']:>{COL['peak']}.4f} "
            f"{r['sharpness']:>{COL['sharp']}.3f} "
            f"{r['local_dx']:>{COL['ldx']}.2f} "
            f"{r['local_dy']:>{COL['ldy']}.2f} "
            f"{r['boundary_density']:>{COL['bden']}.3f}"
        )
        # Print gate note on next line if NCC was not applied
        if not r['ncc_applied'] and r['note']:
            print(f"  {'':>{COL['plot']}}  gate: {r['note']}")

    print(sep)

    # ── Medians ───────────────────────────────────────────────────────
    iou_offs  = [r['iou_official'] for r in rows]
    iou_globs = [r['iou_global']   for r in rows]
    iou_nccs  = [r['iou_ncc']      for r in rows]

    print(
        f"{'MEDIAN':<{COL['plot']}} "
        f"{statistics.median(iou_offs):>{COL['iou_off']}.4f} "
        f"{statistics.median(iou_globs):>{COL['iou_glob']}.4f} "
        f"{statistics.median(iou_nccs):>{COL['iou_ncc']}.4f}"
    )

    print()
    print('NCC effect on 6 example truths (vs global shift):')
    print(f'  Improved   : {n_improved}')
    print(f'  Worsened   : {n_worsened}')
    print(f'  Unchanged  : {n_unchanged}')
    print()

    glob_improve = statistics.median(iou_globs) - statistics.median(iou_offs)
    ncc_improve  = statistics.median(iou_nccs)  - statistics.median(iou_offs)
    print(f'  Global shift improvement over official : {glob_improve:+.4f}')
    print(f'  NCC improvement over official          : {ncc_improve:+.4f}')
    print(f'  NCC delta vs global                    : {ncc_improve - glob_improve:+.4f}')
    print()

    if n_worsened > n_improved:
        print('DIAGNOSIS: NCC is hurting more plots than it helps on example truths.')
        print('  -> NCC gate thresholds may need raising, or search range reducing.')
    elif n_improved > n_worsened:
        print('DIAGNOSIS: NCC is net-positive on example truths.')
    else:
        print('DIAGNOSIS: NCC is neutral on example truths.')


if __name__ == '__main__':
    vdir = sys.argv[1] if len(sys.argv) > 1 else 'data/vadnerbhairav'
    main(vdir)
