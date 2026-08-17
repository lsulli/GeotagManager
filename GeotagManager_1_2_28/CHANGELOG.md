# GeotagManager — Changelog

All notable changes to this project are documented here.
Format: `[version] — summary`

## [1.2.7] — Removed Write EXIF to file and Write EXIF on export

### Removed
- `✏ Write EXIF to file` button from Edit coordinates panel
- `☐ Write EXIF on export` checkbox from toolbar
- `ExifWriteWorker` background thread class (no longer needed)
- `_write_exif_all()`, `_on_exif_write_progress()`, `_on_exif_write_finished()` methods
- `act_write_exif` QAction

### Rationale
EXIF writing is already handled by:
- **Geotag from GPX** — writes coords via ExifTool for all matched photos
- **✔ Apply coordinates** — writes per-photo on confirmation
- **🧭 Set Direction** — writes direction automatically

---

## [1.2.6]

### Fixed
- `BatchWorker` missing from module-level imports (caused `NameError` on Geotag)
- `export_to_geopackage` docstring was misplaced after `layer_name` assignment, causing silent logic error
- `load_geopackage_layer` now receives the exact `layer_name` used during export, eliminating "cannot load layer" errors when layer name differs from filename

### Changed
- `export_to_geopackage` now returns a 3-tuple `(ok, msg, layer_name)` so callers can pass the correct name to the loader

---

## [1.2.5]

### Added
- Default GeoPackage filename and layer name derived from photo EXIF dates:
  - Single date: `20240226_layer.gpkg` / `20240226_photo_points`
  - Date range: `Start20240226_End20240228_layer.gpkg` / `Start20240226_End20240228_photo_points`
  - No dates: `geotagged_photos.gpkg` (fallback)
- New `make_date_prefix(photo_records)` function in `geopackage_exporter.py`

---

## [1.2.4]

### Fixed
- `btn_open_folder` never enabled after loading photos: `_clear_photos()` was called after `setEnabled(True)`, resetting it to `False`. Order inverted in both `_load_folder` and `_load_folder_silent`

---

## [1.2.3]

### Fixed
- Open Folder button: replaced `os.startfile()` with `subprocess.Popen(["explorer", path])` on Windows for more reliable behaviour in QGIS environment
- Added error logging to Log panel if folder cannot be opened

---

## [1.2.2]

### Fixed
- `QgsVectorLayer`, `QgsFeatureRequest`, `QgsGeometry`, `QgsAction`, `QgsMessageLog`, `Qgis` added to module-level `qgis.core` import block (were missing after 1.2.0 refactoring, causing `NameError` on layer connect)
- Removed duplicate `qgis.core` import block introduced by refactoring

---

## [1.2.1]

### Fixed
- `QObject` and `pyqtSignal` added to `QtCore` import (required by `ExifWriteWorker`, caused `NameError` on plugin load)

---

## [1.2.0] — Major refactoring

### Changed
- `_update_layer_geometry`, `_update_layer_coords`, `_update_layer_feature_direction` unified into single `_update_layer_feature(item, update_geom, update_coords, update_direction)` — performs one `getFeatures()` scan instead of three
- All repeated inner imports moved to module level (`QgsMessageLog`, `Qgis`, `QgsVectorLayer`, `QgsGeometry`, `QgsAction`, `QgsCoordinateTransform`, `write_exif_gps`, etc.)
- `processEvents()` removed from `_on_layer_selection_changed` slot (potential recursive re-entry risk)
- `bare except: pass` replaced with logged exceptions throughout
- GPS diagnostic log removed from `exif_handler.py` (was left from debugging)
- Old `_QgsVL` alias replaced with direct `QgsVectorLayer`

### Fixed
- `ExifWriteWorker` now uses module-level `write_exif_gps` import

---

## [1.1.59]

### Added
- `ExifWriteWorker` QThread class: batch EXIF writing now runs in background with progress bar
- `_update_layer_geometry(item)`: Move Point now moves the feature geometry in the connected layer via `changeGeometryValues()`

### Fixed
- `_open_photo_folder`: `_photo_folder` no longer cleared on layer disconnect
- Duplicate `subprocess.run` call in `_write_exiftool` removed (was launching ExifTool twice per photo)

---

## [1.1.58]

### Fixed
- `_open_photo_folder`: replaced `os.startfile()` with `subprocess.Popen(["explorer", ...])` on Windows
- `_disconnect_layer_listener`: removed erroneous `self._photo_folder = ""` that cleared the folder on disconnect

---

## [1.1.57]

### Added
- `direction` parameter added to `write_exif_gps`, `_write_exiftool`, `_write_piexif`
- Direction is written automatically to EXIF after Set Direction tool

---

## [1.1.56]

