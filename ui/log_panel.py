# -*- coding: utf-8 -*-
"""
log_panel.py - Floating log window that mirrors the QGIS Message Log.

Intercepts QgsMessageLog.messageReceived signal and displays messages
with colour-coded levels (Info / Warning / Critical), tag filtering,
search, and export to text file.
"""

from qgis.PyQt.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QPlainTextEdit,
    QPushButton, QLabel, QLineEdit, QComboBox, QCheckBox,
    QFileDialog, QSizePolicy, QToolBar, QAction, QWidget,
)
from qgis.PyQt.QtCore import Qt, QSize, pyqtSlot
from qgis.PyQt.QtGui import QTextCharFormat, QColor, QFont, QTextCursor

from qgis.core import Qgis, QgsMessageLog


# Colour scheme per message level
LEVEL_STYLES = {
    Qgis.Info:     {"fg": "#e8f5e9", "tag_fg": "#a5d6a7", "label": "INFO"},
    Qgis.Warning:  {"fg": "#fff8e1", "tag_fg": "#ffe082", "label": "WARNING"},
    Qgis.Critical: {"fg": "#ffebee", "tag_fg": "#ef9a9a", "label": "ERROR"},
    Qgis.Success:  {"fg": "#e3f2fd", "tag_fg": "#90caf9", "label": "SUCCESS"},
}
DEFAULT_STYLE = {"fg": "#f5f5f5", "tag_fg": "#bdbdbd", "label": "INFO"}

# Background colour for each level (dark theme)
LEVEL_BG = {
    Qgis.Info:     QColor("#1e2a1e"),
    Qgis.Warning:  QColor("#2a2510"),
    Qgis.Critical: QColor("#2a1010"),
    Qgis.Success:  QColor("#0d1a2a"),
}
DEFAULT_BG = QColor("#1e1e1e")


