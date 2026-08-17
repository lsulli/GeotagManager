# -*- coding: utf-8 -*-
"""
gpx_interpolator.py - Parse GPX tracks and interpolate coordinates for photo timestamps.
"""

import xml.etree.ElementTree as ET
from datetime import datetime, timezone, timedelta


GPX_NS = {
    'gpx': 'http://www.topografix.com/GPX/1/1',
    'gpx10': 'http://www.topografix.com/GPX/1/0',
}


def parse_gpx(gpx_path):
    """
    Parse a GPX file and return list of track points.
    Each point: {'lat': float, 'lon': float, 'alt': float|None, 'time': datetime}
    Points are sorted by time ascending.
    """
    tree = ET.parse(gpx_path)
    root = tree.getroot()

    # Detect namespace
    tag = root.tag
    if 'http://www.topografix.com/GPX/1/1' in tag:
        ns = 'gpx'
    else:
        ns = 'gpx10'

    points = []

    # Parse trkpt elements
    for trkpt in root.iter('{' + GPX_NS[ns].replace('gpx:', '') + '}trkpt') if False else []:
        pass

    # More robust: iterate all trkpt regardless of namespace
    for elem in root.iter():
        if elem.tag.endswith('}trkpt') or elem.tag == 'trkpt':
            try:
                lat = float(elem.get('lat'))
                lon = float(elem.get('lon'))
            except (TypeError, ValueError):
                continue

            alt = None
            for child in elem:
                local = child.tag.split('}')[-1]
                if local == 'ele':
                    try:
                        alt = float(child.text)
                    except (TypeError, ValueError):
                        pass
                elif local == 'time':
                    time_str = child.text.strip() if child.text else None

            dt = None
            if time_str:
                dt = _parse_gpx_time(time_str)

            if dt is not None:
                points.append({'lat': lat, 'lon': lon, 'alt': alt, 'time': dt})

    # Also parse wpt (waypoints)
    for elem in root.iter():
        if elem.tag.endswith('}wpt') or elem.tag == 'wpt':
            try:
                lat = float(elem.get('lat'))
                lon = float(elem.get('lon'))
            except (TypeError, ValueError):
                continue
            alt = None
            time_str = None
            for child in elem:
                local = child.tag.split('}')[-1]
                if local == 'ele':
                    try:
                        alt = float(child.text)
                    except (TypeError, ValueError):
                        pass
                elif local == 'time':
                    time_str = child.text.strip() if child.text else None
            dt = _parse_gpx_time(time_str) if time_str else None
            if dt is not None:
                points.append({'lat': lat, 'lon': lon, 'alt': alt, 'time': dt})

    points.sort(key=lambda p: p['time'])
    return points


def _parse_gpx_time(time_str):
    """Parse ISO 8601 datetime string from GPX. Returns UTC-aware datetime."""
    if not time_str:
        return None
    # Common formats
    formats = [
        "%Y-%m-%dT%H:%M:%SZ",
        "%Y-%m-%dT%H:%M:%S.%fZ",
        "%Y-%m-%dT%H:%M:%S+00:00",
        "%Y-%m-%dT%H:%M:%S",
    ]
    for fmt in formats:
        try:
            dt = datetime.strptime(time_str, fmt)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt
        except ValueError:
            continue

    # Try with timezone offset like +01:00
    try:
        # Python 3.7+ fromisoformat doesn't handle Z
        clean = time_str.replace('Z', '+00:00')
        return datetime.fromisoformat(clean)
    except Exception:
        return None


def interpolate_position(track_points, photo_dt,
                         time_offset_seconds=0,
                         utc_offset_seconds=None):
    """
    Interpolate GPS position for a given photo datetime.

    Args:
        track_points:        list of track points from parse_gpx() — all UTC-aware
        photo_dt:            datetime of the photo (EXIF local time, usually naive)
        time_offset_seconds: camera clock correction (seconds to ADD to photo time)
        utc_offset_seconds:  UTC offset of the photo local time in seconds
                             (e.g. UTC+2 → 7200, UTC-5 → -18000).
                             If None, the local system timezone is used.

    Returns:
        dict with 'lat', 'lon', 'alt' or None if outside track range.
    """
    if not track_points:
        return None

    # Convert photo local time → UTC-aware datetime
    if photo_dt.tzinfo is None:
        # Determine UTC offset to apply
        if utc_offset_seconds is None:
            # Use system local timezone
            import time as _time
            utc_offset_seconds = -_time.timezone  # seconds east of UTC
            # Adjust for DST if currently active
            if _time.daylight and _time.localtime().tm_isdst:
                utc_offset_seconds = -_time.altzone
        tz = timezone(timedelta(seconds=utc_offset_seconds))
        # Interpret naive photo_dt as local time, then convert to UTC
        photo_dt = photo_dt.replace(tzinfo=tz).astimezone(timezone.utc)
    else:
        photo_dt = photo_dt.astimezone(timezone.utc)

    # Apply camera clock correction
    adjusted_dt = photo_dt + timedelta(seconds=time_offset_seconds)

    track_start = track_points[0]['time']
    track_end = track_points[-1]['time']

    # Outside range
    if adjusted_dt < track_start or adjusted_dt > track_end:
        return None

    # Find surrounding points
    before = None
    after = None
    for i, pt in enumerate(track_points):
        if pt['time'] <= adjusted_dt:
            before = pt
        if pt['time'] >= adjusted_dt and after is None:
            after = pt
        if before and after:
            break

    if before is None or after is None:
        return None

    # Exact match
    if before['time'] == after['time']:
        return {'lat': before['lat'], 'lon': before['lon'], 'alt': before['alt']}

    # Linear interpolation
    total_secs = (after['time'] - before['time']).total_seconds()
    elapsed_secs = (adjusted_dt - before['time']).total_seconds()
    ratio = elapsed_secs / total_secs if total_secs > 0 else 0

    lat = before['lat'] + (after['lat'] - before['lat']) * ratio
    lon = before['lon'] + (after['lon'] - before['lon']) * ratio

    alt = None
    if before['alt'] is not None and after['alt'] is not None:
        alt = before['alt'] + (after['alt'] - before['alt']) * ratio

    return {'lat': lat, 'lon': lon, 'alt': alt}


def get_track_time_range(track_points):
    """Return (start_datetime, end_datetime) tuple or (None, None)."""
    if not track_points:
        return None, None
    return track_points[0]['time'], track_points[-1]['time']
