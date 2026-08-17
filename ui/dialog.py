# -*- coding: utf-8 -*-
"""
dialog.py - GeotagManager main dialog (English UI).

Layout:
  ┌──────────────────────────────────────────────────────────────────┐
  │  Toolbar: Load Folder | Load GPX | Export                         │
  ├──────────────┬───────────────────────────────────────────────────┤
  │  Photo List  │  Photo Preview (large)   │  EXIF Info panel       │
  │              ├──────────────────────────┴────────────────────────┤
  │              │  Overview minimap + controls                       │
  ├──────────────┴───────────────────────────────────────────────────┤
  │  Controls bar: offset | author | options                          │
  ├──────────────────────────────────────────────────────────────────┤
  │  Status bar                                                       │
  └──────────────────────────────────────────────────────────────────┘
"""

import os
import struct
from datetime import datetime

from qgis.PyQt.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QSplitter,
    QListWidget, QListWidgetItem, QPushButton, QLabel,
    QLineEdit, QSpinBox, QDoubleSpinBox, QComboBox,
    QFileDialog, QProgressBar, QToolBar, QAction,
    QGroupBox, QWidget, QMessageBox,
    QStatusBar, QCheckBox, QFrame, QApplication,
)
from qgis.PyQt.QtCore import Qt, QThread, QSize, QObject, pyqtSlot, pyqtSignal
from qgis.PyQt.QtGui import QPixmap, QIcon, QColor, QTransform

from qgis.core import (
    QgsProject, QgsRectangle, QgsCoordinateReferenceSystem,
    QgsCoordinateTransform, QgsPointXY, QgsWkbTypes,
    QgsMapLayer, QgsVectorLayer, QgsFeatureRequest,
    QgsGeometry, QgsAction, QgsMessageLog, Qgis,
)
from qgis.gui import QgsMapCanvas, QgsRubberBand, QgsScaleWidget

from ..core.exif_handler import (
    read_exif_gps, write_exif_gps, get_image_datetime,
    find_exiftool, HAS_PIEXIF,
)
from ..core.gpx_interpolator import parse_gpx
from ..core.geopackage_exporter import export_to_geopackage
from ..core.batch_worker import list_images, BatchWorker
from ..core.gpx_tagger   import GeotagWorker
from ..core.map_tool import GeotagPointTool, DirectionTool


THUMB_SIZE = 64
PREVIEW_W  = 500
PREVIEW_H  = 375


class PhotoItem:
    """Data container for one photo entry."""
    def __init__(self, filepath):
        self.filepath = filepath
        self.filename = os.path.basename(filepath)
        self.datetime = None
        self.lat       = None
        self.lon       = None
        self.alt       = None
        self.direction        = None  # GPSImgDirection (degrees 0-360)
        self.pdop             = None  # GPS DOP
        self.focal_length     = None  # mm (real)
        self.focal_length_35mm= None  # mm (35mm equiv)
        self.hfov             = None  # horizontal FOV degrees
        self.make             = ""    # camera make
        self.model            = ""    # camera model
        self.satellites       = 0     # GPS satellites
        self.source           = "pending"
        self.notes     = ""
        self.author    = ""   # assigned via AuthorDialog
        self.list_item = None

    @property
    def has_position(self):
        return self.lat is not None and self.lon is not None

    def to_record(self):
        return {
            "filepath":    self.filepath,
            "lat":         self.lat or 0.0,
            "lon":         self.lon or 0.0,
            "alt":         self.alt,
            "datetime":    self.datetime,
            "source":      self.source,
            "author":      self.author,
            "notes":       self.notes,
            "_exif_extra": {
                "alt":              self.alt,
                "direction":        self.direction,
                "pdop":             self.pdop,
                "focal_length":     self.focal_length,
                "focal_length_35mm":self.focal_length_35mm,
                "hfov":             self.hfov,
                "make":             self.make,
                "model":            self.model,
                "satellites":       self.satellites,
            },
        }


