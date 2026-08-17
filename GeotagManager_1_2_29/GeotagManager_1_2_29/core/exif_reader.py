# -*- coding: utf-8 -*-
"""
exif_reader.py  —  Pure-Python EXIF/GPS metadata reader.
Zero external dependencies. Reads directly from JPEG/TIFF binary.

Supported tags:
  GPS:  Latitude, Longitude, Altitude, ImgDirection, DOP
  EXIF: DateTimeOriginal, FocalLength, FocalLengthIn35mmFilm,
        FocalPlaneXResolution, FocalPlaneResolutionUnit, PixelXDimension
  IFD0: Make, Model, Orientation

Supports JPEG (APP1/EXIF), TIFF, and any JPEG-derivative (HEIC is not
supported without a dedicated parser — falls back gracefully to None).
"""

import struct
import os
from datetime import datetime


# ---------------------------------------------------------------------------
#  IFD tag numbers we care about
# ---------------------------------------------------------------------------

# IFD0 / IFD1
TAG_MAKE                    = 0x010F
TAG_MODEL                   = 0x0110
TAG_ORIENTATION             = 0x0112
TAG_EXIF_IFD                = 0x8769   # pointer to Exif sub-IFD

# Exif sub-IFD
TAG_DATETIME_ORIGINAL       = 0x9003
TAG_FOCAL_LENGTH            = 0x920A
TAG_FOCAL_PLANE_X_RES       = 0xA20E
TAG_FOCAL_PLANE_RES_UNIT    = 0xA210
TAG_FOCAL_LENGTH_35MM       = 0xA405
TAG_PIXEL_X_DIM             = 0xA002   # ExifImageWidth

# GPS IFD pointer (in IFD0)
TAG_GPS_IFD                 = 0x8825

# GPS sub-IFD tags
TAG_GPS_LAT_REF             = 0x0001
TAG_GPS_LAT                 = 0x0002
TAG_GPS_LON_REF             = 0x0003
TAG_GPS_LON                 = 0x0004
TAG_GPS_ALT_REF             = 0x0005
TAG_GPS_ALT                 = 0x0006
TAG_GPS_DOP                 = 0x000B
TAG_GPS_IMG_DIR_REF         = 0x0010
TAG_GPS_IMG_DIR             = 0x0011

# TIFF type sizes (bytes)
TYPE_SIZES = {1:1, 2:1, 3:2, 4:4, 5:8, 6:1, 7:1, 8:2, 9:4, 10:8, 11:4, 12:8}


# ---------------------------------------------------------------------------
#  Low-level TIFF/IFD parser
# ---------------------------------------------------------------------------

