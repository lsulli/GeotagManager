# -*- coding: utf-8 -*-
"""
GeotagManager - QGIS Plugin
Geotag photos from GPX tracks with interactive editing and GeoPackage export.
"""


def classFactory(iface):
    from .geotag_manager import GeotagManager_1_2_29
    return GeotagManager_1_2_29(iface)
