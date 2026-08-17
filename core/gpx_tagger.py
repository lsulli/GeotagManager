# -*- coding: utf-8 -*-
"""
gpx_tagger.py  —  GPX-to-photo geotag engine.

Strategy (same as MakeShapeFromExifGPS_15):
  1. If exiftool is available:
       exiftool -overwrite_original -geotag <gpx1> [-geotag <gpx2> ...]
                -geosync=<offset> <image_dir>
     exiftool handles all timezone/UTC conversion internally — the most
     reliable approach, matching the proven script behaviour.

  2. Fallback (no exiftool):
     Pure-Python interpolation via gpx_interpolator.py.
     Note: the fallback assumes camera clock == UTC.  If your camera
     clock is set to local time, use the camera clock offset control
     to compensate manually.

The worker emits the same signals as the old BatchWorker so the dialog
does not need structural changes.
"""

import os
import subprocess
import tempfile
from datetime import datetime, timezone, timedelta

from qgis.PyQt.QtCore import QObject, pyqtSignal

from .exif_handler    import find_exiftool, read_exif_gps, get_image_datetime
from .gpx_interpolator import parse_gpx, interpolate_position
from .batch_worker    import list_images, _find_surrounding_points

SUPPORTED_EXTENSIONS = {'.jpg', '.jpeg', '.tif', '.tiff'}


# ---------------------------------------------------------------------------
#  exiftool-based tagger
# ---------------------------------------------------------------------------

def geotag_with_exiftool(exiftool_path, image_paths, gpx_paths,
                          time_offset_seconds=0,
                          max_time_gap_seconds=0,
                          progress_cb=None):
    """
    Use exiftool -geotag to write GPS coordinates to images.

    exiftool handles UTC conversion, interpolation, and all edge cases.
    One exiftool call per image directory (grouped for efficiency).

    Args:
        exiftool_path:        path to exiftool binary
        image_paths:          list of absolute image paths
        gpx_paths:            list of GPX file paths
        time_offset_seconds:  camera clock offset in seconds (geosync value)
        max_time_gap_seconds: if > 0, passed as -geomaxtimesecs to exiftool
        progress_cb:          callable(current, total, filename) or None

    Returns:
        (matched_paths, skipped_paths, error_message)
    """
    if not image_paths or not gpx_paths:
        return [], image_paths, "No images or GPX files provided."

    # Group images by directory for efficiency
    by_dir = {}
    for p in image_paths:
        d = os.path.dirname(p)
        by_dir.setdefault(d, []).append(p)

    matched  = []
    skipped  = []
    total    = len(image_paths)
    done     = 0

    for img_dir, imgs in by_dir.items():
        # Build exiftool command
        args = [exiftool_path, '-overwrite_original']

        # Add all GPX files
        for gpx in gpx_paths:
            args += [f'-geotag={gpx}']

        # Camera clock offset: positive = camera ahead of GPS
        # exiftool geosync: negative = subtract from photo time to get GPS time
        # so if camera is +120s ahead: geosync=-120
        if time_offset_seconds != 0:
            args.append(f'-geosync={-time_offset_seconds}')

        # Max gap filter
        if max_time_gap_seconds > 0:
            args.append(f'-geomaxtimesecs={max_time_gap_seconds}')

        # Process whole directory (exiftool is much faster this way)
        args.append(img_dir)

        try:
            result = subprocess.run(
                args,
                capture_output=True, text=True, timeout=300
            )
            stdout = result.stdout + result.stderr
        except subprocess.TimeoutExpired:
            for p in imgs:
                skipped.append(p)
                done += 1
                if progress_cb:
                    progress_cb(done, total, os.path.basename(p))
            continue
        except Exception as e:
            for p in imgs:
                skipped.append(p)
            break

        # Parse exiftool output to classify matched/skipped per file
        # exiftool reports: "N image files updated" / "N image files unchanged"
        # Read back EXIF to determine which files got GPS
        for img_path in imgs:
            done += 1
            fname = os.path.basename(img_path)
            if progress_cb:
                progress_cb(done, total, fname)
            # Read back the result
            exif = read_exif_gps(img_path)
            if exif and exif.get('lat') is not None:
                matched.append(img_path)
            else:
                skipped.append(img_path)

    return matched, skipped, None


# ---------------------------------------------------------------------------
#  Pure-Python fallback tagger
# ---------------------------------------------------------------------------

