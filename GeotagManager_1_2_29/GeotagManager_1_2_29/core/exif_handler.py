# -*- coding: utf-8 -*-
"""
exif_handler.py  —  Unified EXIF read/write with four-tier engine priority.

Priority order (read):
  1. Bundled exiftool  (vendor/exiftool[.exe])         — most complete
  2. System exiftool   (found in PATH / OSGeo4W)        — also complete
  3. piexif            (pip install piexif)              — JPEG only
  4. Pure Python       (core/exif_reader.py)            — always available

Priority order (write):
  1. Bundled exiftool
  2. System exiftool
  3. piexif
  4. Pure Python reader is read-only; if nothing else available → raise

The caller never needs to know which engine was used.
"""

import os
import subprocess
import struct
from datetime import datetime

from .exif_reader import read_exif_pure, calc_hfov

# ── Optional engines ──────────────────────────────────────────────────────
try:
    import piexif
    HAS_PIEXIF = True
except ImportError:
    HAS_PIEXIF = False

# Cache for exiftool path (avoid repeated searches)
_EXIFTOOL_CACHE = None   # None = not searched yet, False = not found, str = path


# ---------------------------------------------------------------------------
#  Engine discovery
# ---------------------------------------------------------------------------

def find_exiftool(force_rescan=False):
    """
    Cerca exiftool SOLO nella cartella vendor/ del plugin.
    Nessuna ricerca nel PATH di sistema o in percorsi globali.
    Risultato cachato; usa force_rescan=True per invalidare.
    """
    global _EXIFTOOL_CACHE
    if _EXIFTOOL_CACHE is not None and not force_rescan:
        return _EXIFTOOL_CACHE if _EXIFTOOL_CACHE else None

    from .exiftool_manager import bundled_exiftool_path
    bundled = bundled_exiftool_path()
    if bundled and os.path.isfile(bundled):
        # Verifica che sia eseguibile
        try:
            r = subprocess.run(
                [bundled, '-ver'],
                capture_output=True, text=True, timeout=5
            )
            if r.returncode == 0:
                _EXIFTOOL_CACHE = bundled
                return bundled
        except (FileNotFoundError, PermissionError, subprocess.TimeoutExpired):
            pass

    _EXIFTOOL_CACHE = False
    return None


def engine_info():
    """Restituisce una stringa descrittiva del motore EXIF attivo."""
    et = find_exiftool()
    if et:
        from .exiftool_manager import bundled_version
        ver = bundled_version() or '?'
        return f"ExifTool {ver} (bundled)"
    if HAS_PIEXIF:
        return "piexif (JPEG only) — install ExifTool for full functionality"
    return "Pure Python reader (built-in) — install ExifTool for full functionality"


# ---------------------------------------------------------------------------
#  Helpers shared by exiftool paths
# ---------------------------------------------------------------------------

def _run_et(exiftool, args, timeout=15):
    return subprocess.run(
        [exiftool] + args,
        capture_output=True, text=True, timeout=timeout
    )


def _float_safe(s):
    try:
        return float(s) if s else None
    except ValueError:
        return None


# ---------------------------------------------------------------------------
#  Public read API
# ---------------------------------------------------------------------------

