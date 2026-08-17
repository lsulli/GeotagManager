# -*- coding: utf-8 -*-
"""
scan_panel.py - Batch Scan panel widget.

Reusable widget that can be embedded in a QTabWidget (variant A)
or shown as a standalone QDialog (variant C).
"""

import os

from qgis.PyQt.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QGroupBox,
    QPushButton, QLabel, QLineEdit, QCheckBox,
    QComboBox, QProgressBar, QFileDialog, QMessageBox,
    QListWidget, QListWidgetItem, QSplitter, QFrame,
    QApplication, QAbstractItemView,
)
from qgis.PyQt.QtCore import Qt, QThread, QSize, pyqtSlot
from qgis.PyQt.QtGui import QColor, QBrush, QFont

from qgis.core import QgsProject

from ..core.batch_scan import (
    BatchScanWorker, list_images_recursive, export_records,
    ALL_EXTENSIONS, DEFAULT_EXTENSIONS,
    EXPORT_FORMATS, EXPORT_EXTS,
)


EXT_GROUPS = {
    'JPEG only':        {'.jpg', '.jpeg'},
    'JPEG + TIFF':      {'.jpg', '.jpeg', '.tif', '.tiff'},
    'JPEG + TIFF + PNG':{'.jpg', '.jpeg', '.tif', '.tiff', '.png'},
    'RAW only':         {'.cr2','.cr3','.nef','.nrw','.arw','.srf',
                         '.sr2','.orf','.rw2','.dng','.raf','.pef','.ptx'},
    'JPEG + RAW':       {'.jpg','.jpeg','.cr2','.cr3','.nef','.nrw',
                         '.arw','.srf','.sr2','.orf','.rw2','.dng',
                         '.raf','.pef','.ptx'},
    'All supported':    ALL_EXTENSIONS,
}