def geotag_pure_python(image_paths, track_points,
                        time_offset_seconds=0,
                        max_time_gap_seconds=0,
                        utc_offset_seconds=None,
                        progress_cb=None):
    """
    Fallback: interpolate GPS from merged track points using pure Python.
    Returns (records_matched, skipped_paths).
    """
    matched = []
    skipped = []
    total   = len(image_paths)

    for i, img_path in enumerate(image_paths):
        fname = os.path.basename(img_path)
        if progress_cb:
            progress_cb(i + 1, total, fname)
        try:
            photo_dt = get_image_datetime(img_path)
            if photo_dt is None:
                skipped.append(img_path)
                continue

            # Gap check
            if max_time_gap_seconds > 0:
                dt_utc = photo_dt
                if dt_utc.tzinfo is None:
                    if utc_offset_seconds is not None:
                        tz = timezone(timedelta(seconds=utc_offset_seconds))
                    else:
                        import time as _t
                        off = -_t.timezone
                        if _t.daylight and _t.localtime().tm_isdst:
                            off = -_t.altzone
                        tz = timezone(timedelta(seconds=off))
                    dt_utc = dt_utc.replace(tzinfo=tz).astimezone(timezone.utc)
                dt_adj = dt_utc + timedelta(seconds=time_offset_seconds)
                before, after = _find_surrounding_points(track_points, dt_adj)
                if not before or not after:
                    skipped.append(img_path)
                    continue
                if (after['time'] - before['time']).total_seconds() > max_time_gap_seconds:
                    skipped.append(img_path)
                    continue

            pos = interpolate_position(
                track_points, photo_dt,
                time_offset_seconds=time_offset_seconds,
                utc_offset_seconds=utc_offset_seconds,
            )
            if pos is None:
                skipped.append(img_path)
                continue

            matched.append({
                'filepath':  img_path,
                'lat':       pos['lat'],
                'lon':       pos['lon'],
                'alt':       pos['alt'],
                'datetime':  photo_dt,
                'source':    'gpx',
                'notes':     '',
                'geotagged': False,
            })
        except Exception:
            skipped.append(img_path)

    return matched, skipped


# ---------------------------------------------------------------------------
#  Unified worker — same signal interface as old BatchWorker
# ---------------------------------------------------------------------------

class GeotagWorker(QObject):
    """
    Drop-in replacement for BatchWorker.
    Uses exiftool -geotag when available, pure Python otherwise.
    """

    progress        = pyqtSignal(int, int, str)
    photo_processed = pyqtSignal(dict)
    finished        = pyqtSignal(list, list)
    error           = pyqtSignal(str)

    def __init__(self, image_paths, gpx_paths, track_points,
                 time_offset_seconds=0,
                 max_time_gap_seconds=0,
                 utc_offset_seconds=None,
                 parent=None):
        super().__init__(parent)
        self.image_paths         = image_paths
        self.gpx_paths           = gpx_paths
        self.track_points        = track_points
        self.time_offset_seconds = time_offset_seconds
        self.max_time_gap_seconds= max_time_gap_seconds
        self.utc_offset_seconds  = utc_offset_seconds
        self._cancelled          = False

    def cancel(self):
        self._cancelled = True

    def run(self):
        et = find_exiftool()

        if et and self.gpx_paths:
            self._run_exiftool(et)
        else:
            self._run_pure_python()

    def _run_exiftool(self, et):
        """Run geotag via exiftool -geotag."""
        total = len(self.image_paths)

        def _progress(done, total, fname):
            if self._cancelled:
                return
            self.progress.emit(done, total, fname)

        self.progress.emit(0, total, "Running exiftool geotag…")

        matched_paths, skipped_paths, err = geotag_with_exiftool(
            et,
            self.image_paths,
            self.gpx_paths,
            time_offset_seconds=self.time_offset_seconds,
            max_time_gap_seconds=self.max_time_gap_seconds,
            progress_cb=_progress,
        )

        if err:
            self.error.emit(err)
            return

        # Build records by reading back the EXIF that exiftool just wrote
        matched_records = []
        for img_path in matched_paths:
            if self._cancelled:
                break
            exif = read_exif_gps(img_path)
            if exif and exif.get('lat') is not None:
                rec = {
                    'filepath':          img_path,
                    'lat':               exif['lat'],
                    'lon':               exif['lon'],
                    'alt':               exif.get('alt'),
                    'direction':         exif.get('direction'),
                    'pdop':              exif.get('pdop'),
                    'focal_length':      exif.get('focal_length'),
                    'focal_length_35mm': exif.get('focal_length_35mm'),
                    'hfov':              exif.get('hfov'),
                    'datetime':          exif.get('datetime'),
                    'source':            'gpx',
                    'notes':             '',
                    'geotagged':         True,
                }
                matched_records.append(rec)
                self.photo_processed.emit(rec)

        self.finished.emit(matched_records, skipped_paths)

    def _run_pure_python(self):
        """Fallback: pure Python interpolation."""
        def _progress(done, total, fname):
            if self._cancelled:
                return
            self.progress.emit(done, total, fname)

        pending = [p for p in self.image_paths
                   if not self._cancelled]

        matched, skipped = geotag_pure_python(
            pending,
            self.track_points,
            time_offset_seconds=self.time_offset_seconds,
            max_time_gap_seconds=self.max_time_gap_seconds,
            utc_offset_seconds=self.utc_offset_seconds,
            progress_cb=_progress,
        )

        for rec in matched:
            self.photo_processed.emit(rec)

        self.finished.emit(matched, skipped)
