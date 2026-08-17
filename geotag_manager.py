# -*- coding: utf-8 -*-
"""
geotag_manager.py - Plugin entry point. Registers menu and toolbar action.
"""

import os

from qgis.PyQt.QtWidgets import QAction
from qgis.PyQt.QtGui import QIcon
from qgis.core import QgsApplication


class GeotagManager_1_2_29:

    def __init__(self, iface):
        self.iface = iface
        self.plugin_dir = os.path.dirname(__file__)
        self._action = None
        self._dialog = None

    def initGui(self):
        icon_path = os.path.join(self.plugin_dir, "icons", "icon.png")
        if not os.path.exists(icon_path):
            icon = QgsApplication.getThemeIcon("/mActionIdentify.svg")
        else:
            icon = QIcon(icon_path)

        self._action = QAction(icon, "GeotagManager_1_2_29", self.iface.mainWindow())
        self._action.setToolTip(
            "GeotagManager — Geotag foto da tracce GPX con editing interattivo"
        )
        self._action.triggered.connect(self.run)

        self.iface.addToolBarIcon(self._action)
        self.iface.addPluginToMenu("&GeotagManager", self._action)

    def unload(self):
        self.iface.removePluginMenu("&GeotagManager", self._action)
        self.iface.removeToolBarIcon(self._action)
        if self._dialog:
            self._dialog.close()

    def run(self):
        from .ui.dialog import GeotagManagerDialog
        if self._dialog is None or not self._dialog.isVisible():
            self._dialog = GeotagManagerDialog(self.iface, None)
        self._dialog.show()
        self._dialog.raise_()
        self._dialog.activateWindow()