class ScanPanel(QWidget):
    """
    Self-contained batch scan panel.
    Embed in a tab or wrap in a QDialog.
    """

    def __init__(self, iface, parent=None):
        super().__init__(parent)
        self.iface        = iface
        self._worker      = None
        self._thread      = None
        self._records     = []
        self._folder      = ''

        self._build_ui()

    # ------------------------------------------------------------------ #
    #  UI                                                                  #
    # ------------------------------------------------------------------ #

    def _build_ui(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(6, 6, 6, 6)
        root.setSpacing(6)

        root.addWidget(self._build_source_group())
        root.addWidget(self._build_options_group())
        root.addWidget(self._build_run_bar())

        splitter = QSplitter(Qt.Vertical)
        splitter.addWidget(self._build_results_list())
        splitter.addWidget(self._build_stats_bar())
        splitter.setSizes([300, 60])
        root.addWidget(splitter, stretch=1)

        root.addWidget(self._build_export_group())

        self.progress_bar = QProgressBar()
        self.progress_bar.setVisible(False)
        self.progress_bar.setFixedHeight(16)
        root.addWidget(self.progress_bar)

        self.lbl_status = QLabel("Ready.")
        self.lbl_status.setStyleSheet("color:#555; font-size:10px;")
        root.addWidget(self.lbl_status)

    def _build_source_group(self):
        grp = QGroupBox("Source folder")
        lay = QHBoxLayout(grp)

        self.edit_folder = QLineEdit()
        self.edit_folder.setPlaceholderText("Select root folder…")
        self.edit_folder.setReadOnly(True)
        lay.addWidget(self.edit_folder)

        btn = QPushButton("Browse…")
        btn.setFixedWidth(80)
        btn.clicked.connect(self._browse_folder)
        lay.addWidget(btn)

        self.chk_recursive = QCheckBox("Recursive (include subdirectories)")
        self.chk_recursive.setChecked(True)
        lay.addWidget(self.chk_recursive)
        return grp

    def _build_options_group(self):
        grp = QGroupBox("Options")
        lay = QHBoxLayout(grp)

        lay.addWidget(QLabel("File types:"))
        self.combo_exts = QComboBox()
        for label in EXT_GROUPS:
            self.combo_exts.addItem(label, EXT_GROUPS[label])
        self.combo_exts.setCurrentIndex(1)   # JPEG + TIFF default
        self.combo_exts.setMinimumWidth(160)
        lay.addWidget(self.combo_exts)

        lay.addSpacing(20)
        lay.addWidget(QLabel("Parallel workers:"))
        self.combo_workers = QComboBox()
        for n in [2, 4, 8, 16]:
            self.combo_workers.addItem(str(n), n)
        self.combo_workers.setCurrentIndex(1)   # 4 default
        lay.addWidget(self.combo_workers)

        lay.addSpacing(20)
        lay.addWidget(QLabel("Author:"))
        self.edit_author = QLineEdit()
        self.edit_author.setPlaceholderText("e.g. Mario Rossi (global)")
        self.edit_author.setFixedWidth(140)
        self.edit_author.setToolTip(
            "Global author applied to all records without an author.\n"
            "Use 'Assign authors' to assign per date or camera."
        )
        lay.addWidget(self.edit_author)

        self.btn_batch_authors = QPushButton("👤 Assign authors...")
        self.btn_batch_authors.setFixedHeight(24)
        self.btn_batch_authors.setEnabled(False)
        self.btn_batch_authors.setToolTip(
            "Assign authors to scanned records grouped by date or camera.\n"
            "Records that already have an author are not modified."
        )
        self.btn_batch_authors.clicked.connect(self._open_batch_author_dialog)
        lay.addWidget(self.btn_batch_authors)

        lay.addStretch()
        return grp

    def _build_run_bar(self):
        bar = QWidget()
        lay = QHBoxLayout(bar)
        lay.setContentsMargins(0, 0, 0, 0)

        self.btn_scan = QPushButton("▶  Scan")
        self.btn_scan.setFixedHeight(30)
        self.btn_scan.setEnabled(False)
        self.btn_scan.clicked.connect(self._start_scan)
        lay.addWidget(self.btn_scan)

        self.btn_cancel = QPushButton("■  Cancel")
        self.btn_cancel.setFixedHeight(30)
        self.btn_cancel.setEnabled(False)
        self.btn_cancel.clicked.connect(self._cancel_scan)
        lay.addWidget(self.btn_cancel)

        lay.addStretch()
        return bar

    def _build_results_list(self):
        self.result_list = QListWidget()
        self.result_list.setUniformItemSizes(True)
        self.result_list.setFont(QFont("Consolas, monospace", 8))
        self.result_list.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.result_list.setToolTip("Geotagged photos found during scan")
        return self.result_list

    def _build_stats_bar(self):
        frame = QFrame()
        frame.setFrameStyle(QFrame.StyledPanel)
        lay = QHBoxLayout(frame)
        lay.setContentsMargins(8, 4, 8, 4)

        self.lbl_stat_total    = self._stat_label("Scanned: 0")
        self.lbl_stat_geotagged= self._stat_label("Geotagged: 0", "#27ae60")
        self.lbl_stat_skipped  = self._stat_label("No GPS: 0", "#e67e22")
        self.lbl_stat_cameras  = self._stat_label("Cameras: —")

        lay.addWidget(self.lbl_stat_total)
        lay.addWidget(QLabel(" | "))
        lay.addWidget(self.lbl_stat_geotagged)
        lay.addWidget(QLabel(" | "))
        lay.addWidget(self.lbl_stat_skipped)
        lay.addWidget(QLabel(" | "))
        lay.addWidget(self.lbl_stat_cameras)
        lay.addStretch()
        return frame

    def _stat_label(self, text, color="#333"):
        lbl = QLabel(text)
        lbl.setStyleSheet(f"font-weight:bold; color:{color}; font-size:11px;")
        return lbl

    def _build_export_group(self):
        grp = QGroupBox("Export")
        lay = QHBoxLayout(grp)

        lay.addWidget(QLabel("Format:"))
        self.combo_fmt = QComboBox()
        for label in EXPORT_FORMATS:
            self.combo_fmt.addItem(label)
        self.combo_fmt.setCurrentIndex(0)
        lay.addWidget(self.combo_fmt)

        self.btn_export = QPushButton("💾  Export…")
        self.btn_export.setFixedHeight(28)
        self.btn_export.setEnabled(False)
        self.btn_export.clicked.connect(self._export)
        lay.addWidget(self.btn_export)

        self.chk_load_layer = QCheckBox("Load in QGIS project after export")
        self.chk_load_layer.setChecked(True)
        lay.addWidget(self.chk_load_layer)

        self.chk_apply_symbology = QCheckBox("Apply layer symbology")
        self.chk_apply_symbology.setChecked(True)
        self.chk_apply_symbology.setToolTip(
            "Apply GeotagManager symbology after loading the layer:\n"
            "  • Green dot  = photo file exists\n"
            "  • Red circle = photo file missing\n"
            "  • Blue wedge = camera FOV (hfov + direction)"
        )
        lay.addWidget(self.chk_apply_symbology)

        lay.addStretch()
        return grp

    # ------------------------------------------------------------------ #
    #  Actions                                                             #
    # ------------------------------------------------------------------ #

    def _browse_folder(self):
        folder = QFileDialog.getExistingDirectory(
            self, "Select root photo folder", ""
        )
        if folder:
            self._folder = folder
            self.edit_folder.setText(folder)
            self.btn_scan.setEnabled(True)

    def _start_scan(self):
        if not self._folder:
            return

        self.result_list.clear()
        self._records = []
        self.btn_scan.setEnabled(False)
        self.btn_cancel.setEnabled(True)
        self.btn_export.setEnabled(False)
        self.btn_batch_authors.setEnabled(False)
        self.progress_bar.setVisible(True)
        self.progress_bar.setValue(0)

        exts    = self.combo_exts.currentData()
        workers = self.combo_workers.currentData()
        author  = self.edit_author.text().strip()
        recursive = self.chk_recursive.isChecked()

        self._set_status("Building file list…")
        QApplication.processEvents()

        image_paths = list_images_recursive(
            self._folder, extensions=exts, recursive=recursive
        )

        if not image_paths:
            QMessageBox.information(
                self, "Batch Scan", "No matching images found in the selected folder."
            )
            self._reset_run_bar()
            return

        self.progress_bar.setRange(0, len(image_paths))
        self._set_status(f"Found {len(image_paths)} files — scanning EXIF…")

        self._worker = BatchScanWorker(
            image_paths, self._folder,
            author=author, max_workers=workers
        )
        self._thread = QThread()
        self._worker.moveToThread(self._thread)
        self._thread.started.connect(self._worker.run)
        self._worker.progress.connect(self._on_progress)
        self._worker.record_ready.connect(self._on_record)
        self._worker.finished.connect(self._on_finished)
        self._worker.error.connect(self._on_error)
        self._thread.start()

    def _cancel_scan(self):
        if self._worker:
            self._worker.cancel()
        self._set_status("Cancelling…")

    @pyqtSlot(int, int, str)
    def _on_progress(self, done, total, path):
        self.progress_bar.setValue(done)
        self._set_status(
            f"Scanning {done}/{total}: {os.path.basename(path)}"
        )
        QApplication.processEvents()

    @pyqtSlot(dict)
    def _on_record(self, rec):
        fname = rec.get('filename', '')
        dt    = rec.get('datetime_photo', '')[:10]
        cam   = rec.get('camera', '')
        lat   = rec.get('lat', 0)
        lon   = rec.get('lon', 0)
        txt   = f"📍 {fname}  |  {dt}  |  {cam}  |  {lat:.5f}, {lon:.5f}"
        item  = QListWidgetItem(txt)
        item.setData(Qt.UserRole, rec)
        self.result_list.addItem(item)
        self._records.append(rec)
        self._update_stats_live()

    @pyqtSlot(list, int, int)
    def _on_finished(self, records, total_scanned, total_skipped):
        self._thread.quit()
        self._thread.wait()
        self._records = records
        self._reset_run_bar()
        self.btn_export.setEnabled(bool(records))
        self.btn_batch_authors.setEnabled(bool(records))
        self.progress_bar.setVisible(False)
        self._update_stats_final(total_scanned, total_skipped)
        self._set_status(
            f"Done: {len(records)} geotagged / {total_scanned} scanned"
        )

    @pyqtSlot(str)
    def _on_error(self, msg):
        self._thread.quit()
        self._thread.wait()
        self._reset_run_bar()
        QMessageBox.critical(self, "Scan error", msg)

    def _reset_run_bar(self):
        self.btn_scan.setEnabled(bool(self._folder))
        self.btn_cancel.setEnabled(False)

    # ------------------------------------------------------------------ #
    #  Stats                                                               #
    # ------------------------------------------------------------------ #

    def _update_stats_live(self):
        n = len(self._records)
        self.lbl_stat_geotagged.setText(f"Geotagged: {n}")

    def _update_stats_final(self, total_scanned, total_skipped):
        n = len(self._records)
        cameras = set(r.get('camera','') for r in self._records if r.get('camera'))
        self.lbl_stat_total.setText(f"Scanned: {total_scanned}")
        self.lbl_stat_geotagged.setText(f"Geotagged: {n}")
        self.lbl_stat_skipped.setText(f"No GPS: {total_skipped}")
        cam_str = f"{len(cameras)} model(s)" if cameras else "—"
        self.lbl_stat_cameras.setText(f"Cameras: {cam_str}")

    # ------------------------------------------------------------------ #
    #  Export                                                              #
    # ------------------------------------------------------------------ #

    def _open_batch_author_dialog(self):
        """Open author assignment for scanned records (in-memory)."""
        if not self._records:
            return
        from .batch_author_dialog import BatchAuthorDialog
        dlg = BatchAuthorDialog(self._records, parent=self)
        dlg.exec_()

    def _export(self):

        if not self._records:
            return

        fmt_idx  = self.combo_fmt.currentIndex()
        fmt_name = EXPORT_EXTS[fmt_idx].lstrip('.')   # 'gpkg' | 'csv' | 'geojson'
        ext      = EXPORT_EXTS[fmt_idx]
        fmt_label = EXPORT_FORMATS[fmt_idx]

        # Confirmation
        author = self.edit_author.text().strip() or "(not set)"
        cameras = sorted(set(
            r.get('camera', '') for r in self._records if r.get('camera')
        ))
        cam_str = ', '.join(cameras[:5])
        if len(cameras) > 5:
            cam_str += f' … ({len(cameras)} total)'

        lines = [
            f"Export {len(self._records)} geotagged photo points.",
            "",
            f"  Format  : {fmt_label}",
            f"  Author  : {author}",
            f"  Cameras : {cam_str or '—'}",
            "",
            "Proceed?",
        ]
        reply = QMessageBox.question(
            self, "Export — Confirm",
            "\n".join(lines),
            QMessageBox.Yes | QMessageBox.No
        )
        if reply != QMessageBox.Yes:
            return

        default_name = f"photo_scan{ext}"
        path, _ = QFileDialog.getSaveFileName(
            self, "Save output", default_name,
            f"{fmt_label};;All Files (*)"
        )
        if not path:
            return

        # Apply global author to records that have no per-record author
        global_author = self.edit_author.text().strip()
        if global_author:
            for rec in self._records:
                if not rec.get("author", ""):
                    rec["author"] = global_author
        ok, msg = export_records(
            self._records, path, fmt_name
        )

        if ok:
            self._set_status(msg)
            QMessageBox.information(self, "Export complete", msg)
            if self.chk_load_layer.isChecked() and fmt_name == 'gpkg':
                self._load_layer(path)
        else:
            QMessageBox.critical(self, "Export failed", msg)

    def _load_layer(self, gpkg_path):
        from ..core.geopackage_exporter import load_geopackage_layer
        layer = load_geopackage_layer(gpkg_path)
        if not layer or not layer.isValid():
            self._set_status("Warning: could not load layer into project.")
            return
        # Apply symbology
        if self.chk_apply_symbology.isChecked():
            try:
                from ..core.layer_symbology import apply_photo_symbology
                apply_photo_symbology(layer)
            except Exception as _e:
                from qgis.core import QgsMessageLog, Qgis
                QgsMessageLog.logMessage(
                    f"GeotagManager symbology error: {_e}",
                    "GeotagManager", Qgis.Warning)
        # Zoom to layer extent and refresh canvas
        try:
            self.iface.setActiveLayer(layer)
            self.iface.zoomToActiveLayer()
            self.iface.mapCanvas().refresh()
        except Exception:
            pass
        self._set_status(f"Layer loaded: {layer.name()}")

    # ------------------------------------------------------------------ #
    #  Misc                                                                #
    # ------------------------------------------------------------------ #

    def _set_status(self, msg):
        self.lbl_status.setText(msg)

    def cleanup(self):
        """Call before closing to stop any running thread."""
        if self._thread and self._thread.isRunning():
            if self._worker:
                self._worker.cancel()
            self._thread.quit()
            self._thread.wait(3000)
