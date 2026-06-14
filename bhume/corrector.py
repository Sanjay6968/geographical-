"""
bhume/corrector.py
==================
Per-plot boundary correction via local cross-correlation registration.

Two-track pipeline design
--------------------------
Every valid plot always gets AT MINIMUM the global seed shift (Track A).
The local NCC refinement (Track B) is only trusted and applied when:
  - NCC peak strength  >= NCC_APPLY_THRESHOLD  (real match, not noise)
  - NCC peak sharpness >= SHARPNESS_APPLY_THRESHOLD (unique peak, unambiguous)
  - Peak did NOT hit the search-window boundary (not saturated)

This prevents the NCC from snapping to the wrong boundary feature (neighbouring
plots, roads, tree edges) and dragging the correction AWAY from the truth.

Confidence is deliberately mapped to different ranges for the two tracks:
  Track A (global only):  [0.30, 0.55]  -- moderate, honest uncertainty
  Track B (global+NCC):   [0.50, 0.90]  -- higher, earned by a clear NCC peak

The range separation is what gives the AUC calibration signal room to work.
A narrow flat-ish confidence range (e.g., 0.25-0.60) produces AUC ~= 0.5.

Flagging is reserved for:
  - Extreme area mismatch (ratio < 0.10) -- moving won't help
  - Moderate area mismatch AND no NCC signal (ratio < 0.25, ncc_applied=False)
  - Imagery patch completely out of bounds
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

import numpy as np
import rasterio
from pyproj import Transformer
from rasterio.features import rasterize
from rasterio.windows import from_bounds as window_from_bounds
from shapely.affinity import translate
from shapely.geometry import mapping
from shapely.geometry.base import BaseGeometry
from shapely.ops import transform as shp_transform

from bhume.signals import (
    area_consistency_score,
    boundary_density_score,
    calibrated_confidence,
    edge_alignment_score,
    ncc_peak_score,
    ncc_search,
    ncc_sharpness_score,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Tuning constants
# ---------------------------------------------------------------------------

SEARCH_RANGE_M: float = 20.0       # NCC search radius (metres)
FLAG_THRESHOLD: float = 0.25       # fallback: confidence below this -> flagged
MIN_MASK_PIXELS: int  = 15         # min foreground pixels for a valid NCC mask
RASTER_RES_M: float  = 1.5        # internal rasterisation resolution (m/px)
PAD_M: float         = 30.0       # patch padding around plot (metres)
MAX_SHIFT_M: float   = 40.0       # hard cap on any single-plot shift
EDGE_BAND_PX: int    = 3          # Sobel edge-alignment band half-width (px)

# NCC trust gate — both conditions must hold to apply the local offset
NCC_APPLY_THRESHOLD:       float = 0.08   # minimum peak_ncc to trust
SHARPNESS_APPLY_THRESHOLD: float = 1.20   # minimum sharpness (peak/mean-top-5%)


# ---------------------------------------------------------------------------
# CRS helpers
# ---------------------------------------------------------------------------

def _utm_epsg(geom_4326: BaseGeometry) -> str:
    lon  = geom_4326.centroid.x
    zone = int((lon + 180) // 6) + 1
    return f'EPSG:{32600 + zone}'


def _transformer(from_crs: str, to_crs: str) -> Transformer:
    return Transformer.from_crs(from_crs, to_crs, always_xy=True)


def _reproject(geom: BaseGeometry, tf: Transformer) -> BaseGeometry:
    return shp_transform(lambda xs, ys, _=None: tf.transform(xs, ys), geom)


# ---------------------------------------------------------------------------
# Raster patch helpers
# ---------------------------------------------------------------------------

def _read_patch_imagery(
    src: rasterio.DatasetReader,
    geom_4326: BaseGeometry,
    pad_m: float,
    band_indices: list[int] | None = None,
) -> tuple[np.ndarray, Any] | None:
    """Read a raster patch around a lon/lat geometry.

    Returns (array [C,H,W], window_transform) or None if out of imagery extent.
    """
    if band_indices is None:
        band_indices = list(range(1, src.count + 1))

    tf_fwd = _transformer('EPSG:4326', str(src.crs))

    # Collect all exterior coordinates regardless of geometry type
    def _coords(g: BaseGeometry) -> list[tuple]:
        if hasattr(g, 'exterior'):
            return list(g.exterior.coords)
        if hasattr(g, 'geoms'):
            return [c for part in g.geoms for c in _coords(part)]
        return []

    coords = _coords(geom_4326)
    if not coords:
        return None

    lons, lats = zip(*coords)
    pxs, pys   = tf_fwd.transform(list(lons), list(lats))

    left   = min(pxs) - pad_m;  right  = max(pxs) + pad_m
    bottom = min(pys) - pad_m;  top    = max(pys) + pad_m

    dl, db, dr, dt = src.bounds
    left   = max(left,   dl);  right  = min(right,  dr)
    bottom = max(bottom, db);  top    = min(top,    dt)

    if right <= left or top <= bottom:
        return None

    window = window_from_bounds(left, bottom, right, top, transform=src.transform)
    try:
        data   = src.read(band_indices, window=window)
    except Exception as exc:
        logger.debug('patch read error: %s', exc)
        return None

    return data, src.window_transform(window)


def _rasterize_polygon(
    geom_img_crs: BaseGeometry,
    win_tf: Any,
    height: int,
    width: int,
) -> np.ndarray:
    """Rasterize a polygon (in imagery CRS) to a binary (0/1) float32 mask."""
    if geom_img_crs is None or geom_img_crs.is_empty:
        return np.zeros((height, width), dtype=np.float32)
    try:
        mask = rasterize(
            [(mapping(geom_img_crs), 1)],
            out_shape=(height, width),
            transform=win_tf,
            fill=0,
            dtype=np.float32,
        )
    except Exception as exc:
        logger.debug('rasterize error: %s', exc)
        mask = np.zeros((height, width), dtype=np.float32)
    return mask


def _resize_patch(patch: np.ndarray, h: int, w: int) -> np.ndarray:
    """Resize a 2-D raster patch to (h, w) using bilinear interpolation."""
    if patch.shape == (h, w):
        return patch
    from PIL import Image as PILImage
    pmax = patch.max()
    arr8 = np.clip(patch / (pmax + 1e-6) * 255, 0, 255).astype(np.uint8)
    resized = PILImage.fromarray(arr8).resize((w, h), PILImage.BILINEAR)
    return np.array(resized, dtype=np.float32)


# ---------------------------------------------------------------------------
# Result dataclass
# ---------------------------------------------------------------------------

@dataclass
class CorrectionResult:
    """Everything the corrector computed for one plot."""
    plot_number:   str
    status:        str                  # 'corrected' | 'flagged'
    geometry_4326: BaseGeometry         # output geometry in EPSG:4326
    confidence:    float | None         # None for 'flagged'
    method_note:   str

    # Diagnostics (for debug images; not written to predictions.geojson)
    global_dx_m:      float
    global_dy_m:      float
    local_dx_m:       float
    local_dy_m:       float
    peak_ncc:         float
    sharpness:        float
    area_score:       float
    boundary_density: float
    edge_align:       float
    ncc_applied:      bool


# ---------------------------------------------------------------------------
# Main corrector class
# ---------------------------------------------------------------------------

class PlotCorrector:
    """Two-track per-plot boundary corrector using NCC geospatial registration.

    Parameters
    ----------
    imagery_src   : open rasterio dataset for the satellite image
    boundary_src  : open rasterio dataset for boundary hints (or None)
    global_dx_m   : village-wide seed shift X (UTM metres)
    global_dy_m   : village-wide seed shift Y (UTM metres)
    search_range_m: NCC search radius (metres, default 20)
    """

    def __init__(
        self,
        imagery_src:    rasterio.DatasetReader,
        boundary_src:   rasterio.DatasetReader | None,
        global_dx_m:    float,
        global_dy_m:    float,
        search_range_m: float = SEARCH_RANGE_M,
    ) -> None:
        self.imagery_src    = imagery_src
        self.boundary_src   = boundary_src
        self.global_dx_m    = global_dx_m
        self.global_dy_m    = global_dy_m
        self.search_range_m = search_range_m
        self._img_crs       = str(imagery_src.crs)

    # ------------------------------------------------------------------
    def correct(
        self,
        plot_number:       str,
        geom_4326:         BaseGeometry,
        map_area_sqm:      float | None,
        recorded_area_sqm: float | None,
        pot_kharaba_ha:    float | None,
    ) -> CorrectionResult:
        """Run full correction pipeline. Always returns a CorrectionResult."""
        try:
            return self._correct_inner(
                plot_number, geom_4326,
                map_area_sqm, recorded_area_sqm, pot_kharaba_ha,
            )
        except Exception as exc:
            logger.warning('Plot %s: correction failed (%s)', plot_number, exc)
            return self._make_flag(
                plot_number, geom_4326, f'error: {exc}',
                local_dx_m=0.0, local_dy_m=0.0,
                peak_ncc=0.0, sharpness=1.0,
                area_score=0.0, boundary_density=0.0, edge_align=0.0,
                ncc_applied=False,
            )

    # ------------------------------------------------------------------
    def _correct_inner(
        self,
        plot_number:       str,
        geom_4326:         BaseGeometry,
        map_area_sqm:      float | None,
        recorded_area_sqm: float | None,
        pot_kharaba_ha:    float | None,
    ) -> CorrectionResult:

        utm             = _utm_epsg(geom_4326)
        tf_to_utm       = _transformer('EPSG:4326', utm)
        tf_to_4326      = _transformer(utm, 'EPSG:4326')
        tf_to_img       = _transformer('EPSG:4326', self._img_crs)
        tf_utm_to_img   = _transformer(utm, self._img_crs)
        tf_img_to_utm   = _transformer(self._img_crs, utm)

        # ── Stage 1: Area pre-filter ───────────────────────────────────
        # Extreme area mismatch => placement correction won't help => flag.
        area_score = area_consistency_score(map_area_sqm, recorded_area_sqm, pot_kharaba_ha)
        if area_score < 0.10:
            return self._make_flag(
                plot_number, geom_4326, f'area_mismatch_extreme(score={area_score:.2f})',
                local_dx_m=0.0, local_dy_m=0.0, peak_ncc=0.0, sharpness=1.0,
                area_score=area_score, boundary_density=0.0, edge_align=0.0,
                ncc_applied=False,
            )

        # ── Stage 2: Apply global seed shift ──────────────────────────
        geom_utm  = _reproject(geom_4326, tf_to_utm)
        seed_utm  = translate(geom_utm, self.global_dx_m, self.global_dy_m)
        seed_4326 = _reproject(seed_utm, tf_to_4326)

        # ── Stage 3: Extract imagery patch around seed position ────────
        img_result = _read_patch_imagery(
            self.imagery_src, seed_4326, pad_m=PAD_M, band_indices=[1, 2, 3],
        )
        if img_result is None:
            return self._make_flag(
                plot_number, geom_4326, 'seed_outside_imagery_extent',
                local_dx_m=0.0, local_dy_m=0.0, peak_ncc=0.0, sharpness=1.0,
                area_score=area_score, boundary_density=0.0, edge_align=0.0,
                ncc_applied=False,
            )

        img_data, img_win_tf = img_result   # (3, H, W) uint8
        H, W = img_data.shape[1], img_data.shape[2]

        # Rasterise seed polygon into patch frame
        seed_img_crs = _reproject(seed_4326, tf_to_img)
        polygon_mask = _rasterize_polygon(seed_img_crs, img_win_tf, H, W)

        # ── Stage 4: NCC on boundary raster ───────────────────────────
        ncc_result: dict = {
            'offset_row': 0, 'offset_col': 0,
            'peak_ncc': 0.0, 'sharpness': 1.0,
            'peak_at_boundary': False,
        }
        boundary_density: float = 0.0
        local_dx_m:       float = 0.0
        local_dy_m:       float = 0.0
        ncc_applied:      bool  = False

        if self.boundary_src is not None and polygon_mask.sum() >= MIN_MASK_PIXELS:
            bnd_result = _read_patch_imagery(
                self.boundary_src, seed_4326, pad_m=PAD_M, band_indices=[1],
            )
            if bnd_result is not None:
                bnd_data, _ = bnd_result
                bnd_patch   = _resize_patch(bnd_data[0].astype(np.float32), H, W)

                boundary_density = boundary_density_score(bnd_patch)

                img_res_m = abs(img_win_tf.a)                        # m/px in imagery CRS
                search_px = max(5, int(self.search_range_m / img_res_m))

                ncc_result = ncc_search(polygon_mask, bnd_patch, search_range_px=search_px)

                # Trust gate: only apply local offset if peak is strong & sharp & in-range
                ncc_strong   = ncc_result['peak_ncc']  >= NCC_APPLY_THRESHOLD
                ncc_sharp    = ncc_result['sharpness']  >= SHARPNESS_APPLY_THRESHOLD
                ncc_in_range = not ncc_result['peak_at_boundary']

                if ncc_strong and ncc_sharp and ncc_in_range:
                    dr_px  = ncc_result['offset_row']
                    dc_px  = ncc_result['offset_col']
                    # Pixel offset -> imagery CRS delta
                    dx_img = img_win_tf.a * dc_px + img_win_tf.b * dr_px
                    dy_img = img_win_tf.d * dc_px + img_win_tf.e * dr_px

                    # Convert imagery-CRS delta to UTM metres at this location
                    cx_img, cy_img = tf_utm_to_img.transform(
                        seed_utm.centroid.x, seed_utm.centroid.y
                    )
                    x1, y1 = tf_img_to_utm.transform(cx_img,           cy_img)
                    x2, y2 = tf_img_to_utm.transform(cx_img + dx_img,  cy_img + dy_img)
                    local_dx_m = x2 - x1
                    local_dy_m = y2 - y1

                    # Hard cap: never shift more than MAX_SHIFT_M on top of global
                    shift_mag = np.hypot(local_dx_m, local_dy_m)
                    if shift_mag > MAX_SHIFT_M:
                        local_dx_m *= MAX_SHIFT_M / shift_mag
                        local_dy_m *= MAX_SHIFT_M / shift_mag

                    ncc_applied = True

        # ── Stage 5: Build final corrected geometry ────────────────────
        total_dx     = self.global_dx_m + local_dx_m
        total_dy     = self.global_dy_m + local_dy_m
        refined_utm  = translate(geom_utm, total_dx, total_dy)
        refined_4326 = _reproject(refined_utm, tf_to_4326)

        # ── Stage 6: Edge alignment on refined position ───────────────
        refined_img_crs = _reproject(refined_4326, tf_to_img)
        refined_mask    = _rasterize_polygon(refined_img_crs, img_win_tf, H, W)
        gray            = img_data.mean(axis=0).astype(np.float32)
        edge_align      = edge_alignment_score(refined_mask, gray, EDGE_BAND_PX)

        # ── Stage 7: Calibrated confidence (sigmoid-stretched) ────────
        # Geometry is FROZEN. Only confidence scoring changes here.
        # Uses calibrated_confidence() from signals.py which applies:
        #   base = 0.45*area + 0.25*bden + 0.15*edge + 0.15*stability
        #   + 0.10 NCC bonus (Track B only)
        #   then sigmoid(8*(base-0.5)) for wide variance 0.20->0.90
        confidence = calibrated_confidence(
            area_score=area_score,
            boundary_density=boundary_density,
            edge_align=edge_align,
            peak_ncc=ncc_result['peak_ncc'],
            sharpness=ncc_result['sharpness'],
            ncc_applied=ncc_applied,
        )

        # ── Stage 8: Secondary flagging ───────────────────────────────
        # Flag moderate area mismatches where we also have no NCC signal.
        if area_score < 0.25 and not ncc_applied:
            return self._make_flag(
                plot_number, geom_4326,
                f'area_mismatch(score={area_score:.2f})+no_NCC_signal',
                local_dx_m=local_dx_m, local_dy_m=local_dy_m,
                peak_ncc=ncc_result['peak_ncc'], sharpness=ncc_result['sharpness'],
                area_score=area_score, boundary_density=boundary_density,
                edge_align=edge_align, ncc_applied=False,
            )

        # ── Build method note ─────────────────────────────────────────
        track = 'global+NCC' if ncc_applied else 'global_only'
        parts = [f'track={track}',
                 f'global({self.global_dx_m:+.1f},{self.global_dy_m:+.1f})m']
        if ncc_applied:
            parts += [f'local({local_dx_m:+.1f},{local_dy_m:+.1f})m',
                      f'ncc={ncc_result["peak_ncc"]:.3f}',
                      f'sharp={ncc_result["sharpness"]:.2f}']
        parts.append(f'conf={confidence:.3f}')

        return CorrectionResult(
            plot_number=plot_number,
            status='corrected',
            geometry_4326=refined_4326,
            confidence=confidence,
            method_note=' | '.join(parts),
            global_dx_m=self.global_dx_m,
            global_dy_m=self.global_dy_m,
            local_dx_m=local_dx_m,
            local_dy_m=local_dy_m,
            peak_ncc=ncc_result['peak_ncc'],
            sharpness=ncc_result['sharpness'],
            area_score=area_score,
            boundary_density=boundary_density,
            edge_align=edge_align,
            ncc_applied=ncc_applied,
        )

    # ------------------------------------------------------------------
    def _make_flag(
        self,
        plot_number:      str,
        geom_4326:        BaseGeometry,
        reason:           str,
        local_dx_m:       float,
        local_dy_m:       float,
        peak_ncc:         float,
        sharpness:        float,
        area_score:       float,
        boundary_density: float,
        edge_align:       float,
        ncc_applied:      bool,
    ) -> CorrectionResult:
        return CorrectionResult(
            plot_number=plot_number,
            status='flagged',
            geometry_4326=geom_4326,
            confidence=None,
            method_note=reason,
            global_dx_m=self.global_dx_m,
            global_dy_m=self.global_dy_m,
            local_dx_m=local_dx_m,
            local_dy_m=local_dy_m,
            peak_ncc=peak_ncc,
            sharpness=sharpness,
            area_score=area_score,
            boundary_density=boundary_density,
            edge_align=edge_align,
            ncc_applied=ncc_applied,
        )