class _TiffParser:
    """Parse a TIFF byte stream (or EXIF APP1 block inside a JPEG)."""

    def __init__(self, data, offset=0):
        self.data   = data
        self.base   = offset        # offset of TIFF header within data
        self.endian = '>'           # default big-endian, overridden in _parse_header

    def _parse_header(self):
        hdr = self.data[self.base:self.base+8]
        if hdr[:2] == b'II':
            self.endian = '<'
        elif hdr[:2] == b'MM':
            self.endian = '>'
        else:
            raise ValueError("Not a TIFF header")
        magic = self._u16(self.base+2)
        if magic not in (42, 43):
            raise ValueError(f"Bad TIFF magic: {magic}")
        ifd0_off = self._u32(self.base+4)
        return ifd0_off

    def _u8(self, pos):
        return self.data[pos]

    def _u16(self, pos):
        return struct.unpack_from(self.endian+'H', self.data, pos)[0]

    def _u32(self, pos):
        return struct.unpack_from(self.endian+'I', self.data, pos)[0]

    def _s32(self, pos):
        return struct.unpack_from(self.endian+'i', self.data, pos)[0]

    def _rational(self, pos):
        n = struct.unpack_from(self.endian+'I', self.data, pos)[0]
        d = struct.unpack_from(self.endian+'I', self.data, pos+4)[0]
        return (n, d)

    def _srational(self, pos):
        n = struct.unpack_from(self.endian+'i', self.data, pos)[0]
        d = struct.unpack_from(self.endian+'i', self.data, pos+4)[0]
        return (n, d)

    def _rational_val(self, pos):
        n, d = self._rational(pos)
        return n / d if d else 0.0

    def _read_value(self, tag_type, count, data_or_offset_pos, is_offset):
        """Read tag value(s). Returns list of raw values."""
        type_size = TYPE_SIZES.get(tag_type, 1)
        total     = type_size * count

        if is_offset:
            off = self.base + self._u32(data_or_offset_pos)
        else:
            off = data_or_offset_pos

        values = []
        for i in range(count):
            p = off + i * type_size
            if tag_type == 1:   # BYTE
                values.append(self._u8(p))
            elif tag_type == 2: # ASCII
                # Read all bytes, handle as single string later
                values.append(self.data[off:off+count])
                break
            elif tag_type == 3: # SHORT
                values.append(self._u16(p))
            elif tag_type == 4: # LONG
                values.append(self._u32(p))
            elif tag_type == 5: # RATIONAL
                values.append(self._rational(p))
            elif tag_type == 7: # UNDEFINED
                values.append(self.data[off:off+count])
                break
            elif tag_type == 9: # SLONG
                values.append(self._s32(p))
            elif tag_type == 10:# SRATIONAL
                values.append(self._srational(p))
            elif tag_type == 11:# FLOAT
                values.append(struct.unpack_from(self.endian+'f', self.data, p)[0])
            elif tag_type == 12:# DOUBLE
                values.append(struct.unpack_from(self.endian+'d', self.data, p)[0])
        return values

    def _parse_ifd(self, ifd_abs_offset):
        """Parse one IFD, return dict {tag_id: values_list}."""
        tags = {}
        try:
            n = self._u16(ifd_abs_offset)
        except Exception:
            return tags

        for i in range(n):
            entry = ifd_abs_offset + 2 + i * 12
            try:
                tag_id    = self._u16(entry)
                tag_type  = self._u16(entry + 2)
                count     = self._u32(entry + 4)
                type_size = TYPE_SIZES.get(tag_type, 1)
                is_offset = (type_size * count) > 4
                vals = self._read_value(tag_type, count, entry+8, is_offset)
                tags[tag_id] = vals
            except Exception:
                continue
        return tags

    def parse(self):
        """
        Parse TIFF/EXIF and return a flat dict of named metadata fields.
        Returns {} on any failure.
        """
        try:
            ifd0_rel = self._parse_header()
        except Exception:
            return {}

        result = {}

        # ---- IFD0 -------------------------------------------------------
        ifd0_abs = self.base + ifd0_rel
        ifd0 = self._parse_ifd(ifd0_abs)

        result['make']        = self._ascii(ifd0.get(TAG_MAKE))
        result['model']       = self._ascii(ifd0.get(TAG_MODEL))
        result['orientation'] = self._first_int(ifd0.get(TAG_ORIENTATION))

        # ---- Exif sub-IFD -----------------------------------------------
        exif_ptr = self._first_int(ifd0.get(TAG_EXIF_IFD))
        if exif_ptr:
            exif = self._parse_ifd(self.base + exif_ptr)

            result['datetime_original'] = self._ascii(
                exif.get(TAG_DATETIME_ORIGINAL))

            fl_raw = exif.get(TAG_FOCAL_LENGTH)
            result['focal_length'] = self._rat_float(fl_raw)

            fl35_raw = exif.get(TAG_FOCAL_LENGTH_35MM)
            result['focal_length_35mm'] = self._first_float(fl35_raw)

            fpx_raw = exif.get(TAG_FOCAL_PLANE_X_RES)
            result['focal_plane_x_res'] = self._rat_float(fpx_raw)

            fpu_raw = exif.get(TAG_FOCAL_PLANE_RES_UNIT)
            result['focal_plane_res_unit'] = self._first_int(fpu_raw)

            pxw_raw = exif.get(TAG_PIXEL_X_DIM)
            result['pixel_x_dim'] = self._first_int(pxw_raw)

        # ---- GPS sub-IFD ------------------------------------------------
        gps_ptr = self._first_int(ifd0.get(TAG_GPS_IFD))
        if gps_ptr:
            gps = self._parse_ifd(self.base + gps_ptr)

            lat = self._dms_to_deg(gps.get(TAG_GPS_LAT),
                                   self._ascii(gps.get(TAG_GPS_LAT_REF)))
            lon = self._dms_to_deg(gps.get(TAG_GPS_LON),
                                   self._ascii(gps.get(TAG_GPS_LON_REF)))
            result['gps_lat'] = lat
            result['gps_lon'] = lon

            alt_raw = gps.get(TAG_GPS_ALT)
            alt_ref = self._first_int(gps.get(TAG_GPS_ALT_REF)) or 0
            alt     = self._rat_float(alt_raw)
            if alt is not None and alt_ref == 1:
                alt = -alt
            result['gps_alt'] = alt

            dop_raw = gps.get(TAG_GPS_DOP)
            result['gps_dop'] = self._rat_float(dop_raw)

            dir_raw = gps.get(TAG_GPS_IMG_DIR)
            result['gps_img_direction'] = self._rat_float(dir_raw)

        return result

    # ---- helpers ---------------------------------------------------------

    def _ascii(self, vals):
        if not vals:
            return None
        raw = vals[0]
        if isinstance(raw, (bytes, bytearray)):
            return raw.rstrip(b'\x00').decode('utf-8', errors='replace').strip()
        if isinstance(raw, str):
            return raw.strip()
        return None

    def _first_int(self, vals):
        if not vals:
            return None
        v = vals[0]
        if isinstance(v, int):
            return v
        return None

    def _first_float(self, vals):
        if not vals:
            return None
        v = vals[0]
        try:
            return float(v)
        except Exception:
            return None

    def _rat_float(self, vals):
        if not vals:
            return None
        v = vals[0]
        if isinstance(v, tuple) and len(v) == 2:
            n, d = v
            return round(n / d, 6) if d else None
        try:
            return float(v)
        except Exception:
            return None

    def _dms_to_deg(self, dms_vals, ref):
        if not dms_vals or len(dms_vals) < 3:
            return None
        try:
            d = dms_vals[0]; d = d[0]/d[1] if isinstance(d,tuple) else float(d)
            m = dms_vals[1]; m = m[0]/m[1] if isinstance(m,tuple) else float(m)
            s = dms_vals[2]; s = s[0]/s[1] if isinstance(s,tuple) else float(s)
            deg = d + m/60.0 + s/3600.0
            if ref and ref.upper() in ('S','W'):
                deg = -deg
            return round(deg, 7)
        except Exception:
            return None


