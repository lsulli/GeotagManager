# -*- coding: utf-8 -*-
"""
geopackage_exporter.py - Export geotagged photos to GeoPackage vector layer.
"""

import os
from datetime import datetime

from qgis.core import (
    QgsVectorLayer,
    QgsVectorFileWriter,
    QgsFeature,
    QgsGeometry,
    QgsPointXY,
    QgsField,
    QgsFields,
    QgsCoordinateReferenceSystem,
    QgsProject,
    QgsWkbTypes,
)
from qgis.PyQt.QtCore import QVariant, QDate


LAYER_NAME = "GeotagManager_Photos"  # legacy fallback


def make_date_prefix(photo_records):
    """Build date prefix from photo records.
    Single date  -> '20240226'
    Date range   -> 'Start20240226_End20240228'
    No dates     -> empty string
    """
    import os
    from datetime import datetime as _dt
    dates = set()
    for rec in photo_records:
        dt = rec.get("datetime")
        if dt and hasattr(dt, "date"):
            dates.add(dt.date())
    if not dates:
        return ""
    mn, mx = min(dates), max(dates)
    if mn == mx:
        return mn.strftime("%Y%m%d")
    return f"Start{mn.strftime('%Y%m%d')}_End{mx.strftime('%Y%m%d')}"


def _make_layer_name(output_path=None):
    """Return layer name derived from the GeoPackage filename.
    e.g. 20240226_layer.gpkg → 20240226_layer_photo_points
    """
    import os
    from datetime import datetime
    if output_path:
        base = os.path.splitext(os.path.basename(output_path))[0]
        base = base.replace(" ", "_")
        return f"{base}_photo_points"
    return f"photo_points_{datetime.now().strftime('%Y%m%d')}"

CRS_WGS84 = QgsCoordinateReferenceSystem("EPSG:4326")


def build_fields():
    """Build the QgsFields schema for the photo layer."""
    fields = QgsFields()
    fields.append(QgsField("filename", QVariant.String, len=255))
    fields.append(QgsField("filepath", QVariant.String, len=1024))
    fields.append(QgsField("author", QVariant.String, len=255))
    fields.append(QgsField("datetime_photo", QVariant.String, len=30))
    fields.append(QgsField("photo_date",     QVariant.Date))  # date only (no time)
    fields.append(QgsField("latitude", QVariant.Double))
    fields.append(QgsField("longitude", QVariant.Double))
    fields.append(QgsField("altitude",    QVariant.Double))
    fields.append(QgsField("gps_altitude",QVariant.Double))
    fields.append(QgsField("direction",        QVariant.Double))
    fields.append(QgsField("pdop",             QVariant.Double))
    fields.append(QgsField("focal_length",     QVariant.Double))
    fields.append(QgsField("focal_length_35mm",QVariant.Double))
    fields.append(QgsField("hfov",             QVariant.Double))
    fields.append(QgsField("camera",     QVariant.String, len=255))
    fields.append(QgsField("satellites", QVariant.Int))
    fields.append(QgsField("source", QVariant.String, len=50))
    fields.append(QgsField("notes",  QVariant.String, len=1024))
    return fields


def create_memory_layer():
    """Create an in-memory QgsVectorLayer for live editing."""
    layer = QgsVectorLayer("Point?crs=EPSG:4326", LAYER_NAME, "memory")
    pr = layer.dataProvider()
    pr.addAttributes(build_fields().toList())
    layer.updateFields()
    return layer