def read_exif_batch(folder_or_paths):
    """
    Legge i metadati EXIF da una cartella o lista di file
    con UNA SOLA chiamata a exiftool (molto piu' veloce del
    chiamarlo per ogni singola immagine).

    Returns:
        dict { filepath: exif_dict } con le stesse chiavi di read_exif_gps()
        oppure {} se exiftool non e' disponibile.
    """
    et = find_exiftool()
    if not et:
        return {}   # fallback: il chiamante usera' read_exif_gps() file per file

    if isinstance(folder_or_paths, str):
        # E' una cartella: passa la directory direttamente a exiftool
        targets = [folder_or_paths]
        is_dir  = True
    else:
        targets = list(folder_or_paths)
        is_dir  = False

    if not targets:
        return {}

    try:
        args = [
            et, '-csv', '-n', '-r' if is_dir else '',
            '-GPSLatitude#', '-GPSLongitude#', '-GPSAltitude#',
            '-GPSImgDirection#', '-GPSDOP#',
            '-FocalLength#', '-FocalLengthIn35mmFormat#',
            '-FocalPlaneXResolution#', '-FocalPlaneResolutionUnit#',
            '-ExifImageWidth#',
            '-DateTimeOriginal',
            '-Make', '-Model', '-GPSSatellites#',
        ]
        # Rimuovi stringa vuota se is_dir=False
        args = [a for a in args if a]
        args += targets

        r = subprocess.run(
            args,
            capture_output=True, text=True,
            timeout=120   # batch puo' richiedere piu' tempo
        )
        if r.returncode != 0 or not r.stdout.strip():
            return {}

        results = {}
        lines = r.stdout.splitlines()
        if len(lines) < 2:
            return {}

        headers = [h.strip() for h in lines[0].split(',')]

        for line in lines[1:]:
            if not line.strip():
                continue
            # CSV split robusto: gestisce virgole dentro campi quotati
            import csv as _csv, io as _io
            row = next(_csv.reader(_io.StringIO(line)))
            if len(row) < len(headers):
                row += [''] * (len(headers) - len(row))
            data = dict(zip(headers, row))

            src_file = data.get('SourceFile', '').strip()
            if not src_file:
                continue

            lat = _float_safe(data.get('GPSLatitude'))
            lon = _float_safe(data.get('GPSLongitude'))

            fl   = _float_safe(data.get('FocalLength'))
            fl35 = _float_safe(data.get('FocalLengthIn35mmFormat'))
            fpx  = _float_safe(data.get('FocalPlaneXResolution'))
            fpu  = _float_safe(data.get('FocalPlaneResolutionUnit'))
            imgw = _float_safe(data.get('ExifImageWidth'))
            hfov = calc_hfov(fl35, fl, fpx, fpu, imgw)

            entry = {
                'lat':              lat,
                'lon':              lon,
                'alt':              _float_safe(data.get('GPSAltitude')),
                'direction':        _float_safe(data.get('GPSImgDirection')),
                'pdop':             _float_safe(data.get('GPSDOP')),
                'focal_length':     fl,
                'focal_length_35mm':fl35,
                'hfov':             hfov,
                'make':             data.get('Make', '').strip(),
                'model':            data.get('Model', '').strip(),
                'satellites':       int(float(data.get('GPSSatellites') or 0)),
                'datetime':         None,
                'orientation':      None,
            }
            dt_str = data.get('DateTimeOriginal', '')
            if dt_str:
                for fmt in ('%Y:%m:%d %H:%M:%S', '%Y-%m-%d %H:%M:%S'):
                    try:
                        entry['datetime'] = datetime.strptime(dt_str, fmt)
                        break
                    except ValueError:
                        pass
            results[src_file] = entry

        return results

    except Exception:
        return {}


def read_exif_gps(image_path):
    """
    Read GPS + camera metadata from image.
    Usa sempre pure Python per velocita' massima nel caricamento.
    ExifTool viene usato solo per scrittura e geotag GPX.

    Keys: lat, lon, alt, direction, pdop,
          focal_length, focal_length_35mm, hfov,
          datetime, make, model, orientation
    """
    # Pure Python prima (veloce, zero subprocess)
    if HAS_PIEXIF:
        result = _read_piexif(image_path)
        if result is not None:
            return result
    return _read_pure(image_path)


def get_image_datetime(image_path):
    """Return DateTimeOriginal as datetime, or None.
    Usa pure Python — nessun subprocess.
    """
    # Prova prima con piexif
    if HAS_PIEXIF:
        try:
            exif = piexif.load(image_path)
            dt_raw = exif.get('Exif', {}).get(piexif.ExifIFD.DateTimeOriginal)
            if dt_raw:
                return datetime.strptime(dt_raw.decode(), '%Y:%m:%d %H:%M:%S')
        except Exception:
            pass
    # Fallback pure Python
    raw = read_exif_pure(image_path)
    dt_str = raw.get('datetime_original')
    if dt_str:
        for fmt in ('%Y:%m:%d %H:%M:%S', '%Y-%m-%d %H:%M:%S'):
            try:
                return datetime.strptime(dt_str, fmt)
            except ValueError:
                pass
    return None


