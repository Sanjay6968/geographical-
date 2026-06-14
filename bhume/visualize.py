"""
bhume/visualize.py
==================
Debug visualizer for the LCCGR boundary correction pipeline.

For each plot it generates a 4-panel PNG:

  ┌──────────────┬──────────────┬──────────────┬──────────────┐
  │  Official    │ Global Shift │   Local NCC  │  Confidence  │
  │  (original)  │  (seed)      │  (refined)   │  overlay     │
  └──────────────┴──────────────┴──────────────┴──────────────┘

Saved to:  outputs/debug/<village>/<plot_number>.png

Usage (from solve.py after correction):
    from bhume.visualize import DebugVisualizer
    viz = DebugVisualizer(village, img_src, output_root=Path("outputs/debug"))
    viz.save(result, global_geom_4326, seed_geom_4326)
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import numpy as np
import rasterio
from PIL import Image, ImageDraw, ImageFont
from pyproj import Transformer
from rasterio.features import rasterize
from rasterio.windows import from_bounds as window_from_bounds
from shapely.geometry import mapping
from shapely.geometry.base import BaseGeometry
from shapely.ops import transform as shp_transform

from bhume.corrector import CorrectionResult

logger = logging.getLogger(__name__)


# ── Styling constants ──────────────────────────────────────────────────────

PANEL_W      = 320          # pixels per panel
PANEL_H      = 300          # pixels per panel (image area)
HEADER_H     = 28           # pixels for the panel title bar
FOOTER_H     = 48           # pixels for the bottom confidence bar
BORDER       = 2            # inner border between panels
PANELS       = 4            # number of panels

# Outline colours (RGBA)
COL_OFFICIAL  = (255,  80,  80, 220)   # red  – original
COL_GLOBAL    = (255, 180,  40, 220)   # amber – global shift
COL_LOCAL     = ( 50, 220,  80, 220)   # green – NCC refined
COL_CONF      = ( 60, 160, 255, 220)   # blue  – confidence overlay

PAD_M = 60.0   # metres of context around the plot


# ── Colour helpers ─────────────────────────────────────────────────────────

def _confidence_colour(conf: float | None) -> tuple[int, int, int]:
    """Lerp red→yellow→green for confidence 0→0.5→1."""
    if conf is None:
        return (120, 120, 120)
    c = float(np.clip(conf, 0.0, 1.0))
    if c < 0.5:
        t = c / 0.5
        r = int(220 + (220 - 220) * t)
        g = int(60  + (180 - 60)  * t)
        b = int(60  + (40  - 60)  * t)
    else:
        t = (c - 0.5) / 0.5
        r = int(220 + (60  - 220) * t)
        g = int(180 + (210 - 180) * t)
        b = int(40  + (80  - 40)  * t)
    return (r, g, b)


def _load_font(size: int = 13) -> ImageFont.ImageFont:
    try:
        # Try a few common monospace fonts available on Windows / Linux
        for name in ('consola.ttf', 'DejaVuSansMono.ttf', 'Courier New.ttf', 'arial.ttf'):
            try:
                return ImageFont.truetype(name, size)
            except OSError:
                continue
    except Exception:
        pass
    return ImageFont.load_default()


# ── Geometry → imagery-CRS helpers ────────────────────────────────────────

def _to_img_crs(geom_4326: BaseGeometry, img_crs: str) -> BaseGeometry:
    tf = Transformer.from_crs('EPSG:4326', img_crs, always_xy=True)
    return shp_transform(lambda xs, ys, _=None: tf.transform(xs, ys), geom_4326)


def _read_rgb_patch(
    src: rasterio.DatasetReader,
    geom_4326: BaseGeometry,
    pad_m: float,
) -> tuple[np.ndarray, Any] | None:
    """Read (H,W,3) uint8 RGB patch + window_transform.  Returns None if OOB."""
    tf_fwd = Transformer.from_crs('EPSG:4326', str(src.crs), always_xy=True)

    # Collect all coords from the geometry
    def _coords(g: BaseGeometry) -> list[tuple[float, float]]:
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

    window  = window_from_bounds(left, bottom, right, top, transform=src.transform)
    try:
        rgb = src.read([1, 2, 3], window=window)           # (3, H, W)
    except Exception:
        return None

    win_tf  = src.window_transform(window)
    image   = np.transpose(rgb, (1, 2, 0))                 # (H, W, 3)
    return image, win_tf


def _draw_polygon_outline(
    draw: ImageDraw.ImageDraw,
    geom_img_crs: BaseGeometry,
    win_tf: Any,
    colour: tuple[int, int, int, int],
    width: int = 2,
) -> None:
    """Project a polygon from imagery CRS → pixel coords and draw its outline."""
    # win_tf: affine  col = (x - c) / a,  row = (y - f) / e
    a, b, c, d, e, f = (
        win_tf.a, win_tf.b, win_tf.c,
        win_tf.d, win_tf.e, win_tf.f,
    )

    def _xy_to_rc(x: float, y: float) -> tuple[float, float]:
        # pixel col (x-axis) and row (y-axis, y decreases upward in raster)
        col = (x - c) / a
        row = (y - f) / e
        return col, row

    def _draw_ring(coords):
        pts = [_xy_to_rc(x, y) for x, y in coords]
        if len(pts) < 2:
            return
        for i in range(len(pts) - 1):
            x0, y0 = pts[i]
            x1, y1 = pts[i + 1]
            draw.line([(x0, y0), (x1, y1)], fill=colour, width=width)

    def _draw_geom(g: BaseGeometry) -> None:
        if g is None or g.is_empty:
            return
        if hasattr(g, 'geoms'):
            for part in g.geoms:
                _draw_geom(part)
        elif hasattr(g, 'exterior'):
            _draw_ring(list(g.exterior.coords))
            for interior in g.interiors:
                _draw_ring(list(interior.coords))

    _draw_geom(geom_img_crs)


# ── Panel builder ──────────────────────────────────────────────────────────

def _make_panel(
    base_rgb: np.ndarray,              # (H, W, 3) uint8 — satellite crop
    win_tf: Any,                       # affine for the crop
    img_crs: str,
    geometries: list[tuple[BaseGeometry | None, tuple[int,int,int,int], str]],
    title: str,
    panel_wh: tuple[int, int],
) -> Image.Image:
    """Build one panel: resize base image, overlay polygon outlines, add title."""
    pw, ph = panel_wh

    # Resize satellite image to panel dimensions
    base_pil = Image.fromarray(base_rgb).resize((pw, ph), Image.BILINEAR)
    overlay  = base_pil.convert('RGBA')
    draw     = ImageDraw.Draw(overlay, 'RGBA')

    H_orig, W_orig = base_rgb.shape[:2]
    sx = pw / W_orig
    sy = ph / H_orig

    # Scale the win_tf for the resized image
    from rasterio.transform import Affine
    scaled_tf = Affine(
        win_tf.a * (1 / sx),   win_tf.b,               win_tf.c,
        win_tf.d,               win_tf.e * (1 / sy),    win_tf.f,
    )

    for geom_4326, colour, _label in geometries:
        if geom_4326 is None:
            continue
        try:
            geom_ic = _to_img_crs(geom_4326, img_crs)
            _draw_polygon_outline(draw, geom_ic, scaled_tf, colour, width=2)
        except Exception as exc:
            logger.debug('draw polygon error: %s', exc)

    panel = overlay.convert('RGB')

    # Title bar
    font_title = _load_font(11)
    titled     = Image.new('RGB', (pw, HEADER_H + ph), (20, 20, 28))
    titled.paste(panel, (0, HEADER_H))
    d2 = ImageDraw.Draw(titled)
    d2.rectangle([(0, 0), (pw, HEADER_H)], fill=(35, 35, 50))
    d2.text((6, 5), title, fill=(210, 210, 220), font=font_title)

    return titled


# ── Main visualizer class ──────────────────────────────────────────────────

class DebugVisualizer:
    """Generates one 4-panel debug image per corrected/flagged plot.

    Parameters
    ----------
    village_slug    : str, e.g. 'vadnerbhairav'
    img_src         : open rasterio dataset for the satellite imagery
    output_root     : root path for debug images (outputs/debug/)
    max_plots       : cap on how many debug images to generate (0 = unlimited)
    """

    def __init__(
        self,
        village_slug: str,
        img_src: rasterio.DatasetReader,
        output_root: Path,
        max_plots: int = 0,
    ) -> None:
        self.village_slug = village_slug
        self.img_src      = img_src
        self.img_crs      = str(img_src.crs)
        self.output_dir   = output_root / village_slug
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.max_plots    = max_plots
        self._count       = 0

    def save(
        self,
        result: CorrectionResult,
        official_geom_4326: BaseGeometry,
        global_geom_4326: BaseGeometry,
    ) -> Path | None:
        """Generate and save the 4-panel debug image for one plot.

        Parameters
        ----------
        result              : CorrectionResult from PlotCorrector.correct()
        official_geom_4326  : original un-shifted geometry
        global_geom_4326    : after global seed shift only (before local NCC)
        """
        if self.max_plots > 0 and self._count >= self.max_plots:
            return None

        plot_number = result.plot_number
        # Sanitize: replace path-unsafe chars (e.g. '1006/B' -> '1006_B')
        safe_name = str(plot_number).replace('/', '_').replace('\\', '_').replace(':', '_')

        # Use the union of all geometries for the patch extent
        try:
            patch_geom = official_geom_4326  # base reference extent
            rgb_result = _read_rgb_patch(self.img_src, patch_geom, pad_m=PAD_M)
            if rgb_result is None:
                logger.debug('Plot %s: no imagery patch — skipping debug image', plot_number)
                return None

            base_rgb, win_tf = rgb_result

            # ── Panel geometries ─────────────────────────────────────
            refined_geom = result.geometry_4326 if result.status == 'corrected' else None

            panels_spec = [
                # (base_rgb, win_tf, geoms_to_overlay, title)
                (
                    [(official_geom_4326, COL_OFFICIAL, 'official')],
                    'Official (original)',
                ),
                (
                    [(official_geom_4326, COL_OFFICIAL, 'official'),
                     (global_geom_4326,   COL_GLOBAL,   'global')],
                    'Global Shift (seed)',
                ),
                (
                    [(official_geom_4326,  COL_OFFICIAL, 'official'),
                     (global_geom_4326,    COL_GLOBAL,   'global'),
                     (refined_geom,        COL_LOCAL,    'local')],
                    'Local NCC (refined)' if result.status == 'corrected'
                    else 'Flagged (no correction)',
                ),
                (
                    [(official_geom_4326,  COL_OFFICIAL, 'official'),
                     (refined_geom,        COL_LOCAL,    'local')],
                    f'Confidence = {result.confidence:.3f}'
                    if result.confidence is not None else 'Status: FLAGGED',
                ),
            ]

            panel_imgs = []
            pw, ph     = PANEL_W, PANEL_H

            for geoms, title in panels_spec:
                panel = _make_panel(
                    base_rgb, win_tf, self.img_crs, geoms, title, (pw, ph),
                )
                panel_imgs.append(panel)

            # ── Assemble 4-panel canvas ───────────────────────────────
            total_w = pw * PANELS + BORDER * (PANELS - 1)
            total_h = HEADER_H + ph + FOOTER_H

            canvas = Image.new('RGB', (total_w, total_h), (12, 12, 18))

            for idx, img in enumerate(panel_imgs):
                x = idx * (pw + BORDER)
                canvas.paste(img, (x, 0))

            # ── Footer bar ────────────────────────────────────────────
            conf_colour = _confidence_colour(result.confidence)
            draw = ImageDraw.Draw(canvas)
            draw.rectangle(
                [(0, HEADER_H + ph), (total_w, total_h)],
                fill=(20, 20, 28),
            )

            # Confidence bar fill
            if result.confidence is not None:
                bar_w = int(total_w * result.confidence)
                draw.rectangle(
                    [(0, HEADER_H + ph + 4), (bar_w, HEADER_H + ph + 12)],
                    fill=conf_colour,
                )

            font_footer = _load_font(12)
            font_big    = _load_font(16)

            # Plot number
            draw.text(
                (10, HEADER_H + ph + 14),
                f'Plot {plot_number}  ·  {self.village_slug}',
                fill=(200, 200, 210),
                font=font_footer,
            )

            # Status + confidence on right
            status_txt = (
                f'Status: {result.status.upper()}  |  Confidence: {result.confidence:.3f}'
                if result.confidence is not None
                else f'Status: FLAGGED  |  {result.method_note[:50]}'
            )
            draw.text(
                (10, HEADER_H + ph + 30),
                status_txt,
                fill=conf_colour,
                font=font_footer,
            )

            # Legend (bottom-right corner)
            legend_items = [
                ('━', COL_OFFICIAL[:3], 'Official'),
                ('━', COL_GLOBAL[:3],   'Global shift'),
                ('━', COL_LOCAL[:3],    'Local NCC'),
            ]
            lx = total_w - 220
            ly = HEADER_H + ph + 8
            for sym, col, lbl in legend_items:
                draw.text((lx, ly), sym, fill=col,            font=font_footer)
                draw.text((lx + 16, ly), lbl, fill=(180,180,190), font=font_footer)
                lx += 75

            # ── Separator lines between panels ────────────────────────
            for i in range(1, PANELS):
                x = i * (pw + BORDER) - BORDER
                draw.rectangle([(x, 0), (x + BORDER, HEADER_H + ph)], fill=(40, 40, 55))

            # ── Save ─────────────────────────────────────────────────
            out_path = self.output_dir / f'{safe_name}.png'
            canvas.save(out_path, 'PNG', optimize=False)
            self._count += 1
            return out_path

        except Exception as exc:
            logger.warning('Plot %s: debug image failed (%s)', plot_number, exc)
            return None
