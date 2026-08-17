# -*- coding: utf-8 -*-
"""
author_dialog.py — Assign authors to photos grouped by date or camera.

Sources (one or both can be active):
  - Session PhotoItems
  - Connected layer

Rules:
  - Groups by date (day) or camera
  - By default only items WITHOUT an existing author are shown
  - "Show also already assigned" checkbox includes all items (pre-filled)
  - "Write EXIF Artist" checkbox writes to original files on Apply
"""

from __future__ import annotations
from collections import defaultdict

from qgis.PyQt.QtWidgets import (
    QDialog, QVBoxLayout, QTableWidget, QTableWidgetItem,
    QLabel, QRadioButton, QGroupBox, QHBoxLayout, QCheckBox,
    QAbstractItemView, QHeaderView, QMessageBox, QDialogButtonBox,
    QProgressBar, QApplication, QSpinBox,
)
from qgis.PyQt.QtCore import Qt
from qgis.PyQt.QtGui import QFont, QColor


class AuthorDialog(QDialog):

    def __init__(self, photo_items=None, listened_layer=None, parent=None):
        super().__init__(parent)
        self.photo_items = photo_items or []
        self.layer       = listened_layer
        self._groups      = {}
        self._cancel_flag = [False]  # mutable so batch func can check it
        self.setWindowTitle("GeotagManager — Assign Authors")
        self.setMinimumWidth(620)
        self.setMinimumHeight(440)
        self._build_ui()
        self._populate()

    # ── UI ────────────────────────────────────────────────────────────────

    def _build_ui(self):
        lay = QVBoxLayout(self)
        lay.setSpacing(8)

        self.lbl_info = QLabel()
        self.lbl_info.setWordWrap(True)
        self.lbl_info.setStyleSheet("color:#555; font-size:10px;")
        lay.addWidget(self.lbl_info)

        # Group-by + show-assigned on same row
        opts_row = QHBoxLayout()
        grp_by = QGroupBox("Group by")
        grp_lay = QHBoxLayout(grp_by)
        self.rb_date   = QRadioButton("Date (day)")
        self.rb_camera = QRadioButton("Camera")
        self.rb_date.setChecked(True)
        grp_lay.addWidget(self.rb_date)
        grp_lay.addWidget(self.rb_camera)
        opts_row.addWidget(grp_by)

        self.chk_show_assigned = QCheckBox("Show also already assigned")
        self.chk_show_assigned.setChecked(False)
        self.chk_show_assigned.setToolTip(
            "When checked, photos/features that already have an author\n"
            "are also listed (pre-filled). You can edit or overwrite them."
        )
        opts_row.addWidget(self.chk_show_assigned)
        opts_row.addStretch()
        lay.addLayout(opts_row)

        # Signals
        self.rb_date.toggled.connect(self._populate)
        self.chk_show_assigned.stateChanged.connect(self._populate)

        # Table — 4 columns when showing assigned (adds "Current author")
        self.table = QTableWidget(0, 4)
        self.table.setHorizontalHeaderLabels(
            ["Group", "Count", "Current author", "New author"]
        )
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeToContents)
        self.table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeToContents)
        self.table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeToContents)
        self.table.horizontalHeader().setSectionResizeMode(3, QHeaderView.Stretch)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.table.setEditTriggers(
            QAbstractItemView.DoubleClicked | QAbstractItemView.SelectedClicked
        )
        self.table.verticalHeader().setVisible(False)
        lay.addWidget(self.table)

        # Target info
        targets = []
        if self.photo_items:
            targets.append(f"session photos ({len(self.photo_items)})")
        if self.layer and self.layer.isValid():
            targets.append(f"layer \"{self.layer.name()}\"")
        self.lbl_target = QLabel(
            "Applies to: " + " + ".join(targets) if targets else ""
        )
        self.lbl_target.setStyleSheet("font-size:10px; color:#2255aa;")
        lay.addWidget(self.lbl_target)

        # Options
        self.chk_only_selected = QCheckBox("Apply only to selected rows in table")
        self.chk_only_selected.setChecked(True)
        self.chk_only_selected.setToolTip(
            "When checked, Apply affects only the rows currently\n"
            "selected (highlighted) in the table above.\n"
            "Uncheck to apply to all rows with a non-empty New author."
        )
        lay.addWidget(self.chk_only_selected)

        exif_row = QHBoxLayout()
        self.chk_write_exif = QCheckBox("Write EXIF tag Artist to original photo files")
        self.chk_write_exif.setChecked(False)
        self.chk_write_exif.setToolTip(
            "Writes author to EXIF Artist and XMP:Creator tags.\n"
            "Requires ExifTool or piexif. Original files will be modified."
        )
        exif_row.addWidget(self.chk_write_exif)
        exif_row.addStretch()
        exif_row.addWidget(QLabel("Batch size:"))
        self.spin_chunk = QSpinBox()
        self.spin_chunk.setRange(50, 5000)
        self.spin_chunk.setValue(500)
        self.spin_chunk.setSingleStep(100)
        self.spin_chunk.setFixedWidth(70)
        self.spin_chunk.setToolTip(
            "Number of files per ExifTool batch call.\n"
            "Lower = more calls but safer on slow networks.\n"
            "Higher = fewer calls but more memory per batch.\n"
            "Default: 500"
        )
        exif_row.addWidget(self.spin_chunk)
        lay.addLayout(exif_row)

        # ── Status bar ───────────────────────────────────────────────
        self.lbl_status = QLabel("Ready.")
        self.lbl_status.setStyleSheet(
            "font-size:10px; color:#333; padding: 2px 4px;"
            "background:#f0f0f0; border: 1px solid #ccc; border-radius:3px;"
        )
        lay.addWidget(self.lbl_status)

        prog_row = QHBoxLayout()
        self.progress_bar = QProgressBar()
        self.progress_bar.setFixedHeight(14)
        self.progress_bar.setVisible(False)
        self.progress_bar.setTextVisible(True)
        prog_row.addWidget(self.progress_bar)

        from qgis.PyQt.QtWidgets import QPushButton as _PB
        self.btn_stop = _PB("⏹ Stop")
        self.btn_stop.setFixedWidth(70)
        self.btn_stop.setVisible(False)
        self.btn_stop.setToolTip("Cancel the EXIF write process")
        self.btn_stop.clicked.connect(self._request_cancel)
        prog_row.addWidget(self.btn_stop)
        lay.addLayout(prog_row)

        from qgis.PyQt.QtWidgets import QPushButton
        btns = QDialogButtonBox(QDialogButtonBox.Cancel)
        btn_apply = btns.addButton("Apply", QDialogButtonBox.AcceptRole)
        btn_apply.setToolTip(
            "Update session photos and connected layer attributes.\n"
            "Writes EXIF Artist only to session photo files if checkbox is active."
        )
        btn_apply.clicked.connect(lambda: self._apply(write_layer_files=False))

        self.btn_apply_all = btns.addButton(
            "Apply + Write EXIF to all layer files",
            QDialogButtonBox.ActionRole
        )
        self.btn_apply_all.setToolTip(
            "Update session photos and layer attributes, then write\n"
            "EXIF Artist to ALL photo files referenced in the connected layer.\n"
            "Uses a single ExifTool batch call per unique author."
        )
        self.btn_apply_all.setEnabled(
            self.layer is not None and self.layer.isValid()
        )
        self.btn_apply_all.clicked.connect(lambda: self._apply(write_layer_files=True))

        btns.rejected.connect(self.reject)
        lay.addWidget(btns)

    # ── Status helpers ────────────────────────────────────────────────────

    def _set_status(self, msg: str, progress: int = -1, total: int = -1):
        """Update status label and optional progress bar."""
        self.lbl_status.setText(msg)
        if total > 0:
            self.progress_bar.setRange(0, total)
            self.progress_bar.setValue(max(0, progress))
            self.progress_bar.setVisible(True)
            self.progress_bar.setFormat(f"%v / {total}  (%p%)")
        else:
            self.progress_bar.setVisible(False)
        QApplication.processEvents()

    def _request_cancel(self):
        """Signal the batch process to stop after the current author group."""
        self._cancel_flag[0] = True
        self._set_status("Stopping… please wait for current batch to finish.")
        self.btn_stop.setEnabled(False)

    # ── Population ────────────────────────────────────────────────────────

    def _populate(self):
        import os
        use_date     = self.rb_date.isChecked()
        show_all     = self.chk_show_assigned.isChecked()

        # {key: {'items': [...PhotoItem], 'fids': [...fid],
        #        'current_authors': set()}}
        groups: dict = defaultdict(lambda: {
            'items': [], 'fids': [], 'current_authors': set()
        })

        # ── Source 1: session PhotoItems ──────────────────────────────────
        total_items = len(self.photo_items)
        already_items = 0
        for item in self.photo_items:
            existing = (getattr(item, 'author', '') or '').strip()
            has_author = existing and existing not in ('NULL', 'None')
            if has_author:
                already_items += 1
                if not show_all:
                    continue
            key = self._key_from_item(item, use_date)
            groups[key]['items'].append(item)
            if has_author:
                groups[key]['current_authors'].add(existing)

        # ── Source 2: layer features ───────────────────────────────────────
        total_feats = 0
        already_feats = 0
        if self.layer and self.layer.isValid():
            fields    = [f.name() for f in self.layer.fields()]
            auth_idx  = self.layer.fields().indexFromName('author') \
                        if 'author' in fields else -1
            dt_field  = next((f for f in ('datetime_photo', 'date_photo')
                              if f in fields), None)
            cam_field = next((f for f in ('camera', 'model') if f in fields), None)
            dt_idx    = self.layer.fields().indexFromName(dt_field)  if dt_field  else -1
            cam_idx   = self.layer.fields().indexFromName(cam_field) if cam_field else -1
            fp_idx    = self.layer.fields().indexFromName('filepath') \
                        if 'filepath' in fields else -1

            session_fps = {
                os.path.normpath(getattr(it, 'filepath', '') or '')
                for it in self.photo_items
                if getattr(it, 'filepath', '')
            }

            for feat in self.layer.getFeatures():
                total_feats += 1
                if session_fps and fp_idx >= 0:
                    fp_raw  = str(feat[fp_idx] or '').strip()
                    fp_norm = os.path.normpath(fp_raw) if fp_raw else ''
                    if fp_norm and fp_norm in session_fps:
                        continue

                existing = ''
                if auth_idx >= 0:
                    existing = str(feat[auth_idx] or '').strip()
                    if existing in ('NULL', 'None'):
                        existing = ''
                has_author = bool(existing)
                if has_author:
                    already_feats += 1
                    if not show_all:
                        continue

                key = self._key_from_feature(feat, dt_idx, cam_idx, use_date)
                groups[key]['fids'].append(feat.id())
                if has_author:
                    groups[key]['current_authors'].add(existing)

        # ── Summary ───────────────────────────────────────────────────────
        parts = []
        if self.photo_items:
            parts.append(
                f"Session: {total_items} total, {already_items} assigned"
            )
        if self.layer and self.layer.isValid():
            parts.append(
                f"Layer: {total_feats} total, {already_feats} assigned"
            )
        self.lbl_info.setText("  |  ".join(parts))

        # ── Build table ───────────────────────────────────────────────────
        self.table.setRowCount(0)
        self._groups = {}

        for key in sorted(groups.keys()):
            g   = groups[key]
            n   = len(g['items']) + len(g['fids'])
            if n == 0:
                continue

            row = self.table.rowCount()
            self.table.insertRow(row)

            # Col 0: group key
            cell_key = QTableWidgetItem(key)
            cell_key.setFlags(Qt.ItemIsEnabled | Qt.ItemIsSelectable)
            cell_key.setFont(QFont("", -1, QFont.Bold))
            self.table.setItem(row, 0, cell_key)

            # Col 1: count
            cell_count = QTableWidgetItem(str(n))
            cell_count.setFlags(Qt.ItemIsEnabled | Qt.ItemIsSelectable)
            cell_count.setTextAlignment(Qt.AlignCenter)
            self.table.setItem(row, 1, cell_count)

            # Col 2: current author(s) — read only
            current = ", ".join(sorted(g['current_authors'])) \
                      if g['current_authors'] else ""
            cell_cur = QTableWidgetItem(current)
            cell_cur.setFlags(Qt.ItemIsEnabled | Qt.ItemIsSelectable)
            if current:
                cell_cur.setForeground(QColor("#888"))
                cell_cur.setFont(QFont("", -1, QFont.StyleItalic))
            self.table.setItem(row, 2, cell_cur)

            # Col 3: new author (editable) — pre-fill if single existing value
            prefill = current if len(g['current_authors']) == 1 else ""
            cell_new = QTableWidgetItem(prefill)
            cell_new.setToolTip("Type the author name to assign")
            self.table.setItem(row, 3, cell_new)

            self._groups[row] = g

    # ── Key builders ──────────────────────────────────────────────────────

    def _key_from_item(self, item, use_date: bool) -> str:
        if use_date:
            dt = getattr(item, 'datetime', None)
            if dt and hasattr(dt, 'strftime'):
                return dt.strftime('%Y-%m-%d')
            return '(no date)'
        else:
            parts = [p.strip() for p in [
                getattr(item, 'make', ''), getattr(item, 'model', '')
            ] if p and p.strip()]
            return ' '.join(parts) if parts else '(unknown camera)'

    def _key_from_feature(self, feat, dt_idx, cam_idx, use_date: bool) -> str:
        if use_date:
            if dt_idx >= 0:
                raw = str(feat[dt_idx] or '').strip()
                for part in raw.split():
                    part = part.replace(':', '-')
                    if len(part) >= 10 and part[4] == '-' and part[7] == '-':
                        return part[:10]
            return '(no date)'
        else:
            if cam_idx >= 0:
                v = str(feat[cam_idx] or '').strip()
                if v and v not in ('NULL', 'None'):
                    return v
            return '(unknown camera)'

    # ── Apply ─────────────────────────────────────────────────────────────

    def _apply(self, write_layer_files: bool = False):
        self._set_status("Applying author assignments…")
        n_items = n_feats = 0
        auth_idx = -1
        if self.layer and self.layer.isValid():
            auth_idx = self.layer.fields().indexFromName('author')

        # Determine which rows to process
        only_selected = self.chk_only_selected.isChecked()
        selected_rows = {
            idx.row() for idx in self.table.selectedIndexes()
        } if only_selected else set(self._groups.keys())

        for row, g in self._groups.items():
            if only_selected and row not in selected_rows:
                continue
            # Read from col 3 (new author)
            author = self.table.item(row, 3).text().strip()
            if not author:
                continue
            for item in g['items']:
                item.author = author
                n_items += 1
            if g['fids'] and auth_idx >= 0:
                changes = {fid: {auth_idx: author} for fid in g['fids']}
                self.layer.dataProvider().changeAttributeValues(changes)
                n_feats += len(g['fids'])

        if n_items + n_feats == 0:
            QMessageBox.information(self, "Author Assignment",
                                    "No authors entered — nothing to apply.")
            return

        if self.layer and self.layer.isValid():
            self.layer.triggerRepaint()

        self._set_status(
            f"Updated: {n_items} session photo(s), {n_feats} layer feature(s)."
        )

        # Write EXIF Artist — batch call per unique author
        n_exif_ok = n_exif_fail = 0
        if self.chk_write_exif.isChecked() or write_layer_files:
            from ..core.exif_handler import write_exif_author_batch
            import os
            author_map = {}  # {filepath: author}

            # Source A: session photo items
            for row, g in self._groups.items():
                if only_selected and row not in selected_rows:
                    continue
                author = self.table.item(row, 3).text().strip()
                if not author:
                    continue
                for item in g['items']:
                    if os.path.isfile(item.filepath):
                        author_map[item.filepath] = author

            # Source B: all layer feature filepaths (only for Apply All)
            if write_layer_files and self.layer and self.layer.isValid():
                self._set_status("Reading filepaths from layer…")
                fields = [f.name() for f in self.layer.fields()]
                fp_idx   = self.layer.fields().indexFromName('filepath') \
                           if 'filepath' in fields else -1
                auth_idx_r = self.layer.fields().indexFromName('author') \
                           if 'author' in fields else -1
                if fp_idx >= 0:
                    feat_count = self.layer.featureCount()
                    for i, feat in enumerate(self.layer.getFeatures()):
                        if i % 50 == 0:
                            self._set_status(
                                f"Reading layer features: {i}/{feat_count}…",
                                progress=i, total=feat_count
                            )
                        fp_raw = str(feat[fp_idx] or '').strip()
                        if not fp_raw or fp_raw in ('NULL', 'None'):
                            continue
                        fp = os.path.normpath(fp_raw)
                        if not os.path.isfile(fp):
                            continue
                        a = ''
                        if auth_idx_r >= 0:
                            a = str(feat[auth_idx_r] or '').strip()
                            if a in ('NULL', 'None'):
                                a = ''
                        if a:
                            author_map[fp] = a

            n_total_exif = len(author_map)
            self._cancel_flag[0] = False
            self.btn_stop.setVisible(True)
            self.btn_stop.setEnabled(True)
            import math
            n_chunks = max(1, math.ceil(n_total_exif / self.spin_chunk.value()))
            chunk_info = f" in {n_chunks} batch(es)" if n_chunks > 1 else ""
            self._set_status(
                f"Writing EXIF Artist to {n_total_exif} file(s){chunk_info}…",
                progress=0, total=n_total_exif
            )
            chunk_size = self.spin_chunk.value()
            n_exif_ok, n_exif_fail = write_exif_author_batch(
                author_map,
                cancel_flag=self._cancel_flag,
                chunk_size=chunk_size
            )
            self.btn_stop.setVisible(False)
            cancelled = self._cancel_flag[0]
            status_msg = f"EXIF Artist written: {n_exif_ok}/{n_total_exif} file(s)"
            if cancelled:
                status_msg += " — stopped by user"
            elif n_exif_fail:
                status_msg += f" ({n_exif_fail} failed)"
            self._set_status(status_msg, progress=n_exif_ok, total=n_total_exif)

        parts = []
        if n_items: parts.append(f"{n_items} session photo(s)")
        if n_feats: parts.append(f"{n_feats} layer feature(s)")
        msg = "Author assigned to: " + ", ".join(parts) + "."
        if self.chk_write_exif.isChecked() or write_layer_files:
            msg += f"\n\nEXIF Artist written: {n_exif_ok} file(s)"
            if n_exif_fail:
                msg += f" ({n_exif_fail} failed)"
        self._set_status("Done.")
        QMessageBox.information(self, "Author Assignment", msg)
        self.accept()