# ---------------------------------------------------------------------------
#  Engine implementations — READ
# ---------------------------------------------------------------------------

def _read_exiftool(exiftool, image_path):
    """Read via exiftool -csv (handles all formats, all tag types)."""
    try:
        r = _run_et(exiftool, [
            '-csv', '-n',
            '-GPSLatitude#', '-GPSLongitude#', '-GPSAltitude#',
            '-GPSImgDirection#', '-GPSDOP#',
            '-FocalLength#', '-FocalLengthIn35mmFormat#',
            '-FocalPlaneXResolution#', '-FocalPlaneResolutionUnit#',
            '-ExifImageWidth#',
            '-DateTimeOriginal',
            '-Make', '-Model',
            image_path
        ], timeout=12)

        lines = [l for l in r.stdout.splitlines() if l.strip()]
        if len(lines) >= 2:
            headers = [h.strip() for h in lines[0].split(',')]
            values  = [v.strip().strip('"') for v in lines[1].split(',')]
            data    = dict(zip(headers, values))

            lat = _float_safe(data.get('GPSLatitude'))
            lon = _float_safe(data.get('GPSLongitude'))
            if lat is None or lon is None:
                return None

            fl    = _float_safe(data.get('FocalLength'))
            fl35  = _float_safe(data.get('FocalLengthIn35mmFormat'))
            fpx   = _float_safe(data.get('FocalPlaneXResolution'))
            fpu   = _float_safe(data.get('FocalPlaneResolutionUnit'))
            imgw  = _float_safe(data.get('ExifImageWidth'))
            hfov  = calc_hfov(fl35, fl, fpx, fpu, imgw)

            result = {
                'lat':              lat,
                'lon':              lon,
                'alt':              _float_safe(data.get('GPSAltitude')),
                'direction':        _float_safe(data.get('GPSImgDirection')),
                'pdop':             _float_safe(data.get('GPSDOP')),
                'focal_length':     fl,
                'focal_length_35mm':fl35,
                'hfov':             hfov,
                'make':             data.get('Make', '').strip(),
                'model':            data.get('Model', '').strip(),
                'satellites':       int(float(data.get('GPSSatellites') or 0)),
                'datetime':         None,
                'orientation':      None,
            }
            dt_str = data.get('DateTimeOriginal', '')
            for fmt in ('%Y:%m:%d %H:%M:%S', '%Y-%m-%d %H:%M:%S'):
                try:
                    result['datetime'] = datetime.strptime(dt_str, fmt)
                    break
                except ValueError:
                    pass
            return result

    except Exception:
        pass
    return None


