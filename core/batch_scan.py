# -*- coding: utf-8 -*-
"""
batch_scan.py - Fast parallel EXIF scanner for large directory trees.

Scans a folder (optionally recursive), reads GPS + camera metadata
from every supported image via piexif, and exports geotagged photos
to GeoPackage (or CSV / GeoJSON).

Designed to handle thousands of files without loading images into memory.
Uses ThreadPoolExecutor for parallel EXIF reads.
"""

import os
import csv
import json
from datetime import datetime
import math
from concurrent.futures import ThreadPoolExecutor, as_completed

from qgis.PyQt.QtCore import QObject, pyqtSignal

# Supported extensions (lowercase)
ALL_EXTENSIONS = {
    '.jpg', '.jpeg',
    '.tif', '.tiff',
    '.png',
    '.heic', '.heif',
    '.webp',
    '.cr2', '.cr3',          # Canon RAW
    '.nef', '.nrw',          # Nikon RAW
    '.arw', '.srf', '.sr2',  # Sony RAW
    '.orf',                  # Olympus RAW
    '.rw2',                  # Panasonic RAW
    '.dng',                  # Adobe DNG
    '.raf',                  # Fujifilm RAW
    '.pef', '.ptx',          # Pentax RAW
}

DEFAULT_EXTENSIONS = {'.jpg', '.jpeg', '.tif', '.tiff'}

EXPORT_FORMATS = ['GeoPackage (.gpkg)', 'CSV (.csv)', 'GeoJSON (.geojson)']
EXPORT_EXTS    = ['.gpkg', '.csv', '.geojson']


# ---------------------------------------------------------------------------
#  EXIF reader
# ---------------------------------------------------------------------------

def _read_photo_meta(filepath, root_folder):
    """
    Read GPS + camera metadata from a single image file.
    Returns a dict or None if no GPS data found.
    Pure-Python via piexif — does NOT load pixel data.
    """
    try:
        from .exif_handler import _read_pure as _rp
        from .exif_reader import read_exif_pure, calc_hfov
        raw = read_exif_pure(filepath)
        if not raw.get('gps_lat') or not raw.get('gps_lon'):
            return None

        lat = raw['gps_lat']
        lon = raw['gps_lon']
        alt = raw.get('gps_alt')
        direction = raw.get('gps_img_direction')
        pdop      = raw.get('gps_dop')

        fl   = raw.get('focal_length')
        fl35 = raw.get('focal_length_35mm')
        fpx  = raw.get('focal_plane_x_res')
        fpu  = raw.get('focal_plane_res_unit')
        imgw = raw.get('pixel_x_dim')
        hfov = calc_hfov(fl35, fl, fpx, fpu, imgw)

        # Camera make/model
        make  = raw.get('make', '')
        model = raw.get('model', '')
        if make and model and not model.startswith(make):
            camera = f"{make} {model}"
        elif model:
            camera = model
        else:
            camera = make

        # DateTimeOriginal
        dt_str = None
        dt_raw = raw.get('datetime_original')
        if dt_raw:
            from datetime import datetime as _dt
            for fmt in ('%Y:%m:%d %H:%M:%S', '%Y-%m-%d %H:%M:%S'):
                try:
                    dt_str = _dt.strptime(dt_raw, fmt).strftime('%Y-%m-%d %H:%M:%S')
                    break
                except ValueError:
                    pass

        rel_dir = os.path.relpath(os.path.dirname(filepath), root_folder)
        if rel_dir == '.': rel_dir = ''

        return {
            'filepath':          os.path.normpath(filepath),
            'filename':          os.path.basename(filepath),
            'directory':         rel_dir,
            'lat':               round(lat, 7),
            'lon':               round(lon, 7),
            'alt':               alt,
            'direction':         direction,
            'pdop':              pdop,
            'focal_length':      fl,
            'focal_length_35mm': fl35,
            'hfov':              hfov,
            'datetime_photo':    dt_str or '',
            'camera':            camera,
        }

    except Exception:
        return None