def photo_to_feature(layer, photo_record, author=""):
    """
    Build a QgsFeature from a photo_record dict.
    photo_record keys: filename, filepath, lat, lon, alt, datetime, source, notes
    """
    feat = QgsFeature(layer.fields())
    feat.setGeometry(QgsGeometry.fromPointXY(
        QgsPointXY(photo_record["lon"], photo_record["lat"])
    ))
    dt_str = ""
    dt = photo_record.get("datetime")
    if isinstance(dt, datetime):
        dt_str = dt.strftime("%Y-%m-%d %H:%M:%S")
    elif isinstance(dt, str):
        dt_str = dt

    feat["filename"] = os.path.basename(photo_record.get("filepath", ""))
    feat["filepath"] = photo_record.get("filepath", "")
    # Use per-record author if set, otherwise fall back to global param
    feat["author"] = photo_record.get("author", "") or author
    feat["datetime_photo"] = dt_str
    # Native QDate field — date only, enables temporal filtering in QGIS
    if isinstance(dt, datetime):
        feat["photo_date"] = QDate(dt.year, dt.month, dt.day)
    else:
        feat["photo_date"] = QDate()  # null date
    feat["latitude"] = photo_record.get("lat", 0.0)
    feat["longitude"] = photo_record.get("lon", 0.0)
    feat["altitude"]     = photo_record.get("alt") or 0.0
    exif_extra           = photo_record.get("_exif_extra") or {}
    feat["gps_altitude"] = exif_extra.get("alt")       or photo_record.get("alt") or 0.0
    feat["direction"]         = exif_extra.get("direction")         or 0.0
    feat["pdop"]              = exif_extra.get("pdop")              or 0.0
    feat["focal_length"]      = exif_extra.get("focal_length")      or 0.0
    feat["focal_length_35mm"] = exif_extra.get("focal_length_35mm") or 0.0
    feat["hfov"]              = exif_extra.get("hfov")              or 0.0
    # Camera model: combine make+model from exif_extra
    make  = (exif_extra.get("make") or photo_record.get("make") or "").strip()
    model = (exif_extra.get("model") or photo_record.get("model") or "").strip()
    if make and model and not model.startswith(make):
        camera_str = f"{make} {model}"
    elif model:
        camera_str = model
    else:
        camera_str = make
    feat["camera"]       = camera_str
    feat["satellites"]   = int(exif_extra.get("satellites") or
                               photo_record.get("satellites") or 0)
    feat["source"]            = photo_record.get("source", "gpx")
    feat["notes"]        = photo_record.get("notes", "")
    return feat


def export_to_geopackage(photo_records, output_path, author="", layer_name=None):
    """Write photo_records list to a GeoPackage file.
    Returns (success: bool, message: str, layer_name: str).
    """
    if layer_name is None:
        prefix = make_date_prefix(photo_records)
        layer_name = (
            f"{prefix}_photo_points" if prefix
            else _make_layer_name(output_path)
        )
    fields = build_fields()

    options = QgsVectorFileWriter.SaveVectorOptions()
    options.driverName = "GPKG"
    options.fileEncoding = "UTF-8"
    options.layerName = layer_name
    options.actionOnExistingFile = (
        QgsVectorFileWriter.CreateOrOverwriteLayer
        if os.path.exists(output_path)
        else QgsVectorFileWriter.CreateOrOverwriteFile
    )

    writer = QgsVectorFileWriter.create(
        output_path,
        fields,
        QgsWkbTypes.Point,
        CRS_WGS84,
        QgsProject.instance().transformContext(),
        options,
    )

    if writer.hasError() != QgsVectorFileWriter.NoError:
        return False, f"Errore creazione GeoPackage: {writer.errorMessage()}", None

    # Temporary memory layer to build features
    mem_layer = create_memory_layer()

    for rec in photo_records:
        feat = photo_to_feature(mem_layer, rec, author=author)
        writer.addFeature(feat)

    del writer  # flushes to disk

    return True, f"Esportati {len(photo_records)} punti in {output_path}", layer_name


def load_geopackage_layer(gpkg_path, layer_name=None):
    """Load a GeoPackage layer into the QGIS project and return it.
    Interroga i sublayer effettivi del GPKG per trovare il layer corretto.
    """
    # Leggi i sublayer effettivi dal file
    probe = QgsVectorLayer(gpkg_path, "", "ogr")
    sublayers = probe.dataProvider().subLayers() if probe.isValid() else []
    del probe

    # Estrai i nomi dei layer dal formato QGIS "id!!name!!..."
    available = []
    for sub in sublayers:
        parts = sub.split("!!")
        if len(parts) >= 2:
            available.append(parts[1])

    # Priorita': nome derivato dal file, poi photo_points, poi legacy, poi primo disponibile
    candidates = []
    if layer_name:
        candidates.append(layer_name)
    candidates.append(_make_layer_name(gpkg_path))
    candidates.append(LAYER_NAME)
    # aggiungi qualsiasi nome che contenga "photo_points"
    for a in available:
        if "photo_points" in a.lower() and a not in candidates:
            candidates.append(a)
    # infine il primo sublayer disponibile come last resort
    if available:
        candidates.append(available[0])

    for name in candidates:
        uri = f"{gpkg_path}|layername={name}"
        layer = QgsVectorLayer(uri, name, "ogr")
        if layer.isValid():
            QgsProject.instance().addMapLayer(layer)
            return layer

    return None