def _read_piexif(image_path):
    """Read via piexif (JPEG only)."""
    try:
        exif = piexif.load(image_path)
        gps  = exif.get('GPS', {})

        # Diagnostic: log GPS IFD raw keys
        try:
            from qgis.core import QgsMessageLog, Qgis
            _keys = {k: repr(v)[:80] for k, v in gps.items()}
            QgsMessageLog.logMessage(
                f"GeotagManager GPS raw [{_os.path.basename(image_path)}]: {_keys}",
                "GeotagManager", Qgis.Info
            )
        except Exception:
            pass

        def _rat(raw):
            """Convert piexif rational to float.
            Handles both formats:
              - ((num, den),)  -- standard piexif wrapping
              - (num, den)     -- direct tuple (some writers)
            """
            if raw is None:
                return None
            # Direct tuple (num, den) without outer wrapper
            if isinstance(raw, tuple) and len(raw) == 2 and not isinstance(raw[0], tuple):
                num, den = raw
                return num / den if den else None
            # Wrapped: ((num, den),) or ((num, den), ...)
            if hasattr(raw, "__len__") and len(raw) >= 1:
                v = raw[0]
                if isinstance(v, tuple) and len(v) == 2 and v[1]:
                    return v[0] / v[1]
                if isinstance(v, (int, float)):
                    return float(v)
            return None


        def _dms(raw, ref_raw):
            if not raw or len(raw) < 3:
                return None
            def r(x):
                return x[0]/x[1] if isinstance(x, tuple) and x[1] else float(x)
            deg = r(raw[0]) + r(raw[1])/60 + r(raw[2])/3600
            ref = (ref_raw[0] if ref_raw else b'N')
            if isinstance(ref, int): ref = bytes([ref])
            if ref.upper() in (b'S', b'W'):
                deg = -deg
            return round(deg, 7)

        lat = _dms(gps.get(piexif.GPSIFD.GPSLatitude),
                   gps.get(piexif.GPSIFD.GPSLatitudeRef))
        lon = _dms(gps.get(piexif.GPSIFD.GPSLongitude),
                   gps.get(piexif.GPSIFD.GPSLongitudeRef))
        # Do NOT return None here — continue to read alt, direction,
        # camera, datetime etc. even when GPS coords are missing
        alt_raw = gps.get(piexif.GPSIFD.GPSAltitude)
        alt = _rat(alt_raw)
        if alt is not None and (gps.get(piexif.GPSIFD.GPSAltitudeRef) == 1):
            alt = -alt

        dir_raw = gps.get(piexif.GPSIFD.GPSImgDirection)
        dop_raw = gps.get(piexif.GPSIFD.GPSDOP)

        exif_ifd = exif.get('Exif', {})

        def _rat_exif(raw):
            if raw and isinstance(raw, tuple) and raw[1]:
                return round(raw[0] / raw[1], 4)
            return None

        fl_raw   = exif_ifd.get(piexif.ExifIFD.FocalLength)
        fl35_raw = exif_ifd.get(piexif.ExifIFD.FocalLengthIn35mmFilm)
        fpx_raw  = exif_ifd.get(piexif.ExifIFD.FocalPlaneXResolution)
        fpu_raw  = exif_ifd.get(piexif.ExifIFD.FocalPlaneResolutionUnit)
        imgw_raw = exif_ifd.get(piexif.ExifIFD.PixelXDimension)

        fl   = _rat_exif(fl_raw)
        fl35 = float(fl35_raw) if fl35_raw else None
        fpx  = (fpx_raw[0]/fpx_raw[1] if isinstance(fpx_raw,tuple) and fpx_raw[1] else None)
        fpu  = float(fpu_raw) if fpu_raw else None
        imgw = float(imgw_raw) if imgw_raw else None
        hfov = calc_hfov(fl35, fl, fpx, fpu, imgw)

        ifd0 = exif.get('0th', {})
        def _str(raw):
            if raw and isinstance(raw, (bytes, bytearray)):
                return raw.rstrip(b'\x00').decode('utf-8', errors='replace').strip()
            return ''

        result = {
            'lat':              lat,
            'lon':              lon,
            'alt':              alt,
            'direction':        _rat(dir_raw),
            'pdop':             _rat(dop_raw),
            'focal_length':     fl,
            'focal_length_35mm':fl35,
            'hfov':             hfov,
            'make':             _str(ifd0.get(piexif.ImageIFD.Make)),
            'model':            _str(ifd0.get(piexif.ImageIFD.Model)),
            'satellites':       int(gps.get(piexif.GPSIFD.GPSSatellites) or 0),
            'orientation':      ifd0.get(piexif.ImageIFD.Orientation),
            'datetime':         None,
        }
        dt_raw = exif_ifd.get(piexif.ExifIFD.DateTimeOriginal)
        if dt_raw:
            try:
                result['datetime'] = datetime.strptime(
                    dt_raw.decode(), '%Y:%m:%d %H:%M:%S')
            except Exception:
                pass
        return result

    except Exception:
        return None