# ---------------------------------------------------------------------------
#  JPEG scanner — finds APP1/EXIF marker
# ---------------------------------------------------------------------------

def _find_exif_in_jpeg(data):
    """
    Scan JPEG markers to find APP1 with EXIF data.
    Returns (exif_data_bytes, tiff_offset_within_exif_data) or (None, None).
    """
    if data[:2] != b'\xff\xd8':
        return None, None

    i = 2
    while i < len(data) - 4:
        if data[i] != 0xFF:
            break
        marker = data[i+1]
        if marker == 0xE1:                          # APP1
            seg_len = struct.unpack('>H', data[i+2:i+4])[0]
            seg_data = data[i+4 : i+2+seg_len]
            if seg_data[:6] == b'Exif\x00\x00':    # EXIF APP1
                return seg_data, 6                  # TIFF starts at offset 6
        # Advance past marker
        if marker in (0xD8, 0xD9):                  # SOI, EOI (no length)
            i += 2
        else:
            seg_len = struct.unpack('>H', data[i+2:i+4])[0]
            i += 2 + seg_len

    return None, None


# ---------------------------------------------------------------------------
#  Public API
# ---------------------------------------------------------------------------

def read_exif_pure(filepath):
    """
    Read EXIF metadata from filepath using pure Python (no dependencies).

    Returns dict with keys:
        make, model, orientation,
        datetime_original,
        focal_length, focal_length_35mm,
        focal_plane_x_res, focal_plane_res_unit, pixel_x_dim,
        gps_lat, gps_lon, gps_alt, gps_dop, gps_img_direction

    Any field not found is None.
    Returns {} on unreadable files.
    """
    try:
        # Read enough bytes: 512 KB covers all EXIF in any typical camera file
        with open(filepath, 'rb') as f:
            header = f.read(4)
            f.seek(0)
            data = f.read(524288)   # 512 KB

        ext = os.path.splitext(filepath)[1].lower()

        if header[:2] == b'\xff\xd8':               # JPEG
            seg_data, tiff_off = _find_exif_in_jpeg(data)
            if seg_data is None:
                return {}
            parser = _TiffParser(seg_data, offset=tiff_off)

        elif header[:4] in (b'II\x2a\x00', b'MM\x00\x2a'):  # TIFF
            parser = _TiffParser(data, offset=0)

        else:
            return {}                                # unsupported format

        return parser.parse()

    except Exception:
        return {}


def calc_hfov(fl_35mm=None, fl_real=None,
              fp_xres=None, fp_unit=None, img_w=None):
    """
    Calculate horizontal field of view in degrees.

    Priority:
      1. FocalLengthIn35mmFilm → HFOV = 2·atan(36 / (2·fl_35))
      2. FocalLength + sensor width from FocalPlane tags
         unit_to_mm: 2=inch(25.4), 3=cm(10), 4=mm(1), 5=µm(0.001)

    Returns float or None.
    """
    import math
    if fl_35mm and fl_35mm > 0:
        return round(2 * math.degrees(math.atan(36.0 / (2 * fl_35mm))), 2)
    unit_mm = {2: 25.4, 3: 10.0, 4: 1.0, 5: 0.001}
    if fl_real and fl_real > 0 and fp_xres and fp_xres > 0 and img_w and fp_unit:
        mm_per_px = unit_mm.get(int(fp_unit), 25.4) / fp_xres
        sensor_w  = img_w * mm_per_px
        if sensor_w > 0:
            return round(2 * math.degrees(math.atan(sensor_w / (2 * fl_real))), 2)
    return None
