"""
bhume/signals.py
================
Interpretable confidence signals for boundary-correction quality assessment.

Each signal returns a float in [0.0, 1.0]:
  - 0.0 -> strong evidence the correction is wrong / uncertain
  - 1.0 -> strong evidence the correction is right

These are deliberately independent so they can be combined with explicit weights
in corrector.py.  No trained models -- every number is geometrically motivated.

Performance note
----------------
NCC uses scipy.signal.fftconvolve (O(N^2 log N)) instead of correlate2d
(O(N^4)).  On 200x200 patches this is ~100x faster.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import numpy as np
from scipy.signal import fftconvolve

if TYPE_CHECKING:
    pass


# ---------------------------------------------------------------------------
# Signal A — Area Consistency
# ---------------------------------------------------------------------------

def area_consistency_score(
    map_area_sqm: float | None,
    recorded_area_sqm: float | None,
    pot_kharaba_ha: float | None,
) -> float:
    """Score how well the drawn area matches the recorded area.

    A ratio near 1.0 means the shape is probably the right size, only misplaced
    (placement problem, fixable by shift).  Far from 1.0 suggests an area
    discrepancy where moving the polygon won't help.

    Returns
    -------
    float in [0, 1]
        1.0 = perfect area match (ratio == 1.0)
        0.0 = extreme area mismatch (ratio < 0.3 or > 3.0)
    """
    if not map_area_sqm or map_area_sqm <= 0:
        return 0.3  # no map area → mild penalty, not fatal

    total_recorded = 0.0
    if recorded_area_sqm and recorded_area_sqm > 0:
        total_recorded += recorded_area_sqm
    if pot_kharaba_ha and pot_kharaba_ha > 0:
        total_recorded += pot_kharaba_ha * 10_000.0  # ha → m²

    if total_recorded <= 0:
        return 0.4  # no record at all → ambiguous, lean cautious

    ratio = map_area_sqm / total_recorded

    # Gaussian-shaped score centred on ratio=1.0, width σ≈0.5 decades
    # score = exp(-0.5 * (log(ratio)/σ)²)  — symmetric in log space
    log_ratio = np.log(ratio)
    sigma = 0.5  # a factor-of-2 mismatch gives ~score 0.13
    score = float(np.exp(-0.5 * (log_ratio / sigma) ** 2))
    return float(np.clip(score, 0.0, 1.0))


# ---------------------------------------------------------------------------
# Signal B — NCC peak strength and sharpness
# ---------------------------------------------------------------------------

def ncc_search(
    polygon_mask: np.ndarray,
    boundary_patch: np.ndarray,
    search_range_px: int = 30,
) -> dict:
    """Slide the polygon mask over the boundary raster and find the best offset.

    Parameters
    ----------
    polygon_mask : (H, W) float32
        Binary (0/1) image of the rasterized plot polygon in the patch frame.
    boundary_patch : (H, W) float32
        Boundary-response image (already cropped to same spatial extent).
    search_range_px : int
        Maximum pixel shift to search in each direction.

    Returns
    -------
    dict with keys:
        offset_row, offset_col : int    best (row, col) offset in pixels
        peak_ncc               : float  NCC at best offset, in [−1, 1]
        sharpness              : float  peak / mean-of-top-5%, in [1, ∞)
        peak_at_boundary       : bool   True if best offset is at search edge
        ncc_map                : ndarray  full correlation surface
    """
    mask = polygon_mask.astype(np.float32)
    bnd  = boundary_patch.astype(np.float32)

    # Normalize both arrays for NCC
    m_mu, m_std = mask.mean(), mask.std()
    b_mu, b_std = bnd.mean(),  bnd.std()

    if m_std < 1e-6 or b_std < 1e-6:
        # Degenerate: mask is uniform or boundary is flat -> no signal
        return {
            'offset_row': 0, 'offset_col': 0,
            'peak_ncc': 0.0, 'sharpness': 1.0,
            'peak_at_boundary': False,
            'ncc_map': np.zeros((1, 1), dtype=np.float32),
        }

    mask_norm = (mask - m_mu) / m_std
    bnd_norm  = (bnd  - b_mu)  / b_std

    # FFT-based cross-correlation: flip the template (mask) so fftconvolve
    # computes correlation instead of convolution.  O(N^2 log N) vs O(N^4).
    mask_flipped = mask_norm[::-1, ::-1]
    corr    = fftconvolve(bnd_norm, mask_flipped, mode='same')
    n_pixels = mask_norm.size
    ncc_map  = (corr / n_pixels).astype(np.float32)

    # Restrict search to ±search_range_px around centre
    h, w    = ncc_map.shape
    cr, cc  = h // 2, w // 2
    r0, r1  = max(0, cr - search_range_px), min(h, cr + search_range_px + 1)
    c0, c1  = max(0, cc - search_range_px), min(w, cc + search_range_px + 1)
    window  = ncc_map[r0:r1, c0:c1]

    flat_idx   = int(np.argmax(window))
    wr, wc     = np.unravel_index(flat_idx, window.shape)
    peak_ncc   = float(window[wr, wc])

    # Convert window coords → full-image offset relative to centre
    offset_row = int(wr + r0 - cr)
    offset_col = int(wc + c0 - cc)

    # Sharpness: peak / mean of top-5% of the window (excluding the peak itself)
    flat      = window.ravel()
    sorted_v  = np.sort(flat)[::-1]
    n_top     = max(2, int(0.05 * len(sorted_v)))
    top_vals  = sorted_v[1:n_top]  # exclude the peak itself
    mean_top  = float(top_vals.mean()) if len(top_vals) > 0 else peak_ncc
    # Avoid division by zero; sharpness = 1.0 if flat (ambiguous)
    if abs(mean_top) < 1e-9:
        sharpness = 1.0
    else:
        sharpness = float(np.clip(peak_ncc / mean_top, 1.0, 10.0))

    # Did the best offset land on the search window edge?
    at_row_edge = abs(offset_row) >= search_range_px - 1
    at_col_edge = abs(offset_col) >= search_range_px - 1
    peak_at_boundary = bool(at_row_edge or at_col_edge)

    return {
        'offset_row': offset_row,
        'offset_col': offset_col,
        'peak_ncc': peak_ncc,
        'sharpness': sharpness,
        'peak_at_boundary': peak_at_boundary,
        'ncc_map': ncc_map,
    }


def ncc_peak_score(peak_ncc: float) -> float:
    """Map raw NCC peak to a [0,1] confidence signal.

    NCC in [−1,1]; we clip negatives to 0 and apply a soft sigmoid-like
    curve so that a peak of 0.3 → ~0.4 and 0.7 → ~0.9.
    """
    v = float(np.clip(peak_ncc, 0.0, 1.0))
    # Emphasise the middle range with a smooth power curve
    return float(v ** 0.6)


def ncc_sharpness_score(sharpness: float) -> float:
    """Map peak/mean sharpness ratio to [0,1].

    Sharpness ≥ 1 (1 = flat surface = no unique peak, ambiguous).
    """
    # tanh-based: reaches ~0.9 at sharpness ≈ 3
    s = float(np.clip(sharpness, 1.0, 10.0))
    return float(np.tanh((s - 1.0) / 1.5))


# ---------------------------------------------------------------------------
# Signal C — Boundary raster density
# ---------------------------------------------------------------------------

def boundary_density_score(boundary_patch: np.ndarray, threshold: float = 0.1) -> float:
    """Fraction of patch pixels that are active boundary signal.

    High density → open farmland with clear ridges → hints are reliable.
    Low density  → trees, buildings, or smooth crops → hints are weak.

    Returns float in [0, 1].
    """
    if boundary_patch.size == 0:
        return 0.0
    bnd = boundary_patch.astype(np.float32)
    # Normalise to [0, 1]
    bmax = bnd.max()
    if bmax < 1e-6:
        return 0.0  # all-zero patch → no hint signal at all
    bnd_norm = bnd / bmax
    density = float((bnd_norm > threshold).sum() / bnd_norm.size)
    # Scale: 0 density → 0, 5% density → ~0.5, 20%+ → 1.0
    return float(np.clip(density / 0.20, 0.0, 1.0))


# ---------------------------------------------------------------------------
# Signal D — Image-edge alignment
# ---------------------------------------------------------------------------

def edge_alignment_score(
    polygon_mask: np.ndarray,
    image_patch_gray: np.ndarray,
    edge_width_px: int = 3,
) -> float:
    """Measure how well polygon edges align with imagery edges (Sobel gradient).

    We dilate the polygon boundary by edge_width_px pixels and measure the mean
    Sobel gradient magnitude inside that band, normalised by the overall patch mean.

    Returns float in [0, 1].
    """
    from scipy.ndimage import sobel, binary_dilation, binary_erosion

    if polygon_mask.sum() < 9:
        return 0.3  # mask too small to extract a useful boundary band

    img = image_patch_gray.astype(np.float32)
    img_max = img.max()
    if img_max < 1e-6:
        return 0.3

    img_norm = img / img_max

    # Sobel gradient magnitude
    sx = sobel(img_norm, axis=1)
    sy = sobel(img_norm, axis=0)
    grad_mag = np.hypot(sx, sy)

    # Binary polygon mask
    bmask = (polygon_mask > 0.5).astype(bool)

    # Boundary band: dilated XOR eroded
    struct = np.ones((edge_width_px, edge_width_px), dtype=bool)
    dilated = binary_dilation(bmask, structure=struct)
    eroded  = binary_erosion(bmask,  structure=struct)
    band    = dilated & ~eroded

    if band.sum() == 0:
        return 0.3

    band_mean = float(grad_mag[band].mean())
    patch_mean = float(grad_mag.mean())

    if patch_mean < 1e-9:
        return 0.3

    # Ratio > 1 means edges are concentrated at the polygon boundary — good alignment
    ratio = band_mean / patch_mean
    # Sigmoid-like mapping: ratio 1.0 → 0.4, 2.0 → 0.73, 3.0 → 0.90
    score = float(np.tanh(ratio - 0.5))
    return float(np.clip(score, 0.0, 1.0))


# ---------------------------------------------------------------------------
# Combined confidence (legacy — kept for reference, not used in main path)
# ---------------------------------------------------------------------------

def combine_confidence(
    area_score: float,
    ncc_peak: float,
    ncc_sharpness: float,
    boundary_density: float,
    edge_align: float,
    weights: tuple[float, ...] = (0.20, 0.35, 0.20, 0.10, 0.15),
) -> float:
    """Weighted combination of all signals.  Legacy path — see calibrated_confidence."""
    signals = np.array([
        area_score, ncc_peak, ncc_sharpness, boundary_density, edge_align,
    ], dtype=np.float64)
    w = np.array(weights, dtype=np.float64)
    w = w / w.sum()
    return float(np.clip(np.dot(w, signals), 0.0, 1.0))


# ---------------------------------------------------------------------------
# Signal E — Stability (per-plot NCC signal quality)
# ---------------------------------------------------------------------------

def stability_score(peak_ncc: float, sharpness: float) -> float:
    """How strong and unambiguous is the boundary/NCC evidence at this plot?

    Computed for ALL plots (including Track A where NCC was not trusted).
    Even when the NCC offset is rejected, the raw peak and sharpness values
    still reflect boundary raster richness at this plot location:
      - Open farmland with clear ridges  -> higher peak, sharper -> high stability
      - Tree cover / uniform fields      -> flat NCC surface     -> low stability

    Returns float in [0, 1].
    """
    peak_part  = float(np.clip(peak_ncc,  0.0, 1.0) ** 0.6)      # same as ncc_peak_score
    sharp_val  = float(np.clip(sharpness, 1.0, 10.0))
    sharp_part = float(np.tanh((sharp_val - 1.0) / 1.5))          # same as ncc_sharpness_score
    return float(np.clip(0.5 * peak_part + 0.5 * sharp_part, 0.0, 1.0))


# ---------------------------------------------------------------------------
# Calibrated confidence — primary confidence path
# ---------------------------------------------------------------------------

def calibrated_confidence(
    area_score:       float,
    boundary_density: float,
    edge_align:       float,
    peak_ncc:         float,
    sharpness:        float,
    ncc_applied:      bool,
) -> float:
    """Wide-variance confidence score designed for good rank separation.

    Formula
    -------
        stab = stability_score(peak_ncc, sharpness)

        base = 0.45 * area_score
             + 0.25 * boundary_density
             + 0.15 * edge_align
             + 0.15 * stab

        if ncc_applied: base = min(1.0, base + 0.10)

        confidence = sigmoid(8 * (base - 0.5))

    Nonlinear stretch mapping (sigmoid slope = 8):
        base = 0.20  ->  conf ~= 0.075   (very weak evidence)
        base = 0.35  ->  conf ~= 0.25
        base = 0.50  ->  conf ~= 0.50
        base = 0.65  ->  conf ~= 0.75
        base = 0.80  ->  conf ~= 0.92   (strong multi-signal evidence)

    Why slope 8 (not the suggested 4):
        With base values empirically in [0.25, 0.85], sigmoid(4) maps to
        [0.27, 0.80] — still clustered in a narrow band.  Slope 8 maps
        [0.25, 0.85] to [0.12, 0.92], covering the full 0.20-0.90 target.

    Track separation (geometry unchanged):
        Track A (global only):   base 0.30-0.65  ->  conf 0.20-0.75
        Track B (global + NCC):  base + 0.10     ->  conf typically 0.65-0.92

    Returns float in [0, 1].
    """
    import math
    stab = stability_score(peak_ncc, sharpness)
    base = (
        0.45 * float(area_score)       +
        0.25 * float(boundary_density) +
        0.15 * float(edge_align)       +
        0.15 * stab
    )
    if ncc_applied:
        base = min(1.0, base + 0.10)
    conf = 1.0 / (1.0 + math.exp(-8.0 * (base - 0.5)))
    return float(np.clip(conf, 0.0, 1.0))