def _read_pure(image_path):
    """Read via pure Python reader (always available)."""
    raw = read_exif_pure(image_path)
    if not raw.get('gps_lat') or not raw.get('gps_lon'):
        return None

    fl   = raw.get('focal_length')
    fl35 = raw.get('focal_length_35mm')
    fpx  = raw.get('focal_plane_x_res')
    fpu  = raw.get('focal_plane_res_unit')
    imgw = raw.get('pixel_x_dim')
    hfov = calc_hfov(fl35, fl, fpx, fpu, imgw)

    result = {
        'lat':              raw['gps_lat'],
        'lon':              raw['gps_lon'],
        'alt':              raw.get('gps_alt'),
        'direction':        raw.get('gps_img_direction'),
        'pdop':             raw.get('gps_dop'),
        'focal_length':     fl,
        'focal_length_35mm':fl35,
        'hfov':             hfov,
        'make':             raw.get('make', ''),
        'model':            raw.get('model', ''),
        'satellites':       0,
        'orientation':      raw.get('orientation'),
        'datetime':         None,
    }
    dt_str = raw.get('datetime_original')
    if dt_str:
        for fmt in ('%Y:%m:%d %H:%M:%S', '%Y-%m-%d %H:%M:%S'):
            try:
                result['datetime'] = datetime.strptime(dt_str, fmt)
                break
            except ValueError:
                pass
    return result


# ---------------------------------------------------------------------------
#  Public write API
# ---------------------------------------------------------------------------

def write_exif_gps(image_path, lat, lon, alt=None, dt=None,
                   direction=None, author=None):
    """
    Write GPS coordinates (and optionally direction/author) to image EXIF.
    author is written to the EXIF Artist tag.
    Returns True on success, raises RuntimeError if no write engine available.
    """
    et = find_exiftool()
    if et:
        return _write_exiftool(et, image_path, lat, lon, alt, dt,
                               direction, author)
    if HAS_PIEXIF:
        return _write_piexif(image_path, lat, lon, alt, dt,
                             direction, author)
    raise RuntimeError(
        "No write engine available.\n"
        "Use the ExifTool Setup wizard (toolbar → ⚙ ExifTool) to install "
        "the bundled exiftool, or install piexif (pip install piexif)."
    )


def _write_exiftool(exiftool, image_path, lat, lon, alt, dt,
                    direction=None, author=None):
    try:
        args = [
            exiftool, '-overwrite_original',
            f'-GPSLatitude={abs(lat)}',
            f'-GPSLatitudeRef={"N" if lat >= 0 else "S"}',
            f'-GPSLongitude={abs(lon)}',
            f'-GPSLongitudeRef={"E" if lon >= 0 else "W"}',
        ]
        if alt is not None:
            args += [f'-GPSAltitude={alt}', '-GPSAltitudeRef=0']
        if direction is not None:
            args += [f'-GPSImgDirection={direction}', '-GPSImgDirectionRef=T']
        if dt is not None:
            args.append(f'-DateTimeOriginal={dt.strftime("%Y:%m:%d %H:%M:%S")}')
        if author:
            args += [f'-Artist={author}', f'-XMP:Creator={author}']
        args.append(image_path)
        r = subprocess.run(args, capture_output=True, text=True, timeout=15)
        return r.returncode == 0
    except Exception:
        return False


def _write_piexif(image_path, lat, lon, alt, dt,
                  direction=None, author=None):
    try:
        def _deg_to_rat(v):
            d = int(abs(v))
            mf = (abs(v)-d)*60; m = int(mf)
            s = (mf-m)*60
            return ((d,1),(m,1),(int(s*100),100))
        try:
            ed = piexif.load(image_path)
        except Exception:
            ed = {'0th':{}, 'Exif':{}, 'GPS':{}, '1st':{}}
        gps = {
            piexif.GPSIFD.GPSLatitudeRef:  b'N' if lat>=0 else b'S',
            piexif.GPSIFD.GPSLatitude:     _deg_to_rat(lat),
            piexif.GPSIFD.GPSLongitudeRef: b'E' if lon>=0 else b'W',
            piexif.GPSIFD.GPSLongitude:    _deg_to_rat(lon),
        }
        if alt is not None:
            gps[piexif.GPSIFD.GPSAltitude]    = (int(abs(alt)*100), 100)
            gps[piexif.GPSIFD.GPSAltitudeRef] = 0
        if direction is not None:
            # Store as rational (degrees * 100 / 100)
            gps[piexif.GPSIFD.GPSImgDirection]    = (int(direction * 100), 100)
            gps[piexif.GPSIFD.GPSImgDirectionRef] = b'T'  # True North
        ed['GPS'] = gps
        if dt is not None:
            ed['Exif'][piexif.ExifIFD.DateTimeOriginal] = \
                dt.strftime('%Y:%m:%d %H:%M:%S').encode()
        if author:
            # Artist tag in IFD0 (tag 0x013B = 315)
            ed['0th'][piexif.ImageIFD.Artist] = author.encode('utf-8')
        piexif.insert(piexif.dump(ed), image_path)
        return True
    except Exception:
        return False

