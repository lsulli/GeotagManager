# -*- coding: utf-8 -*-
"""
layer_symbology.py  —  Apply GeotagManager symbology to a photo-point layer.

Symbology rules:
  1. Valid photo   (filepath exists on disk)  → filled green circle
  2. Invalid photo (filepath missing)         → hollow red circle
  3. HFOV wedge    (hfov > 0 AND direction valid) → translucent blue wedge
     oriented to camera direction, aperture = hfov degrees
     drawn as geometry generator on top of the point

The wedge is applied as a second symbol layer using a geometry generator
expression:  wedge_buffer(centroid($geometry), azimuth-hfov/2,
                          azimuth+hfov/2, radius)
where radius is fixed in map units (configurable).

All symbology is applied programmatically so the user never needs to
open the layer properties.
"""

import os

from qgis.core import (
    QgsVectorLayer,
    QgsSymbol,
    QgsMarkerSymbol,
    QgsFillSymbol,
    QgsSimpleMarkerSymbolLayer,
    QgsSimpleFillSymbolLayer,
    QgsGeometryGeneratorSymbolLayer,
    QgsRuleBasedRenderer,
    QgsProperty,
    QgsWkbTypes,
)
from qgis.PyQt.QtGui import QColor
from qgis.PyQt.QtCore import Qt


# ---------------------------------------------------------------------------
#  Configurable defaults
# ---------------------------------------------------------------------------

POINT_SIZE_VALID   = 5.0   # mm
POINT_SIZE_INVALID = 5.0   # mm
WEDGE_RADIUS_M     = 50.0  # wedge radius in metres (map units = degrees if EPSG:4326)
# For EPSG:4326 layers we convert to approximate degrees (1° ≈ 111 km)
WEDGE_RADIUS_DEG   = WEDGE_RADIUS_M / 111320.0

COLOR_VALID        = QColor(39,  174,  96, 230)   # green
COLOR_INVALID      = QColor(231,  76,  60, 200)   # red
COLOR_WEDGE_FILL   = QColor(41,  128, 185, 80)    # translucent blue
COLOR_WEDGE_STROKE = QColor(41,  128, 185, 180)


# ---------------------------------------------------------------------------
#  Public entry point
# ---------------------------------------------------------------------------

def apply_photo_symbology(layer: QgsVectorLayer,
                          wedge_radius_m: float = WEDGE_RADIUS_M) -> bool:
    """
    Apply rule-based symbology to a GeotagManager photo-point layer.

    Args:
        layer:          QgsVectorLayer to style
        wedge_radius_m: wedge radius in metres

    Returns True on success.
    """
    if not layer or not layer.isValid():
        return False

    fields = [f.name() for f in layer.fields()]
    has_filepath  = 'filepath'  in fields
    has_hfov      = 'hfov'      in fields
    has_direction = 'direction' in fields

    # Wedge radius in layer CRS units
    crs = layer.crs()
    if crs.isGeographic():
        radius = wedge_radius_m / 111320.0   # degrees
    else:
        radius = wedge_radius_m              # metres

    # Build renderer
    renderer = QgsRuleBasedRenderer(QgsSymbol.defaultSymbol(QgsWkbTypes.PointGeometry))
    root = renderer.rootRule()
    # Remove default rule
    for child in root.children():
        root.removeChild(child)

    # ── Rule 1: valid photo ────────────────────────────────────────────────
    sym_valid = _make_point_symbol(
        color=COLOR_VALID,
        size=POINT_SIZE_VALID,
        filled=True,
        shape='circle',
    )
    # Optionally add HFOV wedge as second symbol layer
    if has_hfov and has_direction:
        _add_wedge_symbol_layer(sym_valid, radius, filled=True)

    # Explicit NULL + empty check before file_exists avoids QGIS
    # expression errors on Windows paths with mixed separators
    filter_valid = (
        '"filepath" IS NOT NULL AND "filepath" != \'\''
        ' AND file_exists("filepath")'
        if has_filepath else 'TRUE'
    )
    rule_valid = QgsRuleBasedRenderer.Rule(sym_valid)
    rule_valid.setLabel('Valid photo')
    rule_valid.setFilterExpression(filter_valid)
    root.appendChild(rule_valid)

    # ── Rule 2: invalid photo (file missing) ──────────────────────────────
    sym_invalid = _make_point_symbol(
        color=COLOR_INVALID,
        size=POINT_SIZE_INVALID,
        filled=False,
        shape='circle',
    )
    # Only flag as invalid if filepath is present but file is missing
    filter_invalid = (
        '"filepath" IS NOT NULL AND "filepath" != \'\'' 
        ' AND NOT file_exists("filepath")'
        if has_filepath else 'FALSE'
    )
    rule_invalid = QgsRuleBasedRenderer.Rule(sym_invalid)
    rule_invalid.setLabel('Missing photo file')
    rule_invalid.setFilterExpression(filter_invalid)
    root.appendChild(rule_invalid)

    layer.setRenderer(renderer)
    layer.triggerRepaint()
    return True


