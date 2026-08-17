# -*- coding: utf-8 -*-
"""
batch_worker.py - Threaded batch processing for photo geotagging.

Supports:
  - Multiple GPX track files (merged and sorted by time)
  - max_time_gap_seconds: maximum allowed interpolation gap between
    the two surrounding trackpoints. If the gap exceeds the threshold
    the photo is skipped.
"""

import os
from datetime import timedelta, timezone

from qgis.PyQt.QtCore import QObject, pyqtSignal

from .exif_handler import get_image_datetime
from .gpx_interpolator import parse_gpx, interpolate_position

SUPPORTED_EXTENSIONS = {'.jpg', '.jpeg', '.tif', '.tiff'}


def list_images(folder, recursive=False):
    """
    Return sorted list of image paths in folder.
    If recursive=True, walks all subdirectories.
    """
    images = []
    if recursive:
        for root, dirs, files in os.walk(folder):
            dirs.sort()   # deterministic traversal order
            for fname in sorted(files):
                if os.path.splitext(fname)[1].lower() in SUPPORTED_EXTENSIONS:
                    images.append(os.path.join(root, fname))
    else:
        for fname in sorted(os.listdir(folder)):
            if os.path.splitext(fname)[1].lower() in SUPPORTED_EXTENSIONS:
                images.append(os.path.join(folder, fname))
    return images


def merge_track_points(gpx_paths):
    """
    Parse and merge multiple GPX files into a single sorted track list.
    Returns (track_points, info_list).
    info_list: [{"path", "name", "points"}, ...]
    """
    all_points = []
    info_list  = []
    for path in gpx_paths:
        pts = parse_gpx(path)
        info_list.append({
            "path":   path,
            "name":   os.path.basename(path),
            "points": len(pts),
        })
        all_points.extend(pts)
    all_points.sort(key=lambda p: p["time"])
    return all_points, info_list


def _find_surrounding_points(track_points, adjusted_dt):
    """Return (before, after) trackpoints surrounding adjusted_dt."""
    before = None
    after  = None
    for pt in track_points:
        if pt["time"] <= adjusted_dt:
            before = pt
        if pt["time"] >= adjusted_dt and after is None:
            after = pt
        if before and after:
            break
    return before, after


class BatchWorker(QObject):
    """
    Processes images against merged GPX tracks inside a QThread.
    """

    progress        = pyqtSignal(int, int, str)
    photo_processed = pyqtSignal(dict)
    finished        = pyqtSignal(list, list)
    error           = pyqtSignal(str)

    def __init__(self, image_paths, track_points,
                 time_offset_seconds=0,
                 max_time_gap_seconds=0,
                 utc_offset_seconds=None,
                 parent=None):
        super().__init__(parent)
        self.image_paths          = image_paths
        self.track_points         = track_points
        self.time_offset_seconds  = time_offset_seconds
        self.max_time_gap_seconds = max_time_gap_seconds
        self.utc_offset_seconds   = utc_offset_seconds
        self._cancelled           = False

    def cancel(self):
        self._cancelled = True

    def run(self):
        matched = []
        skipped = []
        total   = len(self.image_paths)

        for i, img_path in enumerate(self.image_paths):
            if self._cancelled:
                break

            fname = os.path.basename(img_path)
            self.progress.emit(i + 1, total, fname)

            try:
                photo_dt = get_image_datetime(img_path)
                if photo_dt is None:
                    skipped.append(img_path)
                    continue

                # Time gap check
                if self.max_time_gap_seconds > 0:
                    # Convert photo local time → UTC (same logic as interpolate_position)
                    dt_utc = photo_dt
                    if dt_utc.tzinfo is None:
                        if self.utc_offset_seconds is not None:
                            tz_local = timezone(timedelta(seconds=self.utc_offset_seconds))
                        else:
                            import time as _time
                            off = -_time.timezone
                            if _time.daylight and _time.localtime().tm_isdst:
                                off = -_time.altzone
                            tz_local = timezone(timedelta(seconds=off))
                        dt_utc = dt_utc.replace(tzinfo=tz_local).astimezone(timezone.utc)
                    dt_adj = dt_utc + timedelta(seconds=self.time_offset_seconds)
                    before, after = _find_surrounding_points(self.track_points, dt_adj)
                    if before is None or after is None:
                        skipped.append(img_path)
                        continue
                    gap = (after["time"] - before["time"]).total_seconds()
                    if gap > self.max_time_gap_seconds:
                        skipped.append(img_path)
                        continue

                pos = interpolate_position(
                    self.track_points, photo_dt,
                    time_offset_seconds=self.time_offset_seconds,
                    utc_offset_seconds=self.utc_offset_seconds,
                )
                if pos is None:
                    skipped.append(img_path)
                    continue

                record = {
                    "filepath":  img_path,
                    "lat":       pos["lat"],
                    "lon":       pos["lon"],
                    "alt":       pos["alt"],
                    "datetime":  photo_dt,
                    "source":    "gpx",
                    "notes":     "",
                    "geotagged": False,
                }
                matched.append(record)
                self.photo_processed.emit(record)

            except Exception:
                skipped.append(img_path)

        self.finished.emit(matched, skipped)