def write_exif_author(image_path, author):
    """Write author to EXIF Artist and XMP:Creator tags only.
    Does not modify GPS or other tags.
    Returns True on success, False on failure.
    """
    if not author:
        return False
    et = find_exiftool()
    if et:
        try:
            r = subprocess.run(
                [et, '-overwrite_original',
                 f'-Artist={author}',
                 f'-XMP:Creator={author}',
                 image_path],
                capture_output=True, text=True, timeout=15
            )
            return r.returncode == 0
        except Exception:
            return False
    if HAS_PIEXIF:
        try:
            try:
                ed = piexif.load(image_path)
            except Exception:
                ed = {'0th': {}, 'Exif': {}, 'GPS': {}, '1st': {}}
            ed['0th'][piexif.ImageIFD.Artist] = author.encode('utf-8')
            piexif.insert(piexif.dump(ed), image_path)
            return True
        except Exception:
            return False
    return False

def write_exif_author_batch(author_map: dict,
                            cancel_flag=None,
                            chunk_size: int = 500) -> tuple:
    """Write author to EXIF Artist/XMP:Creator for multiple files at once.

    author_map:  {filepath: author_string}
    cancel_flag: optional list; stop if cancel_flag[0] is True
    chunk_size:  max files per ExifTool call (avoids Windows CLI length limit)

    Uses -@ argfile to avoid command-line length limits on Windows.
    Returns (ok_count, fail_count).
    """
    import os, tempfile
    if not author_map:
        return 0, 0

    ok = fail = 0

    from collections import defaultdict
    by_author = defaultdict(list)
    for fp, author in author_map.items():
        if author:
            by_author[author].append(fp)

    et = find_exiftool()

    if et:
        for author, paths in by_author.items():
            if cancel_flag and cancel_flag[0]:
                break
            # Split into chunks to avoid Windows CLI length limits
            for i in range(0, len(paths), chunk_size):
                if cancel_flag and cancel_flag[0]:
                    break
                chunk = paths[i:i + chunk_size]
                # Write paths to a temp argfile
                tmp = None
                try:
                    with tempfile.NamedTemporaryFile(
                        mode='w', suffix='.txt',
                        delete=False, encoding='utf-8'
                    ) as tf:
                        for fp in chunk:
                            tf.write(fp + '\n')
                        tmp = tf.name
                    args = [
                        et, '-overwrite_original',
                        f'-Artist={author}',
                        f'-XMP:Creator={author}',
                        '-@', tmp
                    ]
                    r = subprocess.run(
                        args,
                        capture_output=True,
                        text=True,
                        timeout=None  # no timeout — large batches may take minutes
                    )
                    if r.returncode == 0:
                        ok += len(chunk)
                    else:
                        fail += len(chunk)
                except Exception:
                    fail += len(chunk)
                finally:
                    if tmp and os.path.exists(tmp):
                        try:
                            os.unlink(tmp)
                        except Exception:
                            pass
        return ok, fail

    if HAS_PIEXIF:
        for author, paths in by_author.items():
            if cancel_flag and cancel_flag[0]:
                break
            for fp in paths:
                if cancel_flag and cancel_flag[0]:
                    break
                try:
                    try:
                        ed = piexif.load(fp)
                    except Exception:
                        ed = {'0th': {}, 'Exif': {}, 'GPS': {}, '1st': {}}
                    ed['0th'][piexif.ImageIFD.Artist] = author.encode('utf-8')
                    piexif.insert(piexif.dump(ed), fp)
                    ok += 1
                except Exception:
                    fail += 1
        return ok, fail

    return 0, len(author_map)