class GeotagManagerDialog(QDialog):


    @staticmethod
    def _has_write_engine():
        """True if at least one EXIF write engine is available."""
        return bool(find_exiftool()) or HAS_PIEXIF

    @staticmethod
    def _write_engine_tooltip():
        """Return tooltip explaining why write controls are disabled."""
        if GeotagManagerDialog._has_write_engine():
            return ""
        return (
            "EXIF write unavailable — no write engine found.\n"
            "Install ExifTool via ⚙ ExifTool in the toolbar, "
            "or run: pip install piexif"
        )

    def __init__(self, iface, parent=None):
        super().__init__(parent)
        self.iface       = iface
        self.main_canvas = iface.mapCanvas()

        self.setWindowTitle("GeotagManager_1_2_29 v1.2.29")
        # Independent top-level window — minimizes to OS taskbar
        # (same pattern as LidarManager: parent=None + Qt.Window)
        self.setWindowFlags(
            Qt.Window |
            Qt.WindowStaysOnTopHint |
            Qt.WindowMinimizeButtonHint |
            Qt.WindowCloseButtonHint
        )
        # Use QGIS icon for taskbar grouping on Windows
        from qgis.PyQt.QtWidgets import QApplication
        self.setWindowIcon(QApplication.windowIcon())
        self.setMinimumSize(960, 660)
        self.resize(1180, 760)

        self.photo_items     = []
        self.track_points    = []      # merged sorted track from all loaded GPX
        self._gpx_paths      = []      # list of loaded GPX file paths
        self._photo_folder   = ""       # root folder of loaded photos
        self._worker         = None
        self._worker_thread  = None
        self._point_tool     = None
        self._prev_map_tool  = None
        self._rubber_bands      = {}    # filepath -> QgsRubberBand on main_canvas
        self._selected_item     = None
        self._listened_layer    = None  # QgsVectorLayer being watched for selection
        self._path_field        = None  # nome campo filepath rilevato al connect
        self._highlight_rb      = None  # selection highlight rubber band
        self._log_panel         = None  # LogPanel instance
        self._scan_dialog       = None  # ScanDialog instance

        self._build_ui()
        self._connect_signals()

    # ------------------------------------------------------------------ #
    #  UI construction                                                     #
    # ------------------------------------------------------------------ #

    def _build_ui(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(4, 4, 4, 4)
        root.setSpacing(4)

        root.addWidget(self._build_toolbar())

        h_split = QSplitter(Qt.Horizontal)
        h_split.addWidget(self._build_left_panel())
        h_split.addWidget(self._build_right_panel())
        h_split.setSizes([290, 870])
        root.addWidget(h_split, stretch=1)

        root.addWidget(self._build_controls_bar())

        self.status_bar = QStatusBar()
        self.status_bar.setFixedHeight(22)
        self.progress_bar = QProgressBar()
        self.progress_bar.setFixedWidth(200)
        self.progress_bar.setVisible(False)
        self.status_bar.addPermanentWidget(self.progress_bar)
        # Permanent widget always showing the active EXIF engine
        self.lbl_engine = QLabel("EXIF: —")
        self.lbl_engine.setStyleSheet(
            "font-size:10px; color:#555; padding: 0 6px;"
        )
        self.lbl_engine.setToolTip(
            "Motore EXIF attivo.\n"
            "Usa ⚙ ExifTool per installare ExifTool bundled."
        )
        self.status_bar.addPermanentWidget(self.lbl_engine)
        root.addWidget(self.status_bar)

        # Initialise engine label and welcome message
        self._update_engine_label()
        self._set_status("Load a photo folder and a GPX track.")

    # ---- Toolbar -------------------------------------------------------

    def _build_toolbar(self):
        tb = QToolBar()
        tb.setIconSize(QSize(20, 20))
        tb.setToolButtonStyle(Qt.ToolButtonTextBesideIcon)

        self.act_load_folder = QAction("📁 Load Photos", self)
        self.act_load_folder.setToolTip("Load image folder (JPG/TIFF)")
        tb.addAction(self.act_load_folder)


        self.act_load_gpx = QAction("🗺 Load GPX", self)
        self.act_load_gpx.setToolTip("Add one or more GPX track files")
        tb.addAction(self.act_load_gpx)

        # act_run kept for internal use; Geotag button moved to GPX Tracks panel
        self.act_run = QAction("▶ Geotag", self)
        self.act_run.setEnabled(False)

        tb.addSeparator()

        self.act_export_gpkg = QAction("💾 Export GeoPackage", self)
        self.act_export_gpkg.setToolTip("Export GPS points to GeoPackage")
        self.act_export_gpkg.setEnabled(False)
        tb.addAction(self.act_export_gpkg)


        tb.addSeparator()
        self.act_clear = QAction("🗑 Clear", self)
        self.act_clear.setToolTip("Remove all photos and track")
        tb.addAction(self.act_clear)

        tb.addSeparator()
        self.act_log = QAction("📋 Log", self)
        self.act_log.setToolTip("Open the GeotagManager log window (mirrors QGIS Message Log)")
        self.act_log.setCheckable(True)
        tb.addAction(self.act_log)

        self.act_exiftool_setup = QAction("⚙ ExifTool", self)
        self.act_exiftool_setup.setToolTip(
            "ExifTool Setup — download and manage the bundled ExifTool binary"
        )
        tb.addAction(self.act_exiftool_setup)

        self.act_batch_scan = QAction("🗂 Batch Scan → GPKG", self)
        self.act_batch_scan.setToolTip(
            "Open Batch Scan window: scan a directory tree for geotagged photos\n"
            "and export directly to GeoPackage / CSV / GeoJSON"
        )
        tb.addAction(self.act_batch_scan)

        tb.addSeparator()

        self.act_help = QAction("❓ Help", self)
        self.act_help.setToolTip("Open the GeotagManager user guide (README.md)")
        self.act_help.triggered.connect(self._open_help)
        tb.addAction(self.act_help)

        self.act_fullscreen_photo = QAction("⛶ Photo", self)
        self.act_fullscreen_photo.setToolTip(
            "Foto a schermo intero: nasconde il pannello sinistro\n"
            "e la barra controlli per massimizzare la preview.\n"
            "Premi di nuovo per ripristinare."
        )
        self.act_fullscreen_photo.setCheckable(True)
        tb.addAction(self.act_fullscreen_photo)

        return tb

    # ---- Left panel: photo list ----------------------------------------

    def _build_left_panel(self):
        panel = QWidget()
        vlay  = QVBoxLayout(panel)
        vlay.setContentsMargins(0, 0, 2, 0)
        vlay.setSpacing(4)

        # ---- Sync from layer selection (top, in QGroupBox) ----
        grp_sync = QGroupBox("Sync from layer selection")
        grp_sync.setStyleSheet("QGroupBox { font-weight:bold; }")
        sync_lay = QVBoxLayout(grp_sync)
        sync_lay.setContentsMargins(6, 8, 6, 6)
        sync_lay.setSpacing(4)

        self.lbl_active_layer = QLabel("Active layer: \u2014")
        self.lbl_active_layer.setStyleSheet("font-size:9px; color:#555;")
        self.lbl_active_layer.setWordWrap(True)
        sync_lay.addWidget(self.lbl_active_layer)

        lst_btn_row = QHBoxLayout()
        self.btn_listen = QPushButton("Connect")
        self.btn_listen.setFixedHeight(24)
        self.btn_listen.setCheckable(True)
        self.btn_listen.setToolTip(
            "Connect to the layer currently selected in the TOC.\n"
            "When a feature is selected in that layer, the matching\n"
            "photo is highlighted, previewed, and zoomed to on the map."
        )
        self.btn_listen.toggled.connect(self._toggle_layer_listener)
        lst_btn_row.addWidget(self.btn_listen)
        self.lbl_listen_status = QLabel("Not connected")
        self.lbl_listen_status.setStyleSheet("font-size:9px; color:#888;")
        self.lbl_listen_status.setWordWrap(True)
        lst_btn_row.addWidget(self.lbl_listen_status)
        sync_lay.addLayout(lst_btn_row)

        vlay.addWidget(grp_sync)

        # ---- Photo list header: label + Clean + Open-folder buttons ----
        photos_hdr = QHBoxLayout()
        photos_hdr.setContentsMargins(0,0,0,0)
        photos_hdr.addWidget(QLabel("<b>Photos</b>"))
        photos_hdr.addStretch()
        self.btn_open_folder = QPushButton("📂 Open folder")
        self.btn_open_folder.setFixedHeight(22)
        self.btn_open_folder.setToolTip("Open the photo folder in the system file manager")
        self.btn_open_folder.setEnabled(False)
        self.btn_open_folder.setStyleSheet("font-size:10px; padding: 0 6px;")
        self.btn_open_folder.clicked.connect(self._open_photo_folder)
        photos_hdr.addWidget(self.btn_open_folder)
        vlay.addLayout(photos_hdr)

        self.photo_list = QListWidget()
        self.photo_list.setUniformItemSizes(True)
        self.photo_list.setSpacing(2)
        self.photo_list.setSelectionMode(QListWidget.ExtendedSelection)
        vlay.addWidget(self.photo_list, stretch=2)

        self.lbl_counts = QLabel("No photos loaded")
        self.lbl_counts.setStyleSheet("color:#555; font-size:10px;")
        vlay.addWidget(self.lbl_counts)

        # ---- GPX track list ----
        sep = QFrame()
        sep.setFrameShape(QFrame.HLine)
        sep.setFrameShadow(QFrame.Sunken)
        vlay.addWidget(sep)

        gpx_hdr = QHBoxLayout()
        gpx_hdr.addWidget(QLabel("<b>GPX Tracks</b>"))
        gpx_hdr.addStretch()
        self.btn_gpx_add = QPushButton("＋ Add")
        self.btn_gpx_add.setFixedHeight(22)
        self.btn_gpx_add.setFixedWidth(56)
        self.btn_gpx_add.setToolTip("Add one or more GPX files")
        gpx_hdr.addWidget(self.btn_gpx_add)
        self.btn_gpx_remove = QPushButton("－ Remove")
        self.btn_gpx_remove.setFixedHeight(22)
        self.btn_gpx_remove.setFixedWidth(68)
        self.btn_gpx_remove.setToolTip("Remove selected GPX file")
        self.btn_gpx_remove.setEnabled(False)
        gpx_hdr.addWidget(self.btn_gpx_remove)
        vlay.addLayout(gpx_hdr)

        self.gpx_list = QListWidget()
        self.gpx_list.setMaximumHeight(120)
        self.gpx_list.setUniformItemSizes(True)
        self.gpx_list.setToolTip("Loaded GPX track files — merged for interpolation")
        vlay.addWidget(self.gpx_list, stretch=1)

        self.lbl_gpx_points = QLabel("Total track points: 0")
        self.lbl_gpx_points.setStyleSheet("color:#555; font-size:10px;")
        vlay.addWidget(self.lbl_gpx_points)

        self.btn_geotag = QPushButton("▶ Geotag photos from GPX")
        self.btn_geotag.setFixedHeight(26)
        self.btn_geotag.setToolTip(
            "Match each photo to the nearest GPX trackpoint by timestamp\n"
            "and assign GPS coordinates. Requires at least one GPX track\n"
            "and ExifTool installed."
        )
        self.btn_geotag.setEnabled(False)
        vlay.addWidget(self.btn_geotag)

        panel.setMinimumWidth(240)
        panel.setMaximumWidth(380)
        return panel

    # ---- Right panel: preview + overview -------------------------------

    def _build_right_panel(self):
        """Right panel: just the preview + info (no overview in 1.0.5)."""
        return self._build_preview_panel()

    def _build_preview_panel(self):
        widget = QWidget()
        hlay   = QHBoxLayout(widget)
        hlay.setContentsMargins(4, 4, 4, 4)
        hlay.setSpacing(8)

        # Large photo preview + navigation bar
        preview_vbox = QVBoxLayout()
        preview_vbox.setContentsMargins(0, 0, 0, 0)
        preview_vbox.setSpacing(4)

        self.photo_preview = QLabel()
        self.photo_preview.setMinimumSize(PREVIEW_W, PREVIEW_H)
        self.photo_preview.setAlignment(Qt.AlignCenter)
        self.photo_preview.setStyleSheet(
            "border:1px solid #999; background:#1c1c1c; color:#777;"
        )
        self.photo_preview.setText("No photo selected")
        preview_vbox.addWidget(self.photo_preview, stretch=1)

        # Navigation bar
        nav_bar = QHBoxLayout()
        nav_bar.setContentsMargins(0, 0, 0, 0)

        self.btn_prev = QPushButton("◀  Prev")
        self.btn_prev.setFixedHeight(28)
        self.btn_prev.setToolTip("Previous photo  [Left arrow]")
        self.btn_prev.setEnabled(False)
        self.btn_prev.clicked.connect(self._go_prev)
        nav_bar.addWidget(self.btn_prev)

        self.lbl_nav_index = QLabel("")
        self.lbl_nav_index.setAlignment(Qt.AlignCenter)
        self.lbl_nav_index.setStyleSheet("color:#aaa; font-size:11px;")
        nav_bar.addWidget(self.lbl_nav_index, stretch=1)

        self.btn_next = QPushButton("Next  ▶")
        self.btn_next.setFixedHeight(28)
        self.btn_next.setToolTip("Next photo  [Right arrow]")
        self.btn_next.setEnabled(False)
        self.btn_next.clicked.connect(self._go_next)
        nav_bar.addWidget(self.btn_next)

        preview_vbox.addLayout(nav_bar)
        hlay.addLayout(preview_vbox, stretch=1)

        # ── Info panel ───────────────────────────────────────────────
        info = QFrame()
        info.setFrameStyle(QFrame.StyledPanel)
        info.setFixedWidth(215)
        ilay = QVBoxLayout(info)
        ilay.setContentsMargins(8, 8, 8, 8)
        ilay.setSpacing(6)

        # ══ GROUP 1: Photo Info metadata ════════════════════════════
        grp_meta = QGroupBox("Photo Info")
        grp_meta.setStyleSheet("QGroupBox { font-weight:bold; }")
        # Unified font for all metadata labels inside the group
        _meta_font_css = "font-size:11px; color:#333;"
        meta_lay = QVBoxLayout(grp_meta)
        meta_lay.setContentsMargins(6, 8, 6, 6)
        meta_lay.setSpacing(4)

        meta_hdr = QHBoxLayout()
        meta_hdr.setContentsMargins(0,0,0,0)
        meta_hdr.addStretch()
        self.btn_open_ext = QPushButton("\U0001f5bc")
        self.btn_open_ext.setFixedSize(24, 22)
        self.btn_open_ext.setToolTip("Open photo in the default application")
        self.btn_open_ext.setEnabled(False)
        self.btn_open_ext.clicked.connect(self._open_photo_external)
        meta_hdr.addWidget(self.btn_open_ext)
        meta_lay.addLayout(meta_hdr)

        self.lbl_photo_name = QLabel("\u2014")
        self.lbl_photo_name.setWordWrap(True)
        self.lbl_photo_name.setStyleSheet("font-weight:bold; color:#2c3e50;")
        meta_lay.addWidget(self.lbl_photo_name)

        self.lbl_photo_source = QLabel("Source: \u2014")
        self.lbl_photo_source.setWordWrap(True)
        self.lbl_photo_source.setStyleSheet(_meta_font_css)
        meta_lay.addWidget(self.lbl_photo_source)

        self.lbl_photo_author = QLabel("Author: \u2014")
        self.lbl_photo_author.setWordWrap(True)
        self.lbl_photo_author.setStyleSheet(_meta_font_css)
        meta_lay.addWidget(self.lbl_photo_author)

        self.lbl_photo_dt = QLabel("Date/Time:\n\u2014")
        self.lbl_photo_dt.setWordWrap(True)
        self.lbl_photo_dt.setStyleSheet(_meta_font_css)
        meta_lay.addWidget(self.lbl_photo_dt)

        self.lbl_photo_alt = QLabel("Altitude: \u2014")
        self.lbl_photo_alt.setStyleSheet(_meta_font_css)
        meta_lay.addWidget(self.lbl_photo_alt)

        self.lbl_photo_direction = QLabel("Direction: \u2014")
        self.lbl_photo_direction.setStyleSheet(_meta_font_css)
        meta_lay.addWidget(self.lbl_photo_direction)

        self.lbl_photo_camera = QLabel("Camera: \u2014")
        self.lbl_photo_camera.setWordWrap(True)
        self.lbl_photo_camera.setStyleSheet(_meta_font_css)
        meta_lay.addWidget(self.lbl_photo_camera)

        self.lbl_photo_hfov = QLabel("FL/HFOV: \u2014")
        self.lbl_photo_hfov.setStyleSheet(_meta_font_css)
        meta_lay.addWidget(self.lbl_photo_hfov)

        # lbl_photo_coords kept hidden for internal compatibility
        self.lbl_photo_coords = QLabel()
        self.lbl_photo_coords.setVisible(False)
        meta_lay.addWidget(self.lbl_photo_coords)

        ilay.addWidget(grp_meta)
        ilay.addStretch()

        # ══ GROUP 2: Map navigation ══════════════════════════════════
        grp_map = QGroupBox("Map navigation")
        grp_map.setStyleSheet("QGroupBox { font-weight:bold; }")
        map_lay = QVBoxLayout(grp_map)
        map_lay.setContentsMargins(6, 8, 6, 6)
        map_lay.setSpacing(4)

        grp_scale_inner = QGroupBox("Scale")
        sl_inner = QHBoxLayout(grp_scale_inner)
        sl_inner.setContentsMargins(4, 4, 4, 4)
        self.scale_main = QgsScaleWidget()
        self.scale_main.setToolTip("Set scale of the main QGIS map canvas")
        sl_inner.addWidget(self.scale_main)
        map_lay.addWidget(grp_scale_inner)

        btn_zoom = QPushButton("\U0001f50d Zoom to Point")
        btn_zoom.setFixedHeight(28)
        btn_zoom.clicked.connect(self._zoom_to_selected)
        map_lay.addWidget(btn_zoom)

        ilay.addWidget(grp_map)

        # ══ GROUP 3: Edit coordinates & EXIF write ═══════════════════
        grp_edit = QGroupBox("Edit coordinates")
        grp_edit.setStyleSheet("QGroupBox { font-weight:bold; }")
        edit_lay = QVBoxLayout(grp_edit)
        edit_lay.setContentsMargins(6, 8, 6, 6)
        edit_lay.setSpacing(4)

        self.btn_edit_point = QPushButton("\U0001f4cc Move Point (map)")
        self.btn_edit_point.setFixedHeight(28)
        self.btn_edit_point.setCheckable(True)
        self.btn_edit_point.setToolTip(
            "Click on the main QGIS map to assign/move the GPS position.\n"
            "Works on any photo, including those without GPX match."
        )
        self.btn_edit_point.toggled.connect(self._toggle_edit_mode)
        edit_lay.addWidget(self.btn_edit_point)

        self.btn_direction = QPushButton("🧭 Set Direction")
        self.btn_direction.setFixedHeight(28)
        self.btn_direction.setCheckable(True)
        self.btn_direction.setToolTip(
            "Set the camera direction (azimuth) for the selected photo.\n"
            "Click 1: camera position (pre-filled if photo has GPS).\n"
            "Click 2: subject/target point.\n"
            "The azimuth angle from North is computed automatically."
        )
        self.btn_direction.setEnabled(False)
        self.btn_direction.toggled.connect(self._toggle_direction_mode)
        edit_lay.addWidget(self.btn_direction)

        sep_mc = QFrame()
        sep_mc.setFrameShape(QFrame.HLine)
        sep_mc.setFrameShadow(QFrame.Sunken)
        edit_lay.addWidget(sep_mc)

        edit_lay.addWidget(QLabel("<b>Manual coordinates</b>"))

        lat_row = QHBoxLayout()
        lat_row.addWidget(QLabel("Lat:"))
        self.spin_manual_lat = QDoubleSpinBox()
        self.spin_manual_lat.setRange(-90.0, 90.0)
        self.spin_manual_lat.setDecimals(6)
        self.spin_manual_lat.setValue(0.0)
        self.spin_manual_lat.setSingleStep(0.0001)
        self.spin_manual_lat.setToolTip("Latitude (WGS84 decimal degrees)")
        lat_row.addWidget(self.spin_manual_lat)
        edit_lay.addLayout(lat_row)

        lon_row = QHBoxLayout()
        lon_row.addWidget(QLabel("Lon:"))
        self.spin_manual_lon = QDoubleSpinBox()
        self.spin_manual_lon.setRange(-180.0, 180.0)
        self.spin_manual_lon.setDecimals(6)
        self.spin_manual_lon.setValue(0.0)
        self.spin_manual_lon.setSingleStep(0.0001)
        self.spin_manual_lon.setToolTip("Longitude (WGS84 decimal degrees)")
        lon_row.addWidget(self.spin_manual_lon)
        edit_lay.addLayout(lon_row)

        alt_row = QHBoxLayout()
        alt_row.addWidget(QLabel("Alt:"))
        self.spin_manual_alt = QDoubleSpinBox()
        self.spin_manual_alt.setRange(-500.0, 9000.0)
        self.spin_manual_alt.setDecimals(1)
        self.spin_manual_alt.setValue(0.0)
        self.spin_manual_alt.setSuffix(" m")
        self.spin_manual_alt.setSpecialValueText("\u2014")
        self.spin_manual_alt.setToolTip("Altitude in metres (optional)")
        alt_row.addWidget(self.spin_manual_alt)
        edit_lay.addLayout(alt_row)

        self.btn_paste_coords = QPushButton("\U0001f4cb Paste from clipboard")
        self.btn_paste_coords.setFixedHeight(26)
        self.btn_paste_coords.setToolTip(
            "Parse lat/lon from clipboard text.\n"
            "Supported formats:\n"
            "  43.123456, 11.654321\n"
            "  43.123456 11.654321\n"
            "  43°7'24.4\"N 11°39'15.6\"E\n"
            "  POINT(11.654321 43.123456)   (WKT)\n"
            "  https://maps.google.com/?q=43.123456,11.654321"
        )
        self.btn_paste_coords.clicked.connect(self._paste_coords_from_clipboard)
        edit_lay.addWidget(self.btn_paste_coords)

        self.btn_apply_coords = QPushButton("\u2714 Apply coordinates")
        self.btn_apply_coords.setFixedHeight(28)
        self.btn_apply_coords.setToolTip(
            "Assign the lat/lon/alt values to the selected photo in the session."
        )
        self.btn_apply_coords.setEnabled(False)
        self.btn_apply_coords.clicked.connect(self._apply_manual_coords)
        edit_lay.addWidget(self.btn_apply_coords)


        ilay.addWidget(grp_edit)

        hlay.addWidget(info)
        return widget

    # ---- Controls bar --------------------------------------------------

    def _build_controls_bar(self):
        bar  = QWidget()
        hlay = QHBoxLayout(bar)
        hlay.setContentsMargins(4, 2, 4, 2)
        hlay.setSpacing(12)

        # ── Offset + Max gap ──────────────────────────────────────
        grp_off = QGroupBox("Clock offset (sec)")
        ol = QHBoxLayout(grp_off)
        self.spin_offset = QSpinBox()
        self.spin_offset.setRange(-86400, 86400)
        self.spin_offset.setValue(0)
        self.spin_offset.setToolTip(
            "Camera clock correction in seconds.\n"
            "Camera 5 min ahead of GPS: enter -300.\n"
            "Timezone is handled automatically by ExifTool."
        )
        ol.addWidget(self.spin_offset)
        hlay.addWidget(grp_off)

        grp_gap = QGroupBox("Max gap (sec)")
        gl = QHBoxLayout(grp_gap)
        self.spin_max_gap = QSpinBox()
        self.spin_max_gap.setRange(0, 86400)
        self.spin_max_gap.setValue(0)
        self.spin_max_gap.setSpecialValueText("No limit")
        self.spin_max_gap.setToolTip(
            "Maximum allowed time gap between surrounding GPX trackpoints.\n"
            "0 = no limit."
        )
        self.spin_max_gap.setFixedWidth(80)
        gl.addWidget(self.spin_max_gap)
        hlay.addWidget(grp_gap)

        vsep1 = QFrame()
        vsep1.setFrameShape(QFrame.VLine)
        vsep1.setFrameShadow(QFrame.Sunken)
        hlay.addWidget(vsep1)

        self.btn_assign_authors = QPushButton("\U0001f464 Assign authors...")
        self.btn_assign_authors.setFixedHeight(26)
        self.btn_assign_authors.setToolTip(
            "Connect a layer first to enable author assignment."
        )
        self.btn_assign_authors.setEnabled(False)
        self.btn_assign_authors.clicked.connect(self._open_author_dialog)
        hlay.addWidget(self.btn_assign_authors)

        vsep2 = QFrame()
        vsep2.setFrameShape(QFrame.VLine)
        vsep2.setFrameShadow(QFrame.Sunken)
        hlay.addWidget(vsep2)

        self.btn_apply_symbology = QPushButton("🎨 Apply Symbology")
        self.btn_apply_symbology.setFixedHeight(26)
        self.btn_apply_symbology.setEnabled(False)
        self.btn_apply_symbology.setToolTip(
            "Apply GeotagManager rule-based symbology to the connected layer.\n"
            "  \u2022 Green dot  = photo file exists\n"
            "  \u2022 Red circle = photo file missing\n"
            "  \u2022 Blue wedge = camera FOV (if hfov + direction available)\n\n"
            "After applying, use Layer Properties to customise or save as QML."
        )
        self.btn_apply_symbology.clicked.connect(self._apply_layer_symbology)
        hlay.addWidget(self.btn_apply_symbology)

        hlay.addStretch()

        self.lbl_gpx_info = QLabel("GPX: no tracks loaded")
        self.lbl_gpx_info.setStyleSheet("color:#666; font-size:10px;")
        hlay.addWidget(self.lbl_gpx_info)

        return bar

    # ------------------------------------------------------------------ #
    #  Signals                                                             #
    # ------------------------------------------------------------------ #

    def _connect_signals(self):
        self.act_load_folder.triggered.connect(self._load_folder)
        self.act_load_gpx.triggered.connect(self._add_gpx_files)
        self.btn_gpx_add.clicked.connect(self._add_gpx_files)
        self.btn_gpx_remove.clicked.connect(self._remove_gpx_file)
        self.gpx_list.currentRowChanged.connect(
            lambda r: self.btn_gpx_remove.setEnabled(r >= 0)
        )
        self.act_run.triggered.connect(self._run_batch)
        self.btn_geotag.clicked.connect(self._run_batch)
        self.btn_direction.toggled.connect(self._toggle_direction_mode)
        self.act_export_gpkg.triggered.connect(self._export_geopackage)
        self.act_clear.triggered.connect(self._clear_all)
        self.act_log.toggled.connect(self._toggle_log_panel)
        self.act_exiftool_setup.triggered.connect(self._open_exiftool_setup)
        self.act_fullscreen_photo.toggled.connect(self._toggle_photo_fullscreen)
        self.act_batch_scan.triggered.connect(self._open_batch_scan)

        self.photo_list.currentItemChanged.connect(self._on_selection_changed)
        self.photo_list.itemSelectionChanged.connect(self._on_multi_selection_changed)
        self.photo_list.itemDoubleClicked.connect(self._zoom_to_selected)

        # Scale widgets ↔ map canvases
        self.scale_main.scaleChanged.connect(self._apply_main_scale)
        self.main_canvas.scaleChanged.connect(self._on_main_scale_changed)

        # Update active layer label when TOC selection changes
        self.iface.currentLayerChanged.connect(self._on_active_layer_changed)
        # Keep project layer tracking for listener disconnect on removal
        QgsProject.instance().layerWillBeRemoved.connect(self._on_project_layers_removed)

    # ------------------------------------------------------------------ #
    #  Scale helpers                                                       #
    # ------------------------------------------------------------------ #

    def _apply_main_scale(self, scale):
        """User changed the main scale widget → set main canvas scale."""
        if scale and scale > 0:
            self.main_canvas.zoomScale(scale)

    def _on_main_scale_changed(self, scale):
        """Main canvas scale changed (pan/zoom) → update widget without loop."""
        self.scale_main.blockSignals(True)
        self.scale_main.setScale(scale)
        self.scale_main.blockSignals(False)

    def _on_active_layer_changed(self, layer):
        """Update the active layer label when TOC selection changes.
        If the listener is connected and the active layer changes,
        warn the user but keep the existing connection intact.
        """
        if layer:
            self.lbl_active_layer.setText(f"Active layer: {layer.name()}")
        else:
            self.lbl_active_layer.setText("Active layer: —")
        self._update_toolbar_state()  # disable btn_assign_authors

    def _on_project_layers_removed(self, layer_id):
        """Disconnect listener BEFORE the layer C++ object is destroyed."""
        if self._listened_layer and self._listened_layer.id() == layer_id:
            self._disconnect_layer_listener()
            self.btn_listen.setChecked(False)

    def _toggle_layer_listener(self, checked):
        if checked:
            # Use the layer currently selected in the TOC
            layer = self.iface.activeLayer()
            if layer is None:
                QMessageBox.information(
                    self, "GeotagManager",
                    "No layer selected in the TOC.\n"
                    "Click on a layer in the Layers panel first."
                )
                self.btn_listen.setChecked(False)
                return
            if not isinstance(layer, QgsVectorLayer):
                QMessageBox.information(
                    self, "GeotagManager",
                    f"The active layer '{layer.name()}' is not a vector layer.\n"
                    "Select a vector layer in the TOC."
                )
                self.btn_listen.setChecked(False)
                return
            # ── Save selection BEFORE any disconnect ──────────────────
            # Must read from the actual layer object before removeSelection()
            same_layer = (
                self._listened_layer is not None
                and self._listened_layer.id() == layer.id()
            )
            if same_layer:
                # Re-connecting same layer: read current live selection
                prev_selection = [f.id() for f in layer.selectedFeatures()]
                if not prev_selection:
                    # Fallback to saved FIDs if selection already cleared
                    prev_selection = getattr(self, "_saved_selection", [])
            else:
                prev_selection = []

            # ── Disconnect previous listener ───────────────────────────
            self._disconnect_layer_listener()
            self._listened_layer = layer
            self._path_field = self._detect_path_field(layer)
            layer.selectionChanged.connect(self._on_layer_selection_changed)
            self.btn_listen.setText("Disconnect")
            field_info = f" [field: {self._path_field}]" if self._path_field else " [no path field found]"
            self.lbl_listen_status.setText(f"Listening: {layer.name()}{field_info}")
            self.lbl_listen_status.setStyleSheet("font-size:9px; color:#27ae60;")
            self.lbl_active_layer.setText(f"Active layer: {layer.name()}")
            self._setup_layer_open_action(layer)
            self._set_status(f"Listening for selections on: {layer.name()}")
            self._update_toolbar_state()
            # ── Restore selection ──────────────────────────────────────
            if prev_selection:
                # Restore FIDs — this emits selectionChanged
                layer.selectByIds(prev_selection)
                # Also call handler directly in case signal is lost
                # (can happen if QGIS deduplicates identical selection events)
                self._on_layer_selection_changed(prev_selection, [], False)
            elif layer.selectedFeatureCount() > 0:
                fids = [f.id() for f in layer.selectedFeatures()]
                self._on_layer_selection_changed(fids, [], False)
        else:
            self._disconnect_layer_listener()

    def _disconnect_layer_listener(self):
        layer = self._listened_layer
        if layer:
            # Save current selection before deselecting
            self._saved_selection = [f.id() for f in layer.selectedFeatures()]
            self._saved_selection_layer_id = layer.id()
            # Deselect features before disconnecting
            try:
                layer.removeSelection()
            except Exception as _e:
                QgsMessageLog.logMessage(
                    f"GeotagManager: removeSelection failed: {_e}",
                    "GeotagManager", Qgis.Warning)
            # Disconnect signal
            try:
                layer.selectionChanged.disconnect(
                    self._on_layer_selection_changed
                )
            except Exception:
                pass
            self._listened_layer = None
        self._path_field = None
        self.btn_listen.setText("Connect")
        self.lbl_listen_status.setText("Not connected")
        self.lbl_listen_status.setStyleSheet("font-size:9px; color:#888;")
        self.lbl_active_layer.setText("Active layer: —")
        # Ask whether to clear photos loaded via layer
        if getattr(self, "_layer_loaded_photos", False) and self.photo_items:
            reply = QMessageBox.question(
                self, "Disconnect Layer",
                f"{len(self.photo_items)} photo(s) were loaded via the layer connection.\n"
                "Clear the Photos section?",
                QMessageBox.Yes | QMessageBox.No
            )
            if reply == QMessageBox.Yes:
                self._clear_photos()
                self._layer_loaded_photos = False
        self._update_toolbar_state()

    def _detect_path_field(self, layer):
        """Rileva campo stringa con path delle foto.
        Cerca fra i campi stringa quelli con 'path','file','location','percorso'
        nel nome (case insensitive).
        - Se trovato uno solo  -> lo usa senza chiedere
        - Se trovati piu' di uno -> QInputDialog per scelta utente
        - Se nessuno            -> primo campo stringa disponibile
        """
        from qgis.PyQt.QtCore import QVariant
        str_fields = [
            f.name() for f in layer.fields()
            if f.type() in (QVariant.String, 10)
        ]
        # Only match fields containing 'path' or 'location' (excludes 'filename' etc.)
        keywords = ('path', 'location')
        candidates = [n for n in str_fields
                      if any(kw in n.lower() for kw in keywords)]

        if len(candidates) == 1:
            return candidates[0]
        if len(candidates) > 1:
            from qgis.PyQt.QtWidgets import QInputDialog
            choice, ok = QInputDialog.getItem(
                self,
                "GeotagManager — Select path field",
                "Select the field containing the photo file path:",
                candidates, 0, False
            )
            return choice if ok else candidates[0]
        # Nessun candidato con keyword: usa primo campo stringa
        return str_fields[0] if str_fields else None

    def _on_layer_selection_changed(self, selected_ids, deselected_ids, clear_and_select):
        """Gestisce la selezione di un punto nel layer connesso.
        Metodo di recupero feature: layer.selectedFeatures() — al 100% affidabile.
        """
        if not self._listened_layer:
            return

        layer = self._listened_layer

        # ── Recupero feature: usa selectedFeatures() direttamente ─────────
        # Most reliable method — does not depend on FID or expressions
        sel = layer.selectedFeatures()
        if not sel:
            return
        feature = sel[0]

        # ── Normalizzazione path: gestisce separatori misti /  e \ ────────
        def norm(p):
            if p is None:
                return None
            s = str(p).strip()
            if s in ("", "NULL", "None", "null"):
                return None
            # Normalise all separators to os.sep in two steps
            s = s.replace("\\\\", "/")   # doppio backslash
            s = s.replace("\\", "/")      # singolo backslash
            s = s.replace("/", os.sep)    # forward slash -> os.sep
            return os.path.normpath(s)

        filepath = None

        # ── Lettura path ───────────────────────────────────────────────────
        # 1. Campo rilevato al connect
        if self._path_field:
            try:
                filepath = norm(feature[self._path_field])
            except Exception:
                pass

        # 2. Tutti i campi stringa con keyword nel nome
        if not filepath:
            from qgis.PyQt.QtCore import QVariant
            keywords = ('path', 'location')
            for field in layer.fields():
                if field.type() not in (QVariant.String, 10):
                    continue
                if not any(kw in field.name().lower() for kw in keywords):
                    continue
                try:
                    v = norm(feature[field.name()])
                    if v:
                        filepath = v
                        break
                except Exception:
                    continue

        # 3. Tutti i campi stringa (brute force)
        if not filepath:
            from qgis.PyQt.QtCore import QVariant
            for field in layer.fields():
                if field.type() not in (QVariant.String, 10):
                    continue
                try:
                    v = norm(feature[field.name()])
                    if v and len(v) > 5:
                        filepath = v
                        break
                except Exception:
                    continue

        if not filepath:
            self._set_status("No valid path field found in the selected feature.")
            return

        # ── Verifica esistenza con fallback ────────────────────────────────
        fname = os.path.basename(filepath)
        if not os.path.isfile(filepath):
            resolved = False
            # a) cartella corrente
            if self._photo_folder:
                alt = os.path.join(self._photo_folder, fname)
                if os.path.isfile(alt):
                    filepath = alt
                    resolved = True
            # b) foto in sessione
            if not resolved:
                for it in self.photo_items:
                    if it.filename == fname and os.path.isfile(it.filepath):
                        filepath = it.filepath
                        resolved = True
                        break
            if not resolved:
                self._set_status(f"File not reachable: {fname}")
                return

        # ── Carica cartella se e' diversa da quella corrente ───────────────
        photo_dir   = os.path.dirname(os.path.abspath(filepath))
        current_dir = os.path.abspath(self._photo_folder) if self._photo_folder else ""
        folder_changed = (photo_dir != current_dir)
        if folder_changed:
            self._set_status(f"Loading: {photo_dir}...")
            self._load_folder_silent(photo_dir)
            self._layer_loaded_photos = True

        # ── Cerca PhotoItem corrispondente ─────────────────────────────────
        fp_abs = os.path.abspath(filepath)
        target = None
        for item in self.photo_items:
            if os.path.abspath(item.filepath) == fp_abs:
                target = item
                break
        if target is None:
            for item in self.photo_items:
                if item.filename == fname:
                    target = item
                    break
        if target is None:
            self._set_status(f"Foto non trovata: {fname}")
            return

        # ── Seleziona e mostra ─────────────────────────────────────────────
        if target.list_item:
            self.photo_list.blockSignals(True)
            self.photo_list.setCurrentItem(target.list_item)
            self.photo_list.blockSignals(False)
            self.photo_list.scrollToItem(target.list_item)

        # ── Read author from layer (always preferred over session value) ───
        def _fval(fname):
            try:
                v = feature[fname]
                if v is None or str(v).strip() in ("", "NULL", "None"):
                    return None
                return v
            except Exception:
                return None

        v = _fval("author")
        if v is not None:
            target.author = str(v).strip()

        self._selected_item = target
        self._update_info_panel(target)
        self.btn_edit_point.setEnabled(True)
        self.btn_apply_coords.setEnabled(True)
        self._prefill_manual_coords(target)
        self._draw_highlight(target)
        if target.has_position:
            self._zoom_to_selected()
        prefix = "New folder loaded - " if folder_changed else ""
        coords  = f"({target.lat:.6f}, {target.lon:.6f})" if target.has_position else ""
        self._set_status(f"{prefix}{target.filename} {coords}")


    # ------------------------------------------------------------------ #
    #  Actions                                                             #
    # ------------------------------------------------------------------ #

    def _load_folder(self):
        folder = QFileDialog.getExistingDirectory(self, "Select photo folder", "")
        if not folder:
            return
        # Warn if a layer is currently connected
        if self._listened_layer and self._listened_layer.isValid():
            reply = QMessageBox.question(
                self, "Load Photos — Layer Connected",
                f"A layer is currently connected:\n"
                f"  {self._listened_layer.name()}\n\n"
                "Loading photos manually may cause confusion between\n"
                "layer-driven and manually loaded photos.\n\n"
                "Continue anyway?",
                QMessageBox.Yes | QMessageBox.No
            )
            if reply != QMessageBox.Yes:
                return
        self._photo_folder = folder
        self._layer_loaded_photos = False  # manual load
        self._clear_photos()
        self.btn_open_folder.setEnabled(True)
        # Always recursive
        image_paths = list_images(folder, recursive=True)
        if not image_paths:
            QMessageBox.warning(
                self, "GeotagManager",
                "No images found in the selected folder (including subdirectories).")
            return

        total = len(image_paths)
        self.progress_bar.setRange(0, total)
        self.progress_bar.setValue(0)
        self.progress_bar.setVisible(True)
        self._set_status(f"Reading EXIF from {total} photos…")

        for i, fpath in enumerate(image_paths):
            self.progress_bar.setValue(i + 1)
            if i % 50 == 0:
                self._set_status(
                    f"Loading {i+1}/{total}: {os.path.basename(fpath)}"
                )
                QApplication.processEvents()
            item = PhotoItem(fpath)
            exif = read_exif_gps(fpath)
            if exif:
                # Always read non-GPS fields (camera, datetime, alt, direction)
                item.alt               = exif.get("alt")
                item.direction         = exif.get("direction")
                item.pdop              = exif.get("pdop")
                item.focal_length      = exif.get("focal_length")
                item.focal_length_35mm = exif.get("focal_length_35mm")
                item.hfov              = exif.get("hfov")
                item.make              = exif.get("make", "")
                item.model             = exif.get("model", "")
                item.satellites        = exif.get("satellites", 0)
                item.datetime          = exif.get("datetime")
                # GPS coords only if present
                if exif.get("lat") is not None:
                    item.lat    = exif["lat"]
                    item.lon    = exif["lon"]
                    item.source = "exif"
                else:
                    item.source = "pending"
            else:
                item.datetime = get_image_datetime(fpath)
                item.source   = "pending"
            self.photo_items.append(item)
            self._add_list_item(item)


        self.progress_bar.setVisible(False)
        self._update_counts()
        self._update_toolbar_state()
        self._draw_all_rubber_bands()
        self._update_nav_state()
        self._set_status(
            f"Loaded {total} photos recursively from {folder}"
        )

    def _load_folder_silent(self, folder):
        """Carica una cartella foto senza aprire il file dialog."""
        image_paths = list_images(folder, recursive=True)
        if not image_paths:
            return
        self._photo_folder = folder
        self._clear_photos()
        self.btn_open_folder.setEnabled(True)
        total = len(image_paths)
        self.progress_bar.setRange(0, total)
        self.progress_bar.setValue(0)
        self.progress_bar.setVisible(True)
        self._set_status(f"Loading {total} photos from {folder}...")
        for i, fpath in enumerate(image_paths):
            self.progress_bar.setValue(i + 1)
            if i % 50 == 0:
                self._set_status(
                    f"Loading {i+1}/{total}: {os.path.basename(fpath)}"
                )
                QApplication.processEvents()
            item = PhotoItem(fpath)
            exif = read_exif_gps(fpath)
            if exif:
                # Always read non-GPS fields (camera, datetime, alt, direction)
                item.alt               = exif.get("alt")
                item.direction         = exif.get("direction")
                item.pdop              = exif.get("pdop")
                item.focal_length      = exif.get("focal_length")
                item.focal_length_35mm = exif.get("focal_length_35mm")
                item.hfov              = exif.get("hfov")
                item.make              = exif.get("make", "")
                item.model             = exif.get("model", "")
                item.satellites        = exif.get("satellites", 0)
                item.datetime          = exif.get("datetime")
                # GPS coords only if present
                if exif.get("lat") is not None:
                    item.lat    = exif["lat"]
                    item.lon    = exif["lon"]
                    item.source = "exif"
                else:
                    item.source = "pending"
            else:
                item.datetime = get_image_datetime(fpath)
                item.source   = "pending"
            self.photo_items.append(item)
            self._add_list_item(item)

        self.progress_bar.setVisible(False)
        self._update_counts()
        self._update_toolbar_state()
        self._draw_all_rubber_bands()
        self._update_nav_state()
        self._set_status(f"Loaded {total} photos from {folder}")

    def _add_gpx_files(self):
        """Open file dialog to add one or more GPX files."""
        paths, _ = QFileDialog.getOpenFileNames(
            self, "Add GPX track files", "",
            "GPX Files (*.gpx);;All Files (*)"
        )
        if not paths:
            return
        added = 0
        for path in paths:
            if path in self._gpx_paths:
                continue  # already loaded
            try:
                pts = parse_gpx(path)
                if not pts:
                    QMessageBox.warning(self, "GeotagManager",
                                        f"No track points in:\n{path}")
                    continue
                self._gpx_paths.append(path)
                item = QListWidgetItem(
                    f"📍 {os.path.basename(path)}  ({len(pts)} pts)"
                )
                item.setData(Qt.UserRole, path)
                self.gpx_list.addItem(item)
                added += 1
            except Exception as e:
                QMessageBox.critical(self, "GeotagManager",
                                     f"Error reading GPX:\n{e}")
        if added:
            self._rebuild_track()

    def _remove_gpx_file(self):
        """Remove the selected GPX file from the list."""
        row = self.gpx_list.currentRow()
        if row < 0:
            return
        item = self.gpx_list.takeItem(row)
        path = item.data(Qt.UserRole)
        if path in self._gpx_paths:
            self._gpx_paths.remove(path)
        self._rebuild_track()

    def _rebuild_track(self):
        """Merge all loaded GPX files and update track_points + UI labels."""
        from ..core.batch_worker import merge_track_points
        if not self._gpx_paths:
            self.track_points = []
            self.lbl_gpx_points.setText("Total track points: 0")
            self.lbl_gpx_info.setText("GPX: no tracks loaded")
            self._update_toolbar_state()
            return
        self.track_points, info = merge_track_points(self._gpx_paths)
        t0 = self.track_points[0]["time"]
        t1 = self.track_points[-1]["time"]
        self.lbl_gpx_points.setText(f"Total track points: {len(self.track_points)}")
        self.lbl_gpx_info.setText(
            f"GPX: {len(self._gpx_paths)} file(s), {len(self.track_points)} pts | "
            f"{t0.strftime('%d/%m/%Y %H:%M')} → {t1.strftime('%d/%m/%Y %H:%M')}"
        )
        self._update_toolbar_state()
        self._set_status(
            f"GPX tracks: {len(self._gpx_paths)} file(s), "
            f"{len(self.track_points)} total points"
        )

    def _run_batch(self):
        if not self.photo_items or not self._gpx_paths:
            return
        self._set_status("Processing…")
        self.progress_bar.setVisible(True)
        self.progress_bar.setRange(0, len(self.photo_items))
        self.progress_bar.setValue(0)

        pending = [it for it in self.photo_items if it.source == "pending"]
        paths   = [it.filepath for it in pending]

        from ..core.exif_handler import find_exiftool, engine_info
        et = find_exiftool()
        if et:
            self._set_status(f"Geotagging via ExifTool ({engine_info()})…")
        else:
            self._set_status(
                "ExifTool not found — using pure Python interpolation"
            )

        self._worker = GeotagWorker(
            paths,
            gpx_paths=self._gpx_paths,
            track_points=self.track_points,
            time_offset_seconds=self.spin_offset.value(),
            max_time_gap_seconds=self.spin_max_gap.value(),
            utc_offset_seconds=None,
        )
        self._worker_thread = QThread()
        self._worker.moveToThread(self._worker_thread)
        self._worker_thread.started.connect(self._worker.run)
        self._worker.progress.connect(self._on_batch_progress)
        self._worker.photo_processed.connect(self._on_photo_matched)
        self._worker.finished.connect(self._on_batch_finished)
        self._worker_thread.start()


        pending = [it for it in self.photo_items if it.source == "pending"]
        paths   = [it.filepath for it in pending]

        self._worker        = BatchWorker(
            paths, self.track_points,
            time_offset_seconds=self.spin_offset.value(),
            max_time_gap_seconds=self.spin_max_gap.value(),
            utc_offset_seconds=utc_off,
        )
        self._worker_thread = QThread()
        self._worker.moveToThread(self._worker_thread)
        self._worker_thread.started.connect(self._worker.run)
        self._worker.progress.connect(self._on_batch_progress)
        self._worker.photo_processed.connect(self._on_photo_matched)
        self._worker.finished.connect(self._on_batch_finished)
        self._worker_thread.start()

    @pyqtSlot(int, int, str)
    def _on_batch_progress(self, current, total, fname):
        self.progress_bar.setValue(current)
        self._set_status(f"Processing {current}/{total}: {fname}")
        # Safe: this slot does not emit signals that could re-enter it
        QApplication.processEvents()

    @pyqtSlot(dict)
    def _on_photo_matched(self, record):
        for item in self.photo_items:
            if item.filepath == record["filepath"]:
                item.lat               = record["lat"]
                item.lon               = record["lon"]
                item.alt               = record.get("alt")
                item.direction         = record.get("direction")
                item.pdop              = record.get("pdop")
                item.focal_length      = record.get("focal_length")
                item.focal_length_35mm = record.get("focal_length_35mm")
                item.hfov              = record.get("hfov")
                item.make              = record.get("make", "")
                item.model             = record.get("model", "")
                item.satellites        = record.get("satellites", 0)
                item.datetime          = record.get("datetime")
                item.source            = "gpx"
                self._update_list_item(item)
                self._draw_rubber_band(item)
                self._update_info_panel(item)
                break
        # NOTE: do NOT call QApplication.processEvents() here.
        # This slot is already on the UI thread — processEvents()
        # would re-enter _on_photo_matched causing infinite recursion.

    @pyqtSlot(list, list)
    def _on_batch_finished(self, matched, skipped):
        self._worker_thread.quit()
        self._worker_thread.wait()
        self.progress_bar.setVisible(False)

        # Mark skipped items in the list
        for fpath in skipped:
            for item in self.photo_items:
                if item.filepath == fpath and item.source == "pending":
                    item.source = "skipped"
                    self._update_list_item(item)

        self._update_counts()
        self._update_toolbar_state()

        total   = len(self.photo_items)
        n_match = len(matched)
        n_skip  = len(skipped)
        n_prev  = total - n_match - n_skip  # already had GPS before this run

        # Persistent status bar summary
        self._set_status(
            f"Geotag complete — {n_match}/{total} matched, "
            f"{n_skip} outside GPX time range"
        )

        # Summary dialog with full breakdown
        lines = [
            f"<b>Geotag complete</b>",
            "",
            f"<table cellspacing='4'>",
            f"<tr><td>Total photos:</td><td><b>{total}</b></td></tr>",
            f"<tr><td>✔ Matched to GPX:</td>"
            f"<td><b><font color='#27ae60'>{n_match}</font></b></td></tr>",
        ]
        if n_skip:
            lines.append(
                f"<tr><td>⚠ Outside GPX time range:</td>"
                f"<td><b><font color='#e67e22'>{n_skip}</font></b></td></tr>"
            )
        if n_prev > 0:
            lines.append(
                f"<tr><td>Already had GPS:</td>"
                f"<td><b>{n_prev}</b></td></tr>"
            )
        lines += ["</table>", ""]

        if n_skip:
            lines.append(
                "<small><i>Unmatched photos are shown in orange in the list.<br>"
                "Try adjusting the time offset or loading additional GPX tracks.</i></small>"
            )

        msg = QMessageBox(self)
        msg.setWindowTitle("Geotag Results")
        msg.setIcon(QMessageBox.Information if n_match > 0 else QMessageBox.Warning)
        msg.setText("\n".join(lines))
        msg.exec_()

    def _export_geopackage(self):

        all_photos   = self.photo_items
        matched      = [it for it in all_photos if it.has_position]
        gpx_m        = [it for it in matched if it.source == "gpx"]
        exif_m       = [it for it in matched if it.source == "exif"]
        manual_m     = [it for it in matched if it.source == "manual"]
        n_skipped    = sum(1 for it in all_photos if it.source == "skipped")
        n_pending    = sum(1 for it in all_photos if it.source == "pending")

        if not matched:
            QMessageBox.information(self, "GeotagManager",
                                    "No photos with position to export.")
            return

        # Author summary: show unique authors or "(not set)"
        _authors = sorted({it.author for it in matched if it.author})
        author_str = ", ".join(_authors) if _authors else "(not set)"
        lines = [
            "Ready to create GeoPackage layer.",
            "",
            f"  Total photos loaded :  {len(all_photos)}",
            f"  To be exported      :  {len(matched)}",
            f"    from GPX track    :  {len(gpx_m)}",
            f"    from EXIF         :  {len(exif_m)}",
            f"    manual placement  :  {len(manual_m)}",
            f"  Skipped (no match)  :  {n_skipped}",
            f"  Not processed       :  {n_pending}",
            "",
            f"  Author              :  {author_str}",
            f"  Write EXIF on export:  {write_exif_str}",
            "",
            "Proceed?",
        ]
        reply = QMessageBox.question(
            self, "Export GeoPackage — Confirm",
            "\n".join(lines),
            QMessageBox.Yes | QMessageBox.No
        )
        if reply != QMessageBox.Yes:
            return

        import os as _os
        from ..core.geopackage_exporter import make_date_prefix
        _prefix = make_date_prefix([it.to_record() for it in matched])
        _fname  = f"{_prefix}_layer.gpkg" if _prefix else "geotagged_photos.gpkg"
        _default = _os.path.join(self._photo_folder or "", _fname)
        path, _ = QFileDialog.getSaveFileName(
            self, "Save GeoPackage", _default,
            "GeoPackage (*.gpkg);;All Files (*)"
        )
        if not path:
            return

        # author is now per-PhotoItem (set via AuthorDialog)
        records = [it.to_record() for it in matched]
        ok, msg, _layer_name = export_to_geopackage(records, path)
        if ok:
            self._set_status(msg)
            # Load layer into QGIS project
            from ..core.geopackage_exporter import load_geopackage_layer
            loaded = load_geopackage_layer(path, layer_name=_layer_name)
            if loaded:
                # Apply symbology before activating
                if False:
                    try:
                        from ..core.layer_symbology import apply_photo_symbology
                        apply_photo_symbology(loaded)
                    except Exception as _e:
                        QgsMessageLog.logMessage(
                            f"GeotagManager symbology error (export): {_e}",
                            "GeotagManager", Qgis.Warning)
                # Activate layer in TOC — do NOT auto-connect listener here
                # (setChecked triggers _toggle_layer_listener which needs
                #  activeLayer() to be set first via a queued call)
                self.iface.setActiveLayer(loaded)
                self.iface.zoomToActiveLayer()
                self.iface.mapCanvas().refresh()
                # Update active layer label and auto-connect
                self.lbl_active_layer.setText(f"Active layer: {loaded.name()}")
                # Action "Open photo"
                self._setup_layer_open_action(loaded)
                # Use a singleShot to let QGIS process setActiveLayer first
                from qgis.PyQt.QtCore import QTimer
                QTimer.singleShot(100, lambda: self.btn_listen.setChecked(True))
                QMessageBox.information(
                    self, "GeotagManager",
                    f"{msg}\n\nLayer loaded: {loaded.name()}"
                )
            else:
                QMessageBox.warning(
                    self, "GeotagManager",
                    f"{msg}\n\nAttenzione: impossibile caricare il layer nel progetto.\n"
                    f"Aprire manualmente il file: {path}"
                )
        else:
            QMessageBox.critical(self, "GeotagManager", msg)

    def _toggle_edit_mode(self, checked):
        if checked:
            # Build queue from selected items (in list order)
            self._move_point_queue = [
                wi.data(Qt.UserRole)
                for wi in self.photo_list.selectedItems()
                if wi.data(Qt.UserRole) is not None
            ]
            if not self._move_point_queue:
                QMessageBox.information(self, "GeotagManager",
                                        "Select one or more photos from the list first.")
                self.btn_edit_point.setChecked(False)
                return
            self._prev_map_tool = self.main_canvas.mapTool()
            self._point_tool    = GeotagPointTool(self.main_canvas)
            self._point_tool.point_placed.connect(self._on_point_placed)
            self.main_canvas.setMapTool(self._point_tool)
            self.iface.mainWindow().activateWindow()
            first = self._move_point_queue[0]
            n = len(self._move_point_queue)
            self._set_status(
                f"📌 Click to position: {first.filename}"
                f"  (1/{n})  |  [Esc] to cancel"
            )
        else:
            self._move_point_queue = []
            if self._point_tool:
                self.main_canvas.unsetMapTool(self._point_tool)
                self._point_tool = None
            if self._prev_map_tool:
                self.main_canvas.setMapTool(self._prev_map_tool)
                self._prev_map_tool = None
            self._set_status("Move Point mode deactivated.")

    @pyqtSlot(float, float)
    def _on_point_placed(self, lat, lon):
        """Handle a map click. Assigns coords to the first photo in the
        queue, then advances to the next. Deactivates when queue is empty.
        """
        if not self._move_point_queue:
            self.btn_edit_point.setChecked(False)
            return

        # Assign to the first item in the queue
        item        = self._move_point_queue.pop(0)
        item.lat    = lat
        item.lon    = lon
        item.source = "manual"
        self._update_list_item(item)
        self._draw_rubber_band(item)
        self._update_layer_coords(item)
        self._update_layer_geometry(item)
        self._prefill_manual_coords(item)
        self._set_status(
            f"Point placed: {item.filename} → lat={lat:.6f}, lon={lon:.6f}"
        )
        self._write_exif_if_immediate(item)

        # Highlight this item in the list
        if item.list_item:
            self.photo_list.blockSignals(True)
            self.photo_list.setCurrentItem(item.list_item)
            self.photo_list.blockSignals(False)
            self.photo_list.scrollToItem(item.list_item)
        self._selected_item = item
        self._update_info_panel(item)
        self._draw_highlight(item)

        if self._move_point_queue:
            # More photos waiting — keep tool active, update status
            nxt = self._move_point_queue[0]
            remaining = len(self._move_point_queue)
            total = self.photo_list.selectedItems().__len__  # approx
            self._set_status(
                f"📌 Next: {nxt.filename}  "
                f"({remaining} remaining)  |  [Esc] to stop"
            )
        else:
            # Queue exhausted — deactivate tool
            self.btn_edit_point.setChecked(False)
            self._set_status("All selected photos positioned.")

    def _toggle_direction_mode(self, checked):
        """Activate/deactivate the DirectionTool on the map canvas."""
        if checked:
            item = self._selected_item
            if item is None:
                QMessageBox.information(self, "GeotagManager",
                                        "Select a photo first.")
                self.btn_direction.setChecked(False)
                return
            # Tool works only for photos with GPS coordinates
            if not item.has_position:
                QMessageBox.information(self, "GeotagManager",
                    "Assign GPS coordinates to this photo before setting direction.")
                self.btn_direction.setChecked(False)
                return
            # Origin is always the photo GPS position (single-click mode)
            origin = (item.lat, item.lon)
            self._prev_map_tool  = self.main_canvas.mapTool()
            self._direction_tool = DirectionTool(self.main_canvas, origin)
            self._direction_tool.direction_set.connect(self._on_direction_set)
            self.main_canvas.setMapTool(self._direction_tool)
            self.iface.mainWindow().activateWindow()
            self._set_status(
                f"🧭 Click the subject to set direction for: {item.filename}"
                "  |  [Esc] to cancel"
            )
        else:
            if self._direction_tool:
                self.main_canvas.unsetMapTool(self._direction_tool)
                self._direction_tool = None
            if self._prev_map_tool:
                self.main_canvas.setMapTool(self._prev_map_tool)
                self._prev_map_tool = None
            self._set_status("Direction tool deactivated.")

    @pyqtSlot(float)
    def _on_direction_set(self, azimuth):
        """Receives azimuth from DirectionTool and assigns to selected photo."""
        item = self._selected_item
        if item is None:
            return
        item.direction = azimuth
        # Update info panel and force direction label directly
        self._update_info_panel(item)
        self.lbl_photo_direction.setText(f"Direction: {azimuth:.1f}\u00b0")
        self._update_layer_feature_direction(item)
        # Write direction to EXIF if file exists
        if os.path.isfile(item.filepath):
            try:
                if item.has_position:
                    write_exif_gps(
                        item.filepath,
                        item.lat, item.lon,
                        alt=item.alt,
                        direction=azimuth,
                        author=getattr(item, "author", None) or None
                    )
                else:
                    # No GPS coords yet — write direction-only via exiftool
                    from ..core.exif_handler import find_exiftool
                    import subprocess as _sp
                    et = find_exiftool()
                    if et:
                        _sp.run(
                            [et, '-overwrite_original',
                             f'-GPSImgDirection={azimuth}',
                             '-GPSImgDirectionRef=T',
                             item.filepath],
                            capture_output=True, timeout=10
                        )
            except Exception as _e:
                from qgis.core import QgsMessageLog, Qgis
                QgsMessageLog.logMessage(
                    f"GeotagManager: direction EXIF write failed: {_e}",
                    "GeotagManager", Qgis.Warning)
        self.btn_direction.setChecked(False)
        self._set_status(
            f"Direction set: {item.filename} \u2192 {azimuth:.1f}\u00b0"
        )

    def _update_layer_feature_direction(self, item):
        """Write direction to the connected layer feature."""
        layer = self._listened_layer
        if not layer or not layer.isValid():
            return
        fields = [f.name() for f in layer.fields()]
        fp_idx  = layer.fields().indexFromName("filepath")  if "filepath"  in fields else -1
        dir_idx = layer.fields().indexFromName("direction") if "direction" in fields else -1
        if fp_idx < 0 or dir_idx < 0:
            return
        fp_target = os.path.normpath(
            item.filepath.replace("\\", "/").replace("/", os.sep)
        )
        changes = {}
        for feat in layer.getFeatures():
            try:
                fp_raw = str(feat[fp_idx] or "").strip()
                if not fp_raw or fp_raw in ("NULL", "None"):
                    continue
                fp_norm = os.path.normpath(
                    fp_raw.replace("\\", "/").replace("/", os.sep)
                )
                if fp_norm == fp_target:
                    changes[feat.id()] = {dir_idx: item.direction}
                    break
            except Exception:
                continue
        if changes:
            layer.dataProvider().changeAttributeValues(changes)
            layer.triggerRepaint()

    def _prefill_manual_coords(self, item):

        """Fill the manual lat/lon/alt spinboxes from item's current position."""
        self.spin_manual_lat.blockSignals(True)
        self.spin_manual_lon.blockSignals(True)
        self.spin_manual_alt.blockSignals(True)
        if item and item.has_position:
            self.spin_manual_lat.setValue(item.lat)
            self.spin_manual_lon.setValue(item.lon)
            self.spin_manual_alt.setValue(item.alt if item.alt is not None else 0.0)
        else:
            self.spin_manual_lat.setValue(0.0)
            self.spin_manual_lon.setValue(0.0)
            self.spin_manual_alt.setValue(0.0)
        self.spin_manual_lat.blockSignals(False)
        self.spin_manual_lon.blockSignals(False)
        self.spin_manual_alt.blockSignals(False)

    def _apply_manual_coords(self):
        """Assign the spinbox lat/lon/alt to all selected photos."""
        lat = self.spin_manual_lat.value()
        lon = self.spin_manual_lon.value()
        alt_val = self.spin_manual_alt.value()
        alt = alt_val if alt_val != self.spin_manual_alt.minimum() else None

        # Collect all selected PhotoItems
        selected_items = [
            wi.data(Qt.UserRole)
            for wi in self.photo_list.selectedItems()
            if wi.data(Qt.UserRole) is not None
        ]
        if not selected_items:
            return

        for item in selected_items:
            item.lat    = lat
            item.lon    = lon
            item.alt    = alt
            item.source = "manual"
            self._update_list_item(item)
            self._draw_rubber_band(item)
            self._update_layer_feature(item, update_geom=True,
                                       update_coords=True,
                                       update_direction=False)

        self._update_toolbar_state()

        # Update info panel and highlight for current (last) item
        if self._selected_item and self._selected_item in selected_items:
            self._update_info_panel(self._selected_item)
            self._draw_highlight(self._selected_item)

        n = len(selected_items)
        self._set_status(
            f"Coordinates applied to {n} photo(s) → lat={lat:.6f}, lon={lon:.6f}"
        )
        # Write EXIF only if single photo selected (batch EXIF needs explicit confirm)
        if n == 1:
            self._write_exif_if_immediate(selected_items[0])

    def _update_layer_feature(self, item, update_geom=True,
                              update_coords=True, update_direction=False):
        """Single method to update attributes and/or geometry of the matching
        layer feature. Performs ONE getFeatures() scan regardless of how many
        fields need updating.

        update_geom:      move the point geometry to item.lat/lon
        update_coords:    write latitude/longitude/altitude attributes
        update_direction: write direction attribute
        """
        layer = self._listened_layer
        if not layer or not layer.isValid():
            return

        fields     = {f.name(): layer.fields().indexFromName(f.name())
                      for f in layer.fields()}
        fp_idx     = fields.get('filepath', -1)
        lat_idx    = fields.get('latitude',  -1)
        lon_idx    = fields.get('longitude', -1)
        alt_idx    = fields.get('altitude',  -1)
        dir_idx    = fields.get('direction', -1)

        if fp_idx < 0:
            return

        fp_target = os.path.normpath(
            item.filepath.replace('\\', '/').replace('/', os.sep)
        )

        # Find matching feature
        target_fid = None
        for feat in layer.getFeatures():
            try:
                fp_raw = str(feat[fp_idx] or '').strip()
                if not fp_raw or fp_raw in ('NULL', 'None'):
                    continue
                fp_norm = os.path.normpath(
                    fp_raw.replace('\\', '/').replace('/', os.sep)
                )
                if fp_norm == fp_target:
                    target_fid = feat.id()
                    break
            except Exception:
                continue

        if target_fid is None:
            return

        # Update attributes
        attr_changes = {}
        if update_coords and lat_idx >= 0 and lon_idx >= 0:
            attr_changes[lat_idx] = item.lat
            attr_changes[lon_idx] = item.lon
            if alt_idx >= 0 and item.alt is not None:
                attr_changes[alt_idx] = item.alt
        if update_direction and dir_idx >= 0 and item.direction is not None:
            attr_changes[dir_idx] = item.direction

        if attr_changes:
            layer.dataProvider().changeAttributeValues(
                {target_fid: attr_changes}
            )

        # Update geometry
        if update_geom and item.has_position:
            crs_wgs84 = QgsCoordinateReferenceSystem('EPSG:4326')
            crs_layer = layer.crs()
            tr = QgsCoordinateTransform(
                crs_wgs84, crs_layer, QgsProject.instance()
            )
            pt = tr.transform(QgsPointXY(item.lon, item.lat))
            layer.dataProvider().changeGeometryValues(
                {target_fid: QgsGeometry.fromPointXY(pt)}
            )

        layer.triggerRepaint()

    # ── Convenience wrappers (kept for backward compat) ────────────────────
    def _update_layer_geometry(self, item):
        self._update_layer_feature(item, update_geom=True,
                                   update_coords=False, update_direction=False)

    def _update_layer_coords(self, item):
        self._update_layer_feature(item, update_geom=False,
                                   update_coords=True, update_direction=False)

    def _update_layer_feature_direction(self, item):
        self._update_layer_feature(item, update_geom=False,
                                   update_coords=False, update_direction=True)

    def _write_exif_if_immediate(self, item):

        """Ask user whether to write EXIF GPS tags now.
        If the file already has GPS coordinates, show them and ask to confirm overwrite.
        Silently skips if no write engine is available.
        """
        if not item.has_position:
            return
        if not self._has_write_engine():
            self._set_status(
                f"Position saved in session only — no EXIF write engine available. "
                "Install ExifTool (⚙) or piexif to write to file."
            )
            return

        # Check for existing GPS EXIF
        existing = read_exif_gps(item.filepath)
        has_existing = existing and existing.get("lat") is not None

        alt_str = f"\n  Alt: {item.alt:.1f} m" if item.alt is not None else ""
        new_coords = (
            f"  Lat: {item.lat:.6f}\n"
            f"  Lon: {item.lon:.6f}{alt_str}"
        )

        if has_existing:
            ex_alt = f"\n  Alt: {existing['alt']:.1f} m" if existing.get("alt") is not None else ""
            old_coords = (
                f"  Lat: {existing['lat']:.6f}\n"
                f"  Lon: {existing['lon']:.6f}{ex_alt}"
            )
            msg = (
                f"⚠ {item.filename}\n"
                f"already has GPS coordinates:\n\n"
                f"{old_coords}\n\n"
                f"Overwrite with:\n\n"
                f"{new_coords}"
            )
            title = "Overwrite existing GPS EXIF?"
        else:
            msg = (
                f"Write GPS coordinates to EXIF of:\n{item.filename}?\n\n"
                f"{new_coords}"
            )
            title = "Write EXIF"

        reply = QMessageBox.question(
            self, title, msg,
            QMessageBox.Yes | QMessageBox.No
        )
        if reply != QMessageBox.Yes:
            return
        ok = write_exif_gps(
            item.filepath, item.lat, item.lon, item.alt, item.datetime,
            direction=item.direction,
            author=getattr(item, "author", None) or None
        )
        if ok:
            self._set_status(f"EXIF written: {item.filename}")
        else:
            self._set_status(f"Warning: could not write EXIF for {item.filename}")

    def _paste_coords_from_clipboard(self):
        """
        Parse lat/lon from clipboard text and fill the spinboxes.
        Supported formats:
          43.123456, 11.654321
          43.123456 11.654321
          43°7'24.4"N 11°39'15.6"E
          POINT(11.654 43.123)          WKT — note lon first
          https://maps.google.com/?q=43.123,11.654
          https://maps.google.com/maps?q=43.123,11.654
          https://www.google.com/maps/@43.123,11.654,15z
        """
        import re
        cb = QApplication.clipboard()
        text = cb.text().strip()
        if not text:
            self._set_status("Clipboard is empty.")
            return

        lat, lon = None, None

        # Google Maps URL patterns
        # ?q=lat,lon  or  @lat,lon,zoom
        m = re.search(r'[?&]q=(-?\d+\.\d+),(-?\d+\.\d+)', text)
        if m:
            lat, lon = float(m.group(1)), float(m.group(2))
        if lat is None:
            m = re.search(r'@(-?\d+\.\d+),(-?\d+\.\d+)', text)
            if m:
                lat, lon = float(m.group(1)), float(m.group(2))

        # WKT POINT(lon lat)
        if lat is None:
            m = re.search(r'POINT\s*\(\s*(-?\d+\.\d+)\s+(-?\d+\.\d+)\s*\)',
                          text, re.IGNORECASE)
            if m:
                lon, lat = float(m.group(1)), float(m.group(2))

        # DMS: 43°7'24.4"N 11°39'15.6"E
        if lat is None:
            dms = re.findall(
                r'(\d+)°\s*(\d+)\'\s*([\d.]+)"?\s*([NSns])\s*'
                r'(\d+)°\s*(\d+)\'\s*([\d.]+)"?\s*([EWew])',
                text
            )
            if dms:
                d = dms[0]
                lat = (float(d[0]) + float(d[1])/60 + float(d[2])/3600)
                if d[3].upper() == "S": lat = -lat
                lon = (float(d[4]) + float(d[5])/60 + float(d[6])/3600)
                if d[7].upper() == "W": lon = -lon

        # Plain decimal: "lat, lon" or "lat lon" (comma or space separated)
        if lat is None:
            m = re.search(
                r'(-?\d{1,3}\.\d+)\s*[,;\s]\s*(-?\d{1,3}\.\d+)', text
            )
            if m:
                a, b = float(m.group(1)), float(m.group(2))
                # Heuristic: lat is in [-90,90], lon in [-180,180]
                if -90 <= a <= 90 and -180 <= b <= 180:
                    lat, lon = a, b
                elif -90 <= b <= 90 and -180 <= a <= 180:
                    lat, lon = b, a

        if lat is None or lon is None:
            QMessageBox.warning(
                self, "Paste coordinates",
                f"Could not parse coordinates from clipboard:\n\n{text[:200]}"
            )
            return

        # Validate ranges
        if not (-90 <= lat <= 90) or not (-180 <= lon <= 180):
            QMessageBox.warning(
                self, "Paste coordinates",
                f"Values out of range: lat={lat:.6f}, lon={lon:.6f}"
            )
            return

        self.spin_manual_lat.setValue(lat)
        self.spin_manual_lon.setValue(lon)
        self._set_status(
            f"Coordinates pasted from clipboard: lat={lat:.6f}, lon={lon:.6f}"
        )

    def _go_prev(self):
        row = self.photo_list.currentRow()
        if row > 0:
            self.photo_list.setCurrentRow(row - 1)

    def _go_next(self):
        row = self.photo_list.currentRow()
        if row < self.photo_list.count() - 1:
            self.photo_list.setCurrentRow(row + 1)

    def _update_nav_state(self):
        total = self.photo_list.count()
        row   = self.photo_list.currentRow()
        if total == 0 or row < 0:
            self.btn_prev.setEnabled(False)
            self.btn_next.setEnabled(False)
            self.lbl_nav_index.setText("")
            return
        self.btn_prev.setEnabled(row > 0)
        self.btn_next.setEnabled(row < total - 1)
        self.lbl_nav_index.setText(f"{row + 1} / {total}")

    def keyPressEvent(self, event):
        if event.key() == Qt.Key_Left:
            self._go_prev()
        elif event.key() == Qt.Key_Right:
            self._go_next()
        else:
            super().keyPressEvent(event)

    def _zoom_to_selected(self, *_):
        item = self._selected_item
        if item is None or not item.has_position:
            self._set_status("No photo with position selected.")
            return
        crs_wgs84 = QgsCoordinateReferenceSystem("EPSG:4326")
        crs_map   = self.main_canvas.mapSettings().destinationCrs()
        transform = QgsCoordinateTransform(crs_wgs84, crs_map, QgsProject.instance())
        pt        = transform.transform(QgsPointXY(item.lon, item.lat))

        # Center the map on the point, then apply the scale from the widget
        self.main_canvas.setCenter(pt)
        scale = self.scale_main.scale()
        if scale and scale > 0:
            self.main_canvas.zoomScale(scale)
        else:
            # Fallback: fixed ~1:2000 view
            margin = 0.001
            self.main_canvas.setExtent(
                QgsRectangle(pt.x() - margin, pt.y() - margin,
                             pt.x() + margin, pt.y() + margin)
            )
        self.main_canvas.refresh()
        self._set_status(f"Zoomed to: {item.filename} ({item.lat:.6f}, {item.lon:.6f})")

    def _clear_all(self):
        # Disconnect layer and deselect features before clearing
        if self.btn_listen.isChecked():
            self.btn_listen.setChecked(False)  # triggers _disconnect_layer_listener
        self._clear_photos()
        self.track_points = []
        self._gpx_paths   = []
        self.gpx_list.clear()
        self.lbl_gpx_points.setText("Total track points: 0")
        self.lbl_gpx_info.setText("GPX: no tracks loaded")
        self._update_toolbar_state()
        self._update_nav_state()
        self._set_status("Cleared.")

    # ------------------------------------------------------------------ #
    #  Photo list helpers                                                  #
    # ------------------------------------------------------------------ #

    @staticmethod
    def _oriented_pixmap(filepath, max_w, max_h):
        """Load image with EXIF Orientation correction, scaled to max_w×max_h."""
        pix = QPixmap(filepath)
        if pix.isNull():
            return pix

        orientation = 1
        try:
            import piexif
            exif_data   = piexif.load(filepath)
            orientation = exif_data.get("0th", {}).get(
                piexif.ImageIFD.Orientation, 1) or 1
        except Exception:
            try:
                with open(filepath, "rb") as f:
                    data = f.read(65536)
                if data[:2] == b'\xff\xd8':
                    i = 2
                    while i < len(data) - 4:
                        if data[i:i+2] == b'\xff\xe1':
                            es     = i + 4
                            endian = '<' if data[es:es+2] == b'II' else '>'
                            ioff   = struct.unpack(endian+'I', data[es+4:es+8])[0]
                            ip     = es + ioff
                            n      = struct.unpack(endian+'H', data[ip:ip+2])[0]
                            for e in range(n):
                                ep = ip + 2 + e * 12
                                if struct.unpack(endian+'H', data[ep:ep+2])[0] == 0x0112:
                                    orientation = struct.unpack(
                                        endian+'H', data[ep+8:ep+10])[0]
                                    break
                            break
                        else:
                            i += 2 + struct.unpack('>H', data[i+2:i+4])[0]
            except Exception:
                pass

        t = QTransform()
        if   orientation == 2: t.scale(-1, 1)
        elif orientation == 3: t.rotate(180)
        elif orientation == 4: t.scale(1, -1)
        elif orientation == 5: t.rotate(90);  t.scale(-1, 1)
        elif orientation == 6: t.rotate(90)
        elif orientation == 7: t.rotate(-90); t.scale(-1, 1)
        elif orientation == 8: t.rotate(-90)

        if not t.isIdentity():
            pix = pix.transformed(t, Qt.SmoothTransformation)

        return pix.scaled(max_w, max_h, Qt.KeepAspectRatio, Qt.SmoothTransformation)

    def _add_list_item(self, photo_item):
        lw = QListWidgetItem()
        lw.setSizeHint(QSize(230, 38))
        lw.setData(Qt.UserRole, photo_item)
        # No thumbnail icon — compact text-only row
        self._refresh_item_text(lw, photo_item)
        photo_item.list_item = lw
        self.photo_list.addItem(lw)

    def _refresh_item_text(self, lw, photo_item):
        from qgis.PyQt.QtGui import QBrush
        emoji = {"gpx": "✅", "matched": "✅", "manual": "📌",
                 "exif": "📷", "skipped": "❌", "pending": "⏳"
                 }.get(photo_item.source, "⏳")
        dt  = photo_item.datetime.strftime("%d/%m %H:%M") if photo_item.datetime else "—"
        # Compact single line: emoji  filename  |  date  |  lat, lon
        if photo_item.has_position:
            pos_str = f"  {photo_item.lat:.5f}, {photo_item.lon:.5f}"
        else:
            pos_str = ""
        lw.setText(f"{emoji}  {photo_item.filename}   {dt}{pos_str}")
        # Row foreground color encodes geotag status
        color_map = {
            "gpx":     QColor(20, 140, 60),
            "matched": QColor(20, 140, 60),
            "exif":    QColor(100, 50, 160),
            "manual":  QColor(30, 100, 190),
            "skipped": QColor(180, 40, 40),
            "pending": QColor(100, 100, 100),
        }
        lw.setForeground(QBrush(color_map.get(photo_item.source, QColor(80, 80, 80))))

    def _update_list_item(self, photo_item):
        if photo_item.list_item:
            self._refresh_item_text(photo_item.list_item, photo_item)

    def _clear_photos(self):
        self.btn_open_folder.setEnabled(False)
        for rb in self._rubber_bands.values():
            try:
                rb.reset(QgsWkbTypes.PointGeometry)
                scene = self.main_canvas.scene()
                if scene:
                    scene.removeItem(rb)
            except Exception:
                pass
        self._rubber_bands.clear()
        self.photo_list.clear()
        self.photo_items.clear()
        self._selected_item = None
        self._clear_info_panel()
        self._update_counts()

    def _update_counts(self):
        total   = len(self.photo_items)
        matched = sum(1 for it in self.photo_items if it.has_position)
        skipped = sum(1 for it in self.photo_items if it.source == "skipped")
        self.lbl_counts.setText(
            f"Total: {total} | Matched: {matched} | Skipped: {skipped}"
        )

    # ------------------------------------------------------------------ #
    #  Rubber bands on main_canvas                                        #
    # ------------------------------------------------------------------ #

    def _draw_rubber_band(self, photo_item):
        if not photo_item.has_position:
            return
        old = self._rubber_bands.get(photo_item.filepath)
        if old:
            try:
                old.reset(QgsWkbTypes.PointGeometry)
                scene = self.main_canvas.scene()
                if scene:
                    scene.removeItem(old)
            except Exception:
                pass
        rb = QgsRubberBand(self.main_canvas, QgsWkbTypes.PointGeometry)
        stroke_color = {
            "gpx":    QColor(39, 174,  96, 220),
            "manual": QColor(41, 128, 185, 220),
            "exif":   QColor(142, 68, 173, 220),
        }.get(photo_item.source, QColor(150, 150, 150, 180))
        rb.setColor(stroke_color)            # stroke colour
        rb.setFillColor(QColor(0, 0, 0, 0))  # transparent fill
        rb.setWidth(2)                        # stroke width px
        rb.setIconSize(12)
        rb.setIcon(QgsRubberBand.ICON_CIRCLE)
        crs_wgs84 = QgsCoordinateReferenceSystem("EPSG:4326")
        transform = QgsCoordinateTransform(
            crs_wgs84,
            self.main_canvas.mapSettings().destinationCrs(),
            QgsProject.instance()
        )
        rb.addPoint(transform.transform(QgsPointXY(photo_item.lon, photo_item.lat)), True)
        self._rubber_bands[photo_item.filepath] = rb
        self.main_canvas.refresh()

    def _draw_all_rubber_bands(self):
        for item in self.photo_items:
            if item.has_position:
                self._draw_rubber_band(item)

    # ------------------------------------------------------------------ #
    #  Selection highlight rubber band                                    #
    # ------------------------------------------------------------------ #

    def _draw_highlight(self, photo_item):
        """Draw a prominent orange circle around the selected photo point."""
        self._clear_highlight()
        if not photo_item or not photo_item.has_position:
            return
        rb = QgsRubberBand(self.main_canvas, QgsWkbTypes.PointGeometry)
        # Outer ring: thick orange circle
        rb.setColor(QColor(255, 140, 0, 220))
        rb.setFillColor(QColor(255, 140, 0, 0))   # transparent fill
        rb.setWidth(3)
        rb.setIconSize(22)
        rb.setIcon(QgsRubberBand.ICON_CIRCLE)
        crs_wgs84 = QgsCoordinateReferenceSystem("EPSG:4326")
        transform = QgsCoordinateTransform(
            crs_wgs84,
            self.main_canvas.mapSettings().destinationCrs(),
            QgsProject.instance()
        )
        pt = transform.transform(QgsPointXY(photo_item.lon, photo_item.lat))
        rb.addPoint(pt, True)
        self._highlight_rb = rb
        self.main_canvas.refresh()

    def _clear_highlight(self):
        """Remove the selection highlight rubber band."""
        if self._highlight_rb:
            try:
                self._highlight_rb.reset(QgsWkbTypes.PointGeometry)
                scene = self.main_canvas.scene()
                if scene:
                    scene.removeItem(self._highlight_rb)
            except Exception:
                pass
            self._highlight_rb = None

    # ------------------------------------------------------------------ #
    #  Selection / info panel                                              #
    # ------------------------------------------------------------------ #

    def _on_selection_changed(self, current, previous):
        if current is None:
            self._selected_item = None
            self._clear_info_panel()
            self.btn_edit_point.setEnabled(False)
            self.btn_apply_coords.setEnabled(False)
            self.btn_direction.setEnabled(False)
            self._clear_highlight()
            self._update_nav_state()
            return
        photo_item = current.data(Qt.UserRole)
        self._selected_item = photo_item
        self._update_info_panel(photo_item)
        self.btn_edit_point.setEnabled(True)
        self.btn_apply_coords.setEnabled(True)
        self.btn_direction.setEnabled(photo_item.has_position)
        self._draw_highlight(photo_item)
        # Pre-fill manual spinboxes with current position if available
        self._prefill_manual_coords(photo_item)
        self._update_nav_state()

    def _on_multi_selection_changed(self):
        """Called when the selection changes (including multi-select).
        Updates Apply button state and info panel for multi-selection.
        """
        selected = self.photo_list.selectedItems()
        count = len(selected)
        if count <= 1:
            return  # handled by currentItemChanged
        # Multiple items selected: show count in info panel
        self.lbl_photo_name.setText(f"{count} photos selected")
        self.lbl_photo_source.setText("Source: —")
        self.lbl_photo_author.setText("Author: —")
        self.lbl_photo_dt.setText("Date/Time:\n—")
        self.lbl_photo_alt.setText("Altitude: —")
        self.lbl_photo_direction.setText("Direction: —")
        self.lbl_photo_camera.setText("Camera: —")
        self.lbl_photo_hfov.setText("FL/HFOV: —")
        self.btn_open_ext.setEnabled(False)
        self.btn_edit_point.setEnabled(True)   # sequential click mode for multi
        self.btn_apply_coords.setEnabled(True)  # spinbox coords -> all selected
        self.btn_direction.setEnabled(True)

    def _update_info_panel(self, item):

        self.lbl_photo_name.setText(item.filename)
        self.btn_open_ext.setEnabled(os.path.isfile(item.filepath))
        # Source (below filename)
        self.lbl_photo_source.setText(
            "Source: " + {
                "gpx":     "GPX (interpolated)",
                "manual":  "Manual",
                "exif":    "Existing EXIF",
                "pending": "Not processed",
                "skipped": "Not matched",
            }.get(item.source, item.source)
        )
        # Author
        author = getattr(item, "author", "") or "\u2014"
        self.lbl_photo_author.setText(f"Author: {author}")
        # Date/Time
        self.lbl_photo_dt.setText(
            f"Date/Time:\n{item.datetime.strftime('%d/%m/%Y %H:%M:%S')}"
            if item.datetime else "Date/Time:\n\u2014"
        )
        # Altitude: use "is not None" to correctly show 0.0 m
        self.lbl_photo_alt.setText(
            f"Altitude: {item.alt:.1f} m" if item.alt is not None else "Altitude: \u2014"
        )
        # Direction
        self.lbl_photo_direction.setText(
            f"Direction: {item.direction:.1f}\u00b0" if item.direction is not None else "Direction: \u2014"
        )
        # Camera model
        cam_parts = [p.strip() for p in [item.make, item.model] if p and p.strip()]
        cam_str = " ".join(cam_parts) if cam_parts else "—"
        self.lbl_photo_camera.setText(f"Camera: {cam_str}")
        # HFOV / focal length
        if item.hfov is not None:
            fl_str = ""
            if item.focal_length_35mm:
                fl_str = f"{item.focal_length_35mm:.0f}mm eq."
            elif item.focal_length:
                fl_str = f"{item.focal_length:.1f}mm"
            self.lbl_photo_hfov.setText(
                f"FL: {fl_str}  HFOV: {item.hfov:.1f}°"
            )
        else:
            self.lbl_photo_hfov.setText("FL/HFOV: —")
        pix = self._oriented_pixmap(
            item.filepath,
            self.photo_preview.width()  or PREVIEW_W,
            self.photo_preview.height() or PREVIEW_H,
        )
        if not pix.isNull():
            self.photo_preview.setPixmap(pix)
        else:
            self.photo_preview.setText("Preview not available")

    def _clear_info_panel(self):
        self.lbl_photo_name.setText("\u2014")
        self.lbl_photo_source.setText("Source: \u2014")
        self.lbl_photo_author.setText("Author: \u2014")
        self.lbl_photo_dt.setText("Date/Time:\n\u2014")
        self.lbl_photo_alt.setText("Altitude: \u2014")
        self.lbl_photo_direction.setText("Direction: \u2014")
        self.lbl_photo_camera.setText("Camera: \u2014")
        self.lbl_photo_hfov.setText("FL/HFOV: \u2014")
        self.btn_open_ext.setEnabled(False)
        self.photo_preview.setText("No photo selected")
        self.photo_preview.setPixmap(QPixmap())

    # ------------------------------------------------------------------ #
    #  Misc                                                                #
    # ------------------------------------------------------------------ #

    def _update_toolbar_state(self):
        has_photos  = bool(self.photo_items)
        has_gpx     = bool(self._gpx_paths)
        has_matched = any(it.has_position for it in self.photo_items)
        has_et      = bool(find_exiftool())
        can_geotag  = has_photos and has_gpx and has_et
        self.act_run.setEnabled(can_geotag)
        self.btn_geotag.setEnabled(can_geotag)
        tip = (
            "Match photos to GPX trackpoints by timestamp and assign GPS coordinates."
            if has_et else
            "GPX geotag requires ExifTool — install it via ⚙ ExifTool in the toolbar."
        )
        self.act_run.setToolTip(tip)
        self.btn_geotag.setToolTip(tip)
        can_write = has_matched and self._has_write_engine()
        self.act_export_gpkg.setEnabled(has_matched)
        # Symbology + Author: need has_layer
        has_layer   = self._listened_layer is not None and self._listened_layer.isValid()
        has_photos  = bool(self.photo_items)
        self.btn_apply_symbology.setEnabled(has_layer)

        # Author assignment: enabled if photos loaded OR layer connected
        can_authors = has_photos or has_layer
        self.btn_assign_authors.setEnabled(can_authors)
        self.btn_assign_authors.setToolTip(
            "Assign authors to photos grouped by date or camera.\n"
            "Works on loaded photos and/or connected layer.\n"
            "Items that already have an author are not modified."
            if can_authors else
            "Load photos or connect a layer first."
        )

    def _refresh_write_controls(self):
        """Re-enable or disable all write-EXIF controls based on current engine."""
        can_write = self._has_write_engine()
        has_matched = any(it.has_position for it in self.photo_items)
        self.act_write_exif.setEnabled(has_matched and can_write)
        self.act_write_exif.setToolTip(
            "Write GPS coordinates to EXIF tags of all matched photos"
            if can_write else
            "Write EXIF unavailable — install ExifTool (⚙) or piexif"
        )
        # btn_apply_coords: keep enabled for session-only coord assignment
        # but update tooltip
        has_selection = self._selected_item is not None
        self.btn_apply_coords.setEnabled(has_selection)
        self.btn_apply_coords.setToolTip(
            "Assign the lat/lon/alt values to the selected photo.\n"
            "A confirmation dialog will ask whether to write EXIF immediately."
            if can_write else
            "Applies coordinates to session only "
            "(install ExifTool or piexif to enable EXIF writing)"
        )

    def _update_engine_label(self):
        """Aggiorna la label motore EXIF leggendo direttamente
        il filesystem — non dipende dalla cache di modulo.
        """
        import sys as _sys
        import subprocess as _sp
        from qgis.core import QgsMessageLog, Qgis
        from ..core.exiftool_manager import vendor_dir
        from ..core.exif_handler import HAS_PIEXIF
        from ..core import exif_handler as _eh

        vdir    = vendor_dir()
        fname   = "exiftool.exe" if _sys.platform.startswith("win") else "exiftool"
        et_path = os.path.join(vdir, fname)

        QgsMessageLog.logMessage(
            f"GeotagManager _update_engine_label:\n"
            f"  vendor_dir = {vdir}\n"
            f"  et_path    = {et_path}\n"
            f"  exists     = {os.path.isfile(et_path)}",
            "GeotagManager", Qgis.Info
        )

        if os.path.isfile(et_path):
            try:
                r = _sp.run(
                    [et_path, "-ver"],
                    capture_output=True, text=True, timeout=8
                )
                QgsMessageLog.logMessage(
                    f"  -ver returncode = {r.returncode}\n"
                    f"  stdout = {r.stdout.strip()!r}\n"
                    f"  stderr = {r.stderr.strip()[:200]!r}",
                    "GeotagManager", Qgis.Info
                )
                if r.returncode == 0:
                    ver = r.stdout.strip()
                    _eh._EXIFTOOL_CACHE = et_path
                    eng   = f"ExifTool {ver} (bundled)"
                    color = "#27ae60"
                    self.lbl_engine.setText(f"EXIF: {eng}")
                    self.lbl_engine.setStyleSheet(
                        f"font-size:10px; color:{color}; padding: 0 6px;")
                    return
            except Exception as _ex:
                QgsMessageLog.logMessage(
                    f"  ECCEZIONE subprocess: {_ex}",
                    "GeotagManager", Qgis.Warning
                )

        # ExifTool not available
        _eh._EXIFTOOL_CACHE = False
        if HAS_PIEXIF:
            eng = "piexif (JPEG only)"
        else:
            eng = "Pure Python (built-in)"
        self.lbl_engine.setText(f"EXIF: {eng} — install ExifTool")
        self.lbl_engine.setStyleSheet(
            "font-size:10px; color:#e67e22; padding: 0 6px;")

    def _set_status(self, msg):
        self.status_bar.showMessage(msg)

    def _toggle_log_panel(self, checked):
        """Show or hide the floating log panel."""
        if checked:
            if self._log_panel is None:
                from .log_panel import LogPanel
                self._log_panel = LogPanel(self)
                self._log_panel.finished.connect(
                    lambda: self.act_log.setChecked(False)
                )
            self._log_panel.show()
            self._log_panel.raise_()
        else:
            if self._log_panel:
                self._log_panel.hide()

    def _apply_layer_symbology(self):
        """Apply GeotagManager symbology to the connected layer,
        then open Layer Properties so the user can customise or save as QML.
        """
        layer = self._listened_layer
        if not layer or not layer.isValid():
            return
        try:
            from ..core.layer_symbology import apply_photo_symbology
            ok = apply_photo_symbology(layer)
            if ok:
                self._set_status(
                    f"Symbology applied to: {layer.name()}. "
                    "Open Layer Properties to customise or save as QML."
                )
                # Open Layer Properties on Symbology tab
                self.iface.showLayerPropertiesDialog(layer)
            else:
                self._set_status("Symbology could not be applied.")
        except Exception as _e:
            QgsMessageLog.logMessage(
                f"GeotagManager symbology error: {_e}",
                "GeotagManager", Qgis.Warning)
            self._set_status(f"Symbology error: {_e}")

    def _open_author_dialog(self):

        """Open the Author Assignment dialog.
        Works on session photos and/or connected layer.
        """
        if not self.photo_items and not self._listened_layer:
            return
        from .author_dialog import AuthorDialog
        dlg = AuthorDialog(
            photo_items=self.photo_items,
            listened_layer=self._listened_layer,
            parent=self
        )
        dlg.exec_()
        # Refresh list items to reflect updated authors
        for item in self.photo_items:
            self._update_list_item(item)

    def _open_help(self):
        """Open README.md from the plugin folder in the default browser/editor."""
        import subprocess, sys
        plugin_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        md_path = os.path.join(plugin_dir, "README.md")
        if not os.path.isfile(md_path):
            QMessageBox.warning(
                self, "Help",
                f"README.md not found in plugin folder:\n{plugin_dir}"
            )
            return
        try:
            if sys.platform.startswith("win"):
                os.startfile(md_path)
            elif sys.platform.startswith("darwin"):
                subprocess.Popen(["open", md_path])
            else:
                subprocess.Popen(["xdg-open", md_path])
        except Exception as e:
            QgsMessageLog.logMessage(
                f"GeotagManager: cannot open help file: {e}",
                "GeotagManager", Qgis.Warning)
            self._set_status(f"Cannot open help: {e}")

    def _open_photo_folder(self):

        """Open the photo folder in the system file manager."""
        import subprocess, sys
        folder = self._photo_folder
        if not folder:
            self._set_status("No photo folder loaded.")
            return
        folder = os.path.abspath(folder)
        if not os.path.isdir(folder):
            self._set_status(f"Folder not found: {folder}")
            self.btn_open_folder.setEnabled(False)
            return
        try:
            if sys.platform.startswith("win"):
                # Use explorer.exe directly — more reliable than os.startfile
                subprocess.Popen(["explorer", os.path.normpath(folder)])
            elif sys.platform.startswith("darwin"):
                subprocess.Popen(["open", folder])
            else:
                subprocess.Popen(["xdg-open", folder])
            self._set_status(f"Opened: {folder}")
        except Exception as e:
            self._set_status(f"Cannot open folder: {e}")
            QgsMessageLog.logMessage(
                f"GeotagManager open folder error: {e}",
                "GeotagManager", Qgis.Warning)

    def _open_photo_external(self):
        """Apre la foto selezionata nell'applicazione predefinita del sistema."""
        if self._selected_item is None:
            return
        fpath = self._selected_item.filepath
        if not os.path.isfile(fpath):
            self._set_status(f"File non trovato: {fpath}")
            return
        import subprocess, sys
        try:
            if sys.platform.startswith("win"):
                os.startfile(fpath)
            elif sys.platform.startswith("darwin"):
                subprocess.Popen(["open", fpath])
            else:
                subprocess.Popen(["xdg-open", fpath])
        except Exception as e:
            self._set_status(f"Impossibile aprire la foto: {e}")

    def _toggle_photo_fullscreen(self, checked):
        """
        Modalita foto a schermo intero:
          - checked=True:  nasconde il pannello sinistro e la controls bar,
                           massimizza il riquadro preview + nav + info
          - checked=False: ripristina il layout normale
        """
        # Il widget root del layout principale e il QSplitter
        # L'indice 0 e il left panel, 1 e il right panel
        # La controls bar e un widget separato nella root VBox
        from qgis.PyQt.QtWidgets import QSplitter
        # Cerca il pannello sinistro (indice 0 nello splitter)
        splitter = None
        controls_bar = None
        for child in self.findChildren(QSplitter):
            if child.count() >= 2:
                splitter = child
                break
        # Cerca la controls bar (QWidget con spin_offset)
        from qgis.PyQt.QtWidgets import QWidget
        if hasattr(self, "spin_offset"):
            # risali al widget padre della spin_offset
            controls_bar = self.spin_offset.parent()
            while controls_bar and not isinstance(controls_bar.parent(), type(self)):
                controls_bar = controls_bar.parent()

        if checked:
            # Nasconde il pannello sinistro
            if splitter:
                splitter.widget(0).setVisible(False)
            # Nasconde la controls bar
            if controls_bar:
                controls_bar.setVisible(False)
            self.act_fullscreen_photo.setText("✖ Esci")
        else:
            # Ripristina tutto
            if splitter:
                splitter.widget(0).setVisible(True)
            if controls_bar:
                controls_bar.setVisible(True)
            self.act_fullscreen_photo.setText("⛶ Photo")

    def _setup_layer_open_action(self, layer):
        """
        Aggiunge al layer una QgsAction "Open photo" che apre il file
        indicato nel campo filepath con l'applicazione predefinita.
        Viene aggiunta solo se almeno una feature ha un filepath valido.
        Evita duplicati rimuovendo l'eventuale action preesistente.
        """
        if layer is None or not layer.isValid():
            return
        import sys as _sys
        field_names = [f.name() for f in layer.fields()]
        if "filepath" not in field_names:
            return
        # Verifica che almeno una feature abbia un filepath esistente su disco
        has_valid = False
        for feat in layer.getFeatures():
            fp = str(feat["filepath"] or "").strip()
            if fp and os.path.isfile(fp):
                has_valid = True
                break
        if not has_valid:
            return
        action_name = "Open photo"
        mgr = layer.actions()
        # Rimuovi duplicati
        for act in mgr.actions():
            if act.name() == action_name:
                mgr.removeAction(act.id())
                break
        # Comando in base alla piattaforma
        if _sys.platform.startswith("win"):
            command = 'import os; fp=r"""[% filepath %]"""; os.startfile(fp) if os.path.isfile(fp) else None'
            action_type = QgsAction.GenericPython
        elif _sys.platform.startswith("darwin"):
            command = 'open "[% filepath %]"'
            action_type = QgsAction.Unix
        else:
            command = 'xdg-open "[% filepath %]"'
            action_type = QgsAction.Unix
        action = QgsAction(action_type, action_name, command, "", False)
        action.setActionScopes({"Feature", "Field"})
        mgr.addAction(action)
        self._set_status(f"Action 'Apri foto' aggiunta al layer '{layer.name()}'")

    def _open_exiftool_setup(self):
        """Apre il wizard di installazione ExifTool bundled."""
        from ..core.exiftool_manager import ExifToolWizard
        wizard = ExifToolWizard(self)
        wizard.exec_()
        # Rescan dopo chiusura wizard — potrebbe essere stato installato
        # Update engine label and controls
        self._update_engine_label()
        self._refresh_write_controls()
        self._update_toolbar_state()

    def _open_batch_scan(self):
        """Open the standalone Batch Scan window."""
        if self._scan_dialog is None or not self._scan_dialog.isVisible():
            from .scan_dialog import ScanDialog
            self._scan_dialog = ScanDialog(self.iface, self)
        self._scan_dialog.show()
        self._scan_dialog.raise_()
        self._scan_dialog.activateWindow()

    def closeEvent(self, event):
        try:
            QgsProject.instance().layerWillBeRemoved.disconnect(
                self._on_project_layers_removed)
        except Exception:
            pass
        self._disconnect_layer_listener()
        self._clear_highlight()
        if self._scan_dialog:
            self._scan_dialog.close()
        if self._log_panel:
            self._log_panel.destroy_panel()
            self._log_panel = None
        if self._point_tool:
            self.main_canvas.unsetMapTool(self._point_tool)
            if self._prev_map_tool:
                self.main_canvas.setMapTool(self._prev_map_tool)
        if self._worker_thread and self._worker_thread.isRunning():
            if self._worker:
                self._worker.cancel()
            self._worker_thread.quit()
            if not self._worker_thread.wait(2000):
                self._worker_thread.terminate()
        for rb in self._rubber_bands.values():
            try:
                rb.reset(QgsWkbTypes.PointGeometry)
                scene = self.main_canvas.scene()
                if scene:
                    scene.removeItem(rb)
            except Exception:
                pass
        self._rubber_bands.clear()
        try:
            self.main_canvas.refresh()
        except Exception:
            pass
        super().closeEvent(event)
