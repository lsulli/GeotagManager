# -*- coding: utf-8 -*-
"""scan_dialog.py - Standalone window wrapping ScanPanel (variant C)."""

from qgis.PyQt.QtWidgets import QDialog, QVBoxLayout
from qgis.PyQt.QtCore import Qt
from .scan_panel import ScanPanel


class ScanDialog(QDialog):
    """Floating window containing the Batch Scan panel."""

    def __init__(self, iface, parent=None):
        super().__init__(parent)
        self.setWindowTitle("GeotagManager — Batch Scan → GPKG")
        self.setMinimumSize(800, 560)
        self.resize(960, 640)
        self.setWindowFlags(
            Qt.Window |
            Qt.WindowCloseButtonHint |
            Qt.WindowMinimizeButtonHint |
            Qt.WindowMaximizeButtonHint
        )
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        self._panel = ScanPanel(iface, self)
        lay.addWidget(self._panel)

    def closeEvent(self, event):
        self._panel.cleanup()
        event.accept()