# ---------------------------------------------------------------------------
#  Symbol builders
# ---------------------------------------------------------------------------

def _make_point_symbol(color: QColor, size: float,
                       filled: bool, shape: str = 'circle') -> QgsMarkerSymbol:
    """Create a simple marker symbol."""
    sym = QgsMarkerSymbol.createSimple({})
    sym.deleteSymbolLayer(0)   # remove default

    sl = QgsSimpleMarkerSymbolLayer()
    sl.setShape(QgsSimpleMarkerSymbolLayer.Shape.Circle)
    sl.setSize(size)
    sl.setSizeUnit(1)           # 1 = MM

    if filled:
        sl.setColor(color)
        sl.setStrokeColor(color.darker(130))
        sl.setStrokeWidth(0.3)
    else:
        sl.setColor(QColor(0, 0, 0, 0))    # transparent fill
        sl.setStrokeColor(color)
        sl.setStrokeWidth(0.8)
        sl.setStrokeStyle(Qt.SolidLine)

    sym.appendSymbolLayer(sl)
    return sym


def _add_wedge_symbol_layer(sym: QgsMarkerSymbol,
                             radius: float,
                             filled: bool = True):
    """
    Append a geometry-generator wedge symbol layer to an existing marker symbol.

    The wedge is only rendered when hfov > 0 and direction IS NOT NULL.
    Expression uses wedge_buffer() which returns a polygon in layer CRS units.
    """
    # wedge_buffer(center, azimuth_start, azimuth_end, inner_radius, outer_radius)
    # QGIS wedge_buffer: center point, start_azimuth, end_azimuth, radius
    # Azimuths in degrees clockwise from North
    expr = (
        f"if(\n"
        f"  \"hfov\" > 0 AND \"direction\" IS NOT NULL AND \"direction\" >= 0,\n"
        f"  wedge_buffer(\n"
        f"    $geometry,\n"
        f"    \"direction\" - \"hfov\" / 2.0,\n"
        f"    \"direction\" + \"hfov\" / 2.0,\n"
        f"    {radius:.8f}\n"
        f"  ),\n"
        f"  geom_from_wkt('GEOMETRYCOLLECTION EMPTY')\n"
        f")"
    )

    # geometry generator outputs a polygon — use fill symbol
    wedge_fill = QgsFillSymbol.createSimple({})
    wedge_fill.deleteSymbolLayer(0)

    fill_sl = QgsSimpleFillSymbolLayer()
    fill_sl.setFillColor(COLOR_WEDGE_FILL)
    fill_sl.setStrokeColor(COLOR_WEDGE_STROKE)
    fill_sl.setStrokeWidth(0.4)
    wedge_fill.appendSymbolLayer(fill_sl)

    gen = QgsGeometryGeneratorSymbolLayer.create({
        'geometryModifier': expr,
        'SymbolType': 'Fill',
    })
    gen.setSubSymbol(wedge_fill)
    gen.setGeometryType(QgsWkbTypes.PolygonGeometry)

    sym.appendSymbolLayer(gen)
