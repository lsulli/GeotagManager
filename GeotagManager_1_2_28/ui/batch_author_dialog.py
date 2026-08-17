# -*- coding: utf-8 -*-
"""
batch_author_dialog.py — Assign authors to batch scan records (in-memory).

Groups records by date (day) or camera and assigns author names.
Only records without an existing author are modified.
Changes are applied directly to the _records list before export.
"""

from collections import defaultdict

from qgis.PyQt.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QTableWidget, QTableWidgetItem,
    QLabel, QRadioButton, QGroupBox, QAbstractItemView, QHeaderView,
    QMessageBox, QDialogButtonBox,
)
from qgis.PyQt.QtCore import Qt
from qgis.PyQt.QtGui import QFont


class BatchAuthorDialog(QDialog):
    """
    Assign authors to batch scan records grouped by date or camera.
    Operates on the in-memory _records list (list of dicts).
    Only records without an existing author are affected.
    """

    def __init__(self, records: list, parent=None):
        super().__init__(parent)
        self.records = records
        self.setWindowTitle("GeotagManager — Assign Authors (Batch Scan)")
        self.setMinimumWidth(580)
        self.setMinimumHeight(380)
        self._groups = {}
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

        grp_by = QGroupBox("Group records by")
        grp_lay = QHBoxLayout(grp_by)
        self.rb_date   = QRadioButton("Date (day)")
        self.rb_camera = QRadioButton("Camera")
        self.rb_date.setChecked(True)
        grp_lay.addWidget(self.rb_date)
        grp_lay.addWidget(self.rb_camera)
        grp_lay.addStretch()
        self.rb_date.toggled.connect(self._populate)
        lay.addWidget(grp_by)

        self.table = QTableWidget(0, 3)
        self.table.setHorizontalHeaderLabels(
            ["Group", "Records (without author)", "Author to assign"]
        )
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeToContents)
        self.table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeToContents)
        self.table.horizontalHeader().setSectionResizeMode(2, QHeaderView.Stretch)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setEditTriggers(
            QAbstractItemView.DoubleClicked | QAbstractItemView.SelectedClicked
        )
        self.table.verticalHeader().setVisible(False)
        lay.addWidget(self.table)

        lbl_note = QLabel(
            "Changes are applied to in-memory records only — "
            "the author will be written to the GeoPackage on export."
        )
        lbl_note.setWordWrap(True)
        lbl_note.setStyleSheet("font-size:10px; color:#2255aa;")
        lay.addWidget(lbl_note)

        btns = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        btns.button(QDialogButtonBox.Ok).setText("Apply")
        btns.accepted.connect(self._apply)
        btns.rejected.connect(self.reject)
        lay.addWidget(btns)

    # ── Population ────────────────────────────────────────────────────────

    def _populate(self):
        use_date = self.rb_date.isChecked()

        # Only records without author
        eligible = [r for r in self.records if not r.get("author", "")]
        total_all = len(self.records)
        already   = total_all - len(eligible)

        self.lbl_info.setText(
            f"Total scanned records: {total_all}  |  "
            f"Without author: {len(eligible)}  |  "
            f"Already assigned (skipped): {already}"
        )

        groups: dict[str, list] = defaultdict(list)
        for i, rec in enumerate(self.records):
            if rec.get("author", ""):
                continue
            if use_date:
                dt = rec.get("datetime")
                if dt and hasattr(dt, "strftime"):
                    key = dt.strftime("%Y-%m-%d")
                else:
                    key = "(no date)"
            else:
                key = rec.get("camera") or "(unknown camera)"
            groups[key].append(i)  # store index into self.records

        self.table.setRowCount(0)
        self._groups = {}

        for key in sorted(groups.keys()):
            indices = groups[key]
            row = self.table.rowCount()
            self.table.insertRow(row)

            cell_key = QTableWidgetItem(key)
            cell_key.setFlags(Qt.ItemIsEnabled | Qt.ItemIsSelectable)
            cell_key.setFont(QFont("", -1, QFont.Bold))
            self.table.setItem(row, 0, cell_key)

            cell_count = QTableWidgetItem(str(len(indices)))
            cell_count.setFlags(Qt.ItemIsEnabled | Qt.ItemIsSelectable)
            cell_count.setTextAlignment(Qt.AlignCenter)
            self.table.setItem(row, 1, cell_count)

            cell_author = QTableWidgetItem("")
            cell_author.setToolTip("Type the author name for this group")
            self.table.setItem(row, 2, cell_author)

            self._groups[row] = indices

    # ── Apply ─────────────────────────────────────────────────────────────

    def _apply(self):
        total_assigned = 0
        for row, indices in self._groups.items():
            author = self.table.item(row, 2).text().strip()
            if not author:
                continue
            for idx in indices:
                self.records[idx]["author"] = author
                total_assigned += 1

        if not total_assigned:
            QMessageBox.information(
                self, "Assign Authors",
                "No authors entered — nothing to apply."
            )
            return

        QMessageBox.information(
            self, "Assign Authors",
            f"Author assigned to {total_assigned} record(s).\n"
            "The author will be written to the GeoPackage on export."
        )
        self.accept()