### Fixed
- `DirectionTool`: converted to single-click mode (origin always pre-filled from photo GPS)
- `btn_direction` enabled only for photos with GPS coordinates
- Removed duplicate `toggled` signal connection for direction button
- `lbl_photo_direction` updated directly in `_on_direction_set` for immediate UI refresh

---

## [1.1.55]

### Added
- **🧭 Set Direction tool**: new `DirectionTool` map tool computes azimuth from camera GPS to clicked subject point
- Button in Edit coordinates panel, enabled only for geotagged photos
- Direction written to `GPSImgDirection` EXIF tag and `direction` layer field

---

## [1.1.54]

### Added
- Multi-selection Move Point: sequential click mode for multiple selected photos
- `_move_point_queue`: photos processed in list order, tool stays active between clicks
- Status bar shows current photo and remaining count

---

## [1.1.53]

### Added
- `photo_list` set to `ExtendedSelection` mode (Shift+click, Ctrl+click)
- `_on_multi_selection_changed`: updates info panel and button states for multi-selection
- `_apply_manual_coords`: now applies to all selected photos

---

## [1.1.52]

### Fixed
- Batch scan and main export: added `iface.zoomToActiveLayer()` and `mapCanvas().refresh()` after loading layer — layer was added to project but not displayed

---

## [1.1.51]

### Added
- `_update_layer_coords(item)`: Apply coordinates now updates `latitude`, `longitude`, `altitude` attributes in the connected layer feature (matched by `filepath`)

---

## [1.1.50]

### Changed
- `AuthorDialog` rewritten: now reads and groups ALL layer features directly (not session PhotoItems), using `getFeatures()` and `changeAttributeValues()`
- Author assignment applies to the entire layer regardless of session state or selection

---

## [1.1.49]

### Fixed
- `_on_photo_matched`: removed `QApplication.processEvents()` call (caused infinite recursion / stack overflow during batch geotag)

---

## [1.1.48]

### Fixed
- Clear (🗑) button now disconnects the layer listener and deselects features before clearing photos

---

## [1.1.47]

### Changed
- `btn_assign_authors` enabled only when a layer is connected (not on photo load)
- Tooltip updates dynamically: describes functionality when connected, prompts to connect otherwise
- `_update_toolbar_state` called on layer connect and disconnect to sync button state

---

## [1.1.46]

### Changed
- Photo rubber bands changed from filled circles to hollow circles (`setFillColor(QColor(0,0,0,0))`, `setWidth(2)`) — underlying map features remain visible

---

## [1.1.45]

### Fixed (crash prevention)
- `layersRemoved` → `layerWillBeRemoved` signal (fires before C++ object destruction)
- `rb.reset()` called before `removeItem()` on all rubber bands
- `scene()` guarded with `if scene:` throughout
- `closeEvent` disconnects `layerWillBeRemoved` before cleanup
- Worker thread: `wait(2000)` + `terminate()` instead of blocking `wait(3000)`
- `DirectionTool.deactivate()`: rubber band cleanup with scene guard

---

## [1.1.44]

### Fixed
- `_write_exif_if_immediate`: `if item.alt` → `if item.alt is not None` (altitude 0.0 m now shown correctly)

---

## [1.1.43]

### Fixed
- `_read_piexif._rat()`: handles both piexif rational formats:
  - `((num, den),)` — standard wrapping
  - `(num, den)` — direct tuple (some camera writers)
  - This fixed missing altitude and direction values from EXIF

---

## [1.1.42]

### Fixed
- `exif_handler._read_piexif`: removed early `return None` when lat/lon missing — all non-GPS fields (altitude, direction, camera, datetime) now always read
- Loading loop: GPS coords assigned only when present; other fields always populated
- Font unified across Photo Info metadata labels (`_meta_font_css`)
- Author read from layer feature in `_on_layer_selection_changed` (always preferred over session value)

---

## [1.1.41]

### Fixed
- `_on_layer_selection_changed`: author, altitude, direction now enriched from layer feature after finding PhotoItem

---

## [1.1.40]

### Fixed
- Camera label always showed "—": now reads `item.make + item.model`
- Duplicate `meta_lay.addWidget(self.lbl_photo_hfov)` removed (left by line-based replacement)

---

## [1.1.39]

### Changed
- "Sync from layer selection" wrapped in `QGroupBox`
- Photo Info metadata panel: removed `lbl_photo_coords` (coords already in Edit section), added `lbl_photo_direction` and `lbl_photo_author`, moved Source below filename, fixed altitude display (`is not None`)
- Controls bar reordered: Clock offset | Max gap | ‖ | Author | ‖ | Apply symbology (with `QFrame.VLine` separators)

---

## [1.1.38]

