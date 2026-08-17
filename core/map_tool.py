# -*- coding: utf-8 -*-
"""
map_tool.py - Custom QgsMapTool for placing/moving photo GPS points on the map canvas.
"""

from qgis.gui import QgsMapTool, QgsRubberBand, QgsMapToolEmitPoint
from qgis.core import (
    QgsWkbTypes,
    QgsPointXY,
    QgsCoordinateReferenceSystem,
    QgsCoordinateTransform,
    QgsProject,
)
from qgis.PyQt.QtCore import pyqtSignal, Qt
from qgis.PyQt.QtGui import QColor, QCursor, QPixmap
from qgis.PyQt.QtWidgets import QApplication


class GeotagPointTool(QgsMapTool):
    """
    Map tool that emits a WGS84 point when the user clicks on the canvas.
    Used to place or move a photo geotag position.
    """

    point_placed = pyqtSignal(float, float)  # lat, lon in WGS84

    def __init__(self, canvas):
        super().__init__(canvas)
        self.canvas = canvas
        self._rubber = QgsRubberBand(canvas, QgsWkbTypes.PointGeometry)
        self._rubber.setColor(QColor(255, 80, 0, 200))
        self._rubber.setIconSize(12)
        self._rubber.setIcon(QgsRubberBand.ICON_CROSS)
        self.setCursor(Qt.CrossCursor)

    def canvasReleaseEvent(self, event):
        """On left click: emit the WGS84 point."""
        if event.button() != Qt.LeftButton:
            return

        point_canvas = self.toMapCoordinates(event.pos())

        # Transform to WGS84
        crs_map = self.canvas.mapSettings().destinationCrs()
        crs_wgs84 = QgsCoordinateReferenceSystem("EPSG:4326")
        transform = QgsCoordinateTransform(crs_map, crs_wgs84, QgsProject.instance())
        point_wgs84 = transform.transform(point_canvas)

        # Show rubber band marker
        self._rubber.reset(QgsWkbTypes.PointGeometry)
        self._rubber.addPoint(point_canvas, True)

        self.point_placed.emit(point_wgs84.y(), point_wgs84.x())  # lat, lon

    def deactivate(self):
        """Clean up rubber band when tool is deactivated."""
        try:
            self._rubber.reset(QgsWkbTypes.PointGeometry)
            self.canvas.scene().removeItem(self._rubber)
        except Exception:
            pass
        super().deactivate()

    def keyPressEvent(self, event):
        """Escape cancels the tool."""
        if event.key() == Qt.Key_Escape:
            self.canvas.unsetMapTool(self)


class DirectionTool(QgsMapTool):
    """
    Two-click map tool to measure azimuth (direction from North).

    Click 1: origin point (camera position — pre-filled from photo coords)
    Click 2: target point (subject being photographed)

    Emits direction_set(float) with the azimuth in degrees [0-360].
    Draws a rubber band line between the two points while the user clicks.
    """

    direction_set = pyqtSignal(float)   # azimuth degrees 0-360

    def __init__(self, canvas, origin_wgs84=None):
        """
        canvas        : QgsMapCanvas
        origin_wgs84  : (lat, lon) tuple — pre-fills click 1 if provided.
                        If None the user must click twice.
        """
        super().__init__(canvas)
        self.canvas = canvas
        self._origin_wgs84  = origin_wgs84   # (lat, lon) or None
        self._origin_canvas = None            # QgsPointXY in canvas CRS
        self._step          = 1 if origin_wgs84 else 0  # 0=wait origin, 1=wait target

        # Rubber band line
        self._line = QgsRubberBand(canvas, QgsWkbTypes.LineGeometry)
        self._line.setColor(QColor(255, 160, 0, 220))
        self._line.setWidth(2)

        # Rubber band for origin point
        self._origin_rb = QgsRubberBand(canvas, QgsWkbTypes.PointGeometry)
        self._origin_rb.setColor(QColor(255, 80, 0, 220))
        self._origin_rb.setIconSize(10)
        self._origin_rb.setIcon(QgsRubberBand.ICON_CIRCLE)

        self.setCursor(Qt.CrossCursor)

        # If origin is pre-filled, project it to canvas CRS and draw it
        if origin_wgs84:
            self._project_origin(origin_wgs84[1], origin_wgs84[0])  # lon, lat

    def _project_origin(self, lon, lat):
        """Project WGS84 origin to canvas CRS and draw marker."""
        crs_wgs84  = QgsCoordinateReferenceSystem("EPSG:4326")
        crs_canvas = self.canvas.mapSettings().destinationCrs()
        tr = QgsCoordinateTransform(crs_wgs84, crs_canvas, QgsProject.instance())
        self._origin_canvas = tr.transform(QgsPointXY(lon, lat))
        self._origin_rb.reset(QgsWkbTypes.PointGeometry)
        self._origin_rb.addPoint(self._origin_canvas, True)

    def canvasMoveEvent(self, event):
        """Draw live rubber band line while moving toward target."""
        if self._step != 1 or self._origin_canvas is None:
            return
        target = self.toMapCoordinates(event.pos())
        self._line.reset(QgsWkbTypes.LineGeometry)
        self._line.addPoint(self._origin_canvas)
        self._line.addPoint(target, True)

    def canvasReleaseEvent(self, event):
        if event.button() != Qt.LeftButton:
            return
        # Single-click mode: origin is always pre-filled from photo GPS
        if self._origin_wgs84 is None or self._origin_canvas is None:
            return  # no origin — should not happen

        crs_map   = self.canvas.mapSettings().destinationCrs()
        crs_wgs84 = QgsCoordinateReferenceSystem("EPSG:4326")
        tr_to_wgs = QgsCoordinateTransform(crs_map, crs_wgs84, QgsProject.instance())

        click_canvas = self.toMapCoordinates(event.pos())
        click_wgs84  = tr_to_wgs.transform(click_canvas)

        import math
        lat1 = math.radians(self._origin_wgs84[0])
        lon1 = math.radians(self._origin_wgs84[1])
        lat2 = math.radians(click_wgs84.y())
        lon2 = math.radians(click_wgs84.x())
        dlon = lon2 - lon1
        x = math.sin(dlon) * math.cos(lat2)
        y = math.cos(lat1) * math.sin(lat2) - \
            math.sin(lat1) * math.cos(lat2) * math.cos(dlon)
        azimuth = (math.degrees(math.atan2(x, y)) + 360) % 360

        self.direction_set.emit(round(azimuth, 1))
        self.canvas.unsetMapTool(self)

    def deactivate(self):
        try:
            self._line.reset(QgsWkbTypes.LineGeometry)
            sc = self.canvas.scene()
            if sc:
                sc.removeItem(self._line)
                sc.removeItem(self._origin_rb)
        except Exception:
            pass
        super().deactivate()

    def keyPressEvent(self, event):
        if event.key() == Qt.Key_Escape:
            self.canvas.unsetMapTool(self)