def list_images_recursive(folder, extensions=None, recursive=True):
    """Return sorted list of image paths matching extensions."""
    if extensions is None:
        extensions = DEFAULT_EXTENSIONS
    images = []
    if recursive:
        for root, dirs, files in os.walk(folder):
            dirs.sort()
            for fname in sorted(files):
                if os.path.splitext(fname)[1].lower() in extensions:
                    images.append(os.path.join(root, fname))
    else:
        for fname in sorted(os.listdir(folder)):
            if os.path.splitext(fname)[1].lower() in extensions:
                images.append(os.path.join(folder, fname))
    return images


# ---------------------------------------------------------------------------
#  Worker thread
# ---------------------------------------------------------------------------

class BatchScanWorker(QObject):
    """
    Parallel EXIF scanner. Runs inside a QThread.

    Signals:
        progress(current, total, path)
        record_ready(dict)          — emitted for each geotagged photo found
        finished(list, int, int)    — (records, total_scanned, total_skipped)
        error(str)
    """

    progress     = pyqtSignal(int, int, str)
    record_ready = pyqtSignal(dict)
    finished     = pyqtSignal(list, int, int)
    error        = pyqtSignal(str)

    def __init__(self, image_paths, root_folder,
                 author='', max_workers=8, parent=None):
        super().__init__(parent)
        self.image_paths = image_paths
        self.root_folder = root_folder
        self.author      = author
        self.max_workers = max_workers
        self._cancelled  = False

    def cancel(self):
        self._cancelled = True

    def run(self):
        records  = []
        total    = len(self.image_paths)
        skipped  = 0
        done     = 0
        CHUNK    = 50   # emit progress every N files

        try:
            with ThreadPoolExecutor(max_workers=self.max_workers) as pool:
                future_map = {
                    pool.submit(_read_photo_meta, p, self.root_folder): p
                    for p in self.image_paths
                }
                for future in as_completed(future_map):
                    if self._cancelled:
                        pool.shutdown(wait=False, cancel_futures=True)
                        break
                    done += 1
                    try:
                        result = future.result()
                    except Exception:
                        result = None

                    if result:
                        result['author'] = self.author
                        result['scan_date'] = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
                        records.append(result)
                        self.record_ready.emit(result)
                    else:
                        skipped += 1

                    if done % CHUNK == 0 or done == total:
                        self.progress.emit(done, total, future_map[future])

        except Exception as e:
            self.error.emit(str(e))

        # Sort results by datetime_photo for consistent ordering
        records.sort(key=lambda r: r.get('datetime_photo') or '')
        self.finished.emit(records, total, skipped)


# ---------------------------------------------------------------------------
#  Exporters
# ---------------------------------------------------------------------------

def export_records(records, output_path, fmt, author=''):
    """
    Export records to the chosen format.
    fmt: 'gpkg' | 'csv' | 'geojson'
    Returns (success: bool, message: str)
    """
    if not records:
        return False, "No records to export."

    if fmt == 'gpkg':
        return _export_gpkg(records, output_path, author)
    elif fmt == 'csv':
        return _export_csv(records, output_path)
    elif fmt == 'geojson':
        return _export_geojson(records, output_path)
    else:
        return False, f"Unknown format: {fmt}"