### Changed
- Info panel split into three `QGroupBox` sections: **Photo Info**, **Map navigation**, **Edit coordinates**

---

## [1.1.37]

### Changed
- "Sync from layer selection" moved to top of left panel (above Photos section)
- Info panel: Zoom to Point grouped with Map Scale; separator added before Move Point section

---

## [1.1.36]

### Changed
- Info panel reverted to single unified layout (removed zone separation from 1.1.30)

---

## [1.1.35]

### Added
- `AuthorDialog` (`ui/author_dialog.py`): assign authors to photos grouped by date or camera
- `PhotoItem.author` field; included in `to_record()`
- `btn_assign_authors` in controls bar (replaces `edit_author` text field)
- Per-photo author used in GeoPackage export

---

## [1.1.28]

### Added
- `photo_date` field (`QVariant.Date`) in GeoPackage schema — date only, for QGIS temporal filtering

---

## [1.1.27]

### Added
- `photo_date` (`QDate`) field in GeoPackage export

---

## [1.1.25]

### Changed
- `▶ Geotag` button moved from toolbar to GPX Tracks panel

---

## [1.1.24]

### Added
- `☐ Write EXIF on export` checkbox moved from controls bar to toolbar (inline after Export button via `QWidgetAction`)
- **Geotag summary dialog**: shows matched/skipped counts after batch geotag
- `btn_open_folder` label changed to "📂 Open folder" (was emoji only)

---

## [1.1.23]

### Added
- `_detect_path_field(layer)`: auto-detects filepath field on Connect (keywords: `path`, `location`)
- `_on_layer_selection_changed`: rewritten using `layer.selectedFeatures()` (most reliable method)
- Connect processes already-selected features immediately
- Disconnect clears Photos section and deselects features

---

## [1.1.22]

### Fixed
- Feature retrieval: switched to `QgsFeatureRequest(fid)` for reliable field population

---

## [1.1.21]

### Fixed
- Path normalization: mixed separators (`Z:/foo\bar`) now handled correctly in two steps

---

## [1.1.20]

### Fixed
- `load_geopackage_layer`: uses `dataProvider().subLayers()` to find actual layer names in GPKG

---

## [1.1.19]

### Fixed
- `_on_layer_selection_changed`: robust filepath reading (NULL QVariant, mixed separators, fallback to filename)

---

## [1.1.18]

### Changed
- `read_exif_gps` always uses pure Python / piexif (fast); ExifTool reserved for writing and GPX geotag only

---

## [1.1.17]

### Fixed
- Batch photo loading performance: ExifTool removed from read path entirely

---

## [1.1.16]

### Fixed
- ExifTool Windows extraction: `exiftool(-k).exe` found recursively via `os.walk()`; `exiftool_files/` copied alongside exe (required Perl runtime)

---

## [1.1.15]

### Fixed
- `_update_engine_label`: reads filesystem directly, bypasses module cache

---

## [1.1.14]

### Added
- `lbl_engine`: permanent QLabel in status bar showing active EXIF engine (green = ExifTool, orange = fallback)
- `_update_engine_label()` called on startup and after ExifTool wizard

---

## [1.1.13]

### Fixed
- `find_exiftool`: searches only `vendor/` (no system PATH)
- Loop logic fixed (operator precedence bug)

---

## [1.1.12]

### Added
- `_setup_layer_open_action`: adds "Open photo" QgsAction to connected layer if `filepath` field has valid paths

---

## [1.1.11]

### Fixed
- `log_panel.py`: `QgsApplication.messageLog().messageReceived` connected via lambda (signature compatibility fix); `_log_slot` saved for proper disconnect

---

## [1.1.10]

### Added
- `_load_folder_silent`: loads photo folder without dialog (used by layer selection)
- `_on_layer_selection_changed`: selecting a layer point auto-loads its photo folder and shows preview
- `btn_open_folder` (📂) in Photos header
- `btn_open_ext` (🖼) in Photo Info header
- `btn_write_exif` (✏) in Edit coordinates
- `act_fullscreen_photo` (⛶): hides left panel and controls bar for maximum preview area

---

## [1.1.9] — Initial tracked version

### Features present at this version
- Load photos from folder (recursive, with EXIF read)
- Load GPX tracks and batch geotag by timestamp
- Export GeoPackage with full metadata schema
- Manual coordinate entry and map point placement
- Bundled ExifTool wizard (download + vendor install)
- Layer symbology (rule-based: green/red/wedge)
- Batch scan panel (scan directory tree → GPKG)
- Log panel (mirrors QGIS message log)
- EXIF engine detection (ExifTool / piexif / pure Python)
- Photo preview with navigation (Prev/Next)
- Sync from layer selection (connect to TOC layer)