class LogPanel(QDialog):
    """
    Floating window that mirrors QGIS Message Log output.
    Can be opened/closed without losing accumulated messages.
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("GeotagManager — Log")
        self.setMinimumSize(600, 320)
        self.resize(800, 400)
        # Non-modal: stays open while using the main dialog
        self.setWindowFlags(
            Qt.Window |
            Qt.WindowCloseButtonHint |
            Qt.WindowMinimizeButtonHint |
            Qt.WindowMaximizeButtonHint
        )

        self._entries = []          # (tag, message, level) — full history
        self._connected = False
        self._log_slot   = None

        self._build_ui()
        self._connect_log()

    # ------------------------------------------------------------------ #
    #  UI                                                                  #
    # ------------------------------------------------------------------ #

    def _build_ui(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(4, 4, 4, 4)
        root.setSpacing(4)

        # ---- Toolbar row ----
        ctrl = QHBoxLayout()

        lbl_filter = QLabel("Tag filter:")
        ctrl.addWidget(lbl_filter)

        self.combo_tag = QComboBox()
        self.combo_tag.setMinimumWidth(160)
        self.combo_tag.addItem("All tags", None)
        self.combo_tag.setToolTip("Filter messages by tag (plugin name / source)")
        self.combo_tag.currentIndexChanged.connect(self._refilter)
        ctrl.addWidget(self.combo_tag)

        lbl_level = QLabel("Level:")
        ctrl.addWidget(lbl_level)

        self.combo_level = QComboBox()
        self.combo_level.addItem("All",      None)
        self.combo_level.addItem("Info",     Qgis.Info)
        self.combo_level.addItem("Warning",  Qgis.Warning)
        self.combo_level.addItem("Error",    Qgis.Critical)
        self.combo_level.currentIndexChanged.connect(self._refilter)
        ctrl.addWidget(self.combo_level)

        lbl_search = QLabel("Search:")
        ctrl.addWidget(lbl_search)

        self.edit_search = QLineEdit()
        self.edit_search.setPlaceholderText("Filter text…")
        self.edit_search.setMinimumWidth(140)
        self.edit_search.textChanged.connect(self._refilter)
        ctrl.addWidget(self.edit_search)

        self.chk_autoscroll = QCheckBox("Auto-scroll")
        self.chk_autoscroll.setChecked(True)
        ctrl.addWidget(self.chk_autoscroll)

        ctrl.addStretch()

        btn_clear = QPushButton("🗑 Clear")
        btn_clear.setFixedHeight(26)
        btn_clear.setToolTip("Clear all messages")
        btn_clear.clicked.connect(self._clear)
        ctrl.addWidget(btn_clear)

        btn_export = QPushButton("💾 Export…")
        btn_export.setFixedHeight(26)
        btn_export.setToolTip("Save log to text file")
        btn_export.clicked.connect(self._export)
        ctrl.addWidget(btn_export)

        root.addLayout(ctrl)

        # ---- Log output ----
        self.log_view = QPlainTextEdit()
        self.log_view.setReadOnly(True)
        self.log_view.setMaximumBlockCount(5000)   # keep last 5000 lines
        self.log_view.setLineWrapMode(QPlainTextEdit.WidgetWidth)

        font = QFont("Consolas, Courier New, monospace")
        font.setPointSize(9)
        self.log_view.setFont(font)
        self.log_view.setStyleSheet(
            "QPlainTextEdit {"
            "  background-color: #1e1e1e;"
            "  color: #d4d4d4;"
            "  border: 1px solid #444;"
            "}"
        )
        root.addWidget(self.log_view, stretch=1)

        # ---- Status bar ----
        self.lbl_count = QLabel("0 messages")
        self.lbl_count.setStyleSheet("color:#888; font-size:10px;")
        root.addWidget(self.lbl_count)

    # ------------------------------------------------------------------ #
    #  Log connection                                                      #
    # ------------------------------------------------------------------ #

    def _connect_log(self):
        if not self._connected:
            from qgis.core import QgsApplication
            # messageReceived(QString, QString, Qgis::MessageLevel)
            # Salva la lambda come attributo per poterla disconnettere
            self._log_slot = lambda msg, tag, lvl: self._on_message(msg, tag, lvl)
            QgsApplication.messageLog().messageReceived.connect(self._log_slot)
            self._connected = True

    def _disconnect_log(self):
        if self._connected:
            try:
                from qgis.core import QgsApplication
                try:
                    QgsApplication.messageLog().messageReceived.disconnect(self._log_slot)
                except Exception:
                    pass
                self._log_slot = None
            except Exception:
                pass
            self._connected = False
        self._log_slot   = None

    # ------------------------------------------------------------------ #
    #  Message handling                                                    #
    # ------------------------------------------------------------------ #

    def _on_message(self, message, tag, level):
        """Called for every message sent to QgsMessageLog."""
        self._entries.append((tag, message, level))
        self._update_tag_combo(tag)
        if self._passes_filter(tag, message, level):
            self._append_line(tag, message, level)
        self._update_count()

    def _passes_filter(self, tag, message, level):
        tag_filter   = self.combo_tag.currentData()
        level_filter = self.combo_level.currentData()
        text_filter  = self.edit_search.text().strip().lower()

        if tag_filter is not None and tag != tag_filter:
            return False
        if level_filter is not None and level != level_filter:
            return False
        if text_filter and text_filter not in message.lower() and text_filter not in tag.lower():
            return False
        return True

    def _append_line(self, tag, message, level):
        style  = LEVEL_STYLES.get(level, DEFAULT_STYLE)
        bg     = LEVEL_BG.get(level, DEFAULT_BG)
        label  = style["label"]

        # Format: [LEVEL] [Tag] message
        line = f"[{label:<7}] [{tag}] {message}"

        cursor = self.log_view.textCursor()
        cursor.movePosition(QTextCursor.End)

        fmt = QTextCharFormat()
        fmt.setBackground(bg)
        fmt.setForeground(QColor(style["fg"]))
        cursor.insertText(line + "\n", fmt)

        if self.chk_autoscroll.isChecked():
            self.log_view.setTextCursor(cursor)
            self.log_view.ensureCursorVisible()

    def _update_tag_combo(self, tag):
        """Add tag to combo if not already present."""
        if self.combo_tag.findData(tag) == -1:
            self.combo_tag.addItem(tag, tag)

    def _update_count(self):
        total    = len(self._entries)
        visible  = self.log_view.document().blockCount() - 1
        self.lbl_count.setText(f"{visible} shown / {total} total")

    # ------------------------------------------------------------------ #
    #  Filter / clear / export                                            #
    # ------------------------------------------------------------------ #

    def _refilter(self, *_):
        """Rebuild log_view applying current filters to all stored entries."""
        self.log_view.clear()
        for tag, message, level in self._entries:
            if self._passes_filter(tag, message, level):
                self._append_line(tag, message, level)
        self._update_count()

    def _clear(self):
        self._entries.clear()
        self.log_view.clear()
        self._update_count()

    def _export(self):
        path, _ = QFileDialog.getSaveFileName(
            self, "Export log", "geotag_manager.log",
            "Text files (*.log *.txt);;All Files (*)"
        )
        if not path:
            return
        lines = []
        for tag, message, level in self._entries:
            style = LEVEL_STYLES.get(level, DEFAULT_STYLE)
            lines.append(f"[{style['label']:<7}] [{tag}] {message}")
        try:
            with open(path, "w", encoding="utf-8") as f:
                f.write("\n".join(lines))
            self.lbl_count.setText(
                f"Exported {len(lines)} lines → {path}"
            )
        except Exception as e:
            from qgis.PyQt.QtWidgets import QMessageBox
            QMessageBox.critical(self, "Export failed", str(e))

    # ------------------------------------------------------------------ #
    #  Lifecycle                                                           #
    # ------------------------------------------------------------------ #

    def closeEvent(self, event):
        # Hide instead of destroy — keeps message history
        event.ignore()
        self.hide()

    def destroy_panel(self):
        """Call this from plugin unload to properly disconnect."""
        self._disconnect_log()
        super().close()