def _export_gpkg(records, output_path, author):
    from qgis.core import (
        QgsVectorFileWriter, QgsVectorLayer, QgsFeature,
        QgsGeometry, QgsPointXY, QgsField, QgsFields,
        QgsWkbTypes, QgsCoordinateReferenceSystem, QgsProject,
    )
    from qgis.PyQt.QtCore import QVariant

    fields = QgsFields()
    for name, vtype, length in [
        ('filename',      QVariant.String, 255),
        ('filepath',      QVariant.String, 1024),
        ('directory',     QVariant.String, 512),
        ('author',        QVariant.String, 255),
        ('datetime_photo',QVariant.String, 30),
        ('latitude',      QVariant.Double, 0),
        ('longitude',     QVariant.Double, 0),
        ('altitude',      QVariant.Double, 0),
        ('direction',         QVariant.Double, 0),
        ('pdop',              QVariant.Double, 0),
        ('focal_length',      QVariant.Double, 0),
        ('focal_length_35mm', QVariant.Double, 0),
        ('hfov',              QVariant.Double, 0),
        ('camera',            QVariant.String, 255),
        ('scan_date',     QVariant.String, 30),
    ]:
        f = QgsField(name, vtype)
        if length: f.setLength(length)
        fields.append(f)

    opts = QgsVectorFileWriter.SaveVectorOptions()
    opts.driverName = 'GPKG'
    opts.fileEncoding = 'UTF-8'
    import os as _os
    _base = _os.path.splitext(_os.path.basename(output_path))[0].replace(' ','_')
    opts.layerName = f'{_base}_photo_points'
    opts.actionOnExistingFile = (
        QgsVectorFileWriter.CreateOrOverwriteLayer
        if os.path.exists(output_path)
        else QgsVectorFileWriter.CreateOrOverwriteFile
    )

    writer = QgsVectorFileWriter.create(
        output_path, fields, QgsWkbTypes.Point,
        QgsCoordinateReferenceSystem('EPSG:4326'),
        QgsProject.instance().transformContext(), opts,
    )
    if writer.hasError() != QgsVectorFileWriter.NoError:
        return False, f"GeoPackage error: {writer.errorMessage()}"

    mem = QgsVectorLayer('Point?crs=EPSG:4326', 'tmp', 'memory')
    mem.dataProvider().addAttributes(fields.toList())
    mem.updateFields()

    BATCH = 500
    buf = []
    for rec in records:
        feat = QgsFeature(mem.fields())
        feat.setGeometry(QgsGeometry.fromPointXY(
            QgsPointXY(rec['lon'], rec['lat'])
        ))
        feat['filename']       = rec.get('filename', '')
        _fp = rec.get('filepath', '')
        feat['filepath']       = os.path.normpath(_fp) if _fp else ''
        feat['directory']      = rec.get('directory', '')
        feat['author']         = rec.get('author', author)
        feat['datetime_photo'] = rec.get('datetime_photo', '')
        feat['latitude']       = rec.get('lat', 0.0)
        feat['longitude']      = rec.get('lon', 0.0)
        feat['altitude']       = rec.get('alt') or 0.0
        feat['direction']          = rec.get('direction') or 0.0
        feat['pdop']               = rec.get('pdop') or 0.0
        feat['focal_length']       = rec.get('focal_length') or 0.0
        feat['focal_length_35mm']  = rec.get('focal_length_35mm') or 0.0
        feat['hfov']               = rec.get('hfov') or 0.0
        feat['camera']             = rec.get('camera', '')
        feat['scan_date']      = rec.get('scan_date', '')
        buf.append(feat)
        if len(buf) >= BATCH:
            for f in buf: writer.addFeature(f)
            buf.clear()
    for f in buf:
        writer.addFeature(f)
    del writer
    return True, f"GeoPackage: {len(records)} points → {output_path}"


def _export_csv(records, output_path):
    fieldnames = [
        'filename', 'filepath', 'directory',
        'lat', 'lon', 'alt', 'direction', 'pdop',
        'focal_length', 'focal_length_35mm', 'hfov',
        'datetime_photo', 'camera',
        'author', 'scan_date',
    ]
    try:
        with open(output_path, 'w', newline='', encoding='utf-8-sig') as f:
            w = csv.DictWriter(f, fieldnames=fieldnames, extrasaction='ignore')
            w.writeheader()
            w.writerows(records)
        return True, f"CSV: {len(records)} rows → {output_path}"
    except Exception as e:
        return False, f"CSV error: {e}"


def _export_geojson(records, output_path):
    features = []
    for rec in records:
        features.append({
            'type': 'Feature',
            'geometry': {
                'type': 'Point',
                'coordinates': [rec['lon'], rec['lat'],
                                rec['alt'] or 0.0],
            },
            'properties': {
                k: v for k, v in rec.items()
                if k not in ('lat', 'lon', 'alt')
            },
        })
    fc = {'type': 'FeatureCollection', 'features': features}
    try:
        with open(output_path, 'w', encoding='utf-8') as f:
            json.dump(fc, f, ensure_ascii=False, indent=2)
        return True, f"GeoJSON: {len(records)} features → {output_path}"
    except Exception as e:
        return False, f"GeoJSON error: {e}"
