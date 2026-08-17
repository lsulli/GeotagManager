# -*- coding: utf-8 -*-
"""
exiftool_manager.py  —  Download e gestione di ExifTool bundled nella cartella vendor/.

Scarica l'ultima versione da SourceForge e la installa in vendor/exiftool[.exe].
Nessuna dipendenza da installazioni di sistema — tutto autocontenuto nel plugin.
"""

import os
import sys
import stat
import zipfile
import shutil
import platform
import urllib.request
from datetime import datetime

from qgis.PyQt.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QLabel,
    QPushButton, QProgressBar, QTextEdit, QMessageBox,
)
from qgis.PyQt.QtCore import Qt, QThread, pyqtSignal, QObject
from qgis.core import QgsMessageLog, Qgis


# ---------------------------------------------------------------------------
#  Percorsi
# ---------------------------------------------------------------------------

def plugin_dir():
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

def vendor_dir():
    return os.path.join(plugin_dir(), 'vendor')

def bundled_exiftool_path():
    """Return path to the bundled binary, or None if not present."""
    vdir = vendor_dir()
    p = os.path.join(vdir, 'exiftool.exe' if sys.platform.startswith('win') else 'exiftool')
    return p if os.path.isfile(p) else None

def bundled_version():
    """Return the installed version string from vendor/exiftool_version.txt, or None."""
    vfile = os.path.join(vendor_dir(), 'exiftool_version.txt')
    if os.path.isfile(vfile):
        with open(vfile) as f:
            return f.read().strip()
    return None


# ---------------------------------------------------------------------------
#  Worker di download
# ---------------------------------------------------------------------------

EXIFTOOL_VER_URL = 'https://exiftool.org/ver.txt'
SF_BASE          = 'https://sourceforge.net/projects/exiftool/files'


class DownloadWorker(QObject):
    log      = pyqtSignal(str)
    progress = pyqtSignal(int)       # 0-100
    finished = pyqtSignal(bool, str) # success, message

    def __init__(self, parent=None):
        super().__init__(parent)
        self._cancelled = False

    def cancel(self):
        self._cancelled = True

    def run(self):
        try:
            self._download()
        except Exception as e:
            self.finished.emit(False, str(e))

    def _download(self):
        vdir = vendor_dir()
        os.makedirs(vdir, exist_ok=True)

        # 1. Versione
        self.log.emit("Fetching ExifTool version from exiftool.org...")
        try:
            with urllib.request.urlopen(EXIFTOOL_VER_URL, timeout=10) as r:
                version = r.read().decode().strip()
        except Exception:
            version = '13.50'
            self.log.emit(f"Could not fetch version, using fallback: {version}")
        self.log.emit(f"Version: {version}")
        self.progress.emit(10)
        if self._cancelled:
            self.finished.emit(False, "Cancelled.")
            return

        # 2. URL
        if sys.platform.startswith('win'):
            filename   = f'exiftool-{version}_64.zip'
            url        = f'{SF_BASE}/{filename}/download'
            final_name = 'exiftool.exe'
        else:
            filename   = f'Image-ExifTool-{version}.tar.gz'
            url        = f'{SF_BASE}/{filename}/download'
            final_name = 'exiftool'

        dest = os.path.join(vdir, filename)

        # 3. Download
        self.log.emit(f"Downloading {filename}...")
        self.log.emit(f"URL: {url}")
        try:
            self._download_file(url, dest)
        except Exception as e:
            self.finished.emit(False, f"Download fallito: {e}")
            return

        self.progress.emit(70)
        if self._cancelled:
            self.finished.emit(False, "Cancelled.")
            return

        # 4. Estrazione
        self.log.emit("Estrazione...")
        try:
            if sys.platform.startswith('win'):
                self._extract_windows(dest, vdir, final_name)
            else:
                self._extract_unix(dest, vdir, final_name, version)
        except Exception as e:
            self.finished.emit(False, f"Estrazione fallita: {e}")
            return

        self.progress.emit(90)

        # 5. Salva versione
        with open(os.path.join(vdir, 'exiftool_version.txt'), 'w') as f:
            f.write(version)

        # 6. Pulizia archivio
        try:
            os.remove(dest)
        except Exception:
            pass

        self.progress.emit(100)
        et_path = os.path.join(vdir, final_name)
        self.log.emit(f"Completato. ExifTool {version} installato in:\n{et_path}")
        self.finished.emit(True, f"ExifTool {version} installed successfully.")

    def _download_file(self, url, dest):
        req = urllib.request.Request(url, headers={'User-Agent': 'GeotagManager/1.1.13'})
        with urllib.request.urlopen(req, timeout=120) as response:
            total      = int(response.headers.get('Content-Length', 0))
            downloaded = 0
            with open(dest, 'wb') as f:
                while True:
                    if self._cancelled:
                        return
                    buf = response.read(65536)
                    if not buf:
                        break
                    f.write(buf)
                    downloaded += len(buf)
                    if total:
                        self.progress.emit(10 + min(int(60 * downloaded / total), 59))

    def _extract_windows(self, zip_path, vdir, final_name):
        """Estrae exiftool.exe e la cartella exiftool_files/ in vendor/.
        ExifTool Windows richiede che exiftool_files/ sia nella stessa
        directory dell'exe.
        """
        with zipfile.ZipFile(zip_path, 'r') as zf:
            members = zf.namelist()
            self.log.emit(f"Archive: {len(members)} files — extracting...")
            zf.extractall(vdir)

        # Find the root subfolder inside the archive
        # e.g. vendor/exiftool-13.54_64/
        subdir = None
        for name in os.listdir(vdir):
            full = os.path.join(vdir, name)
            if os.path.isdir(full) and 'exiftool' in name.lower():
                subdir = full
                self.log.emit(f"Archive subfolder: {subdir}")
                break

        if subdir is None:
            raise RuntimeError("Sottocartella exiftool non trovata dopo estrazione.")

        # 1. Copia exiftool.exe (o exiftool(-k).exe) in vendor/
        dst_exe = os.path.join(vdir, final_name)
        src_exe = None
        for fname in os.listdir(subdir):
            if 'exiftool' in fname.lower() and fname.lower().endswith('.exe'):
                src_exe = os.path.join(subdir, fname)
                break
        if src_exe is None:
            raise RuntimeError(f"exe non trovato in {subdir}")
        if os.path.isfile(dst_exe):
            os.remove(dst_exe)
        shutil.copy2(src_exe, dst_exe)
        self.log.emit(f"Copiato: {os.path.basename(src_exe)} -> {final_name}")

        # 2. Copia exiftool_files/ in vendor/exiftool_files/
        src_files = os.path.join(subdir, 'exiftool_files')
        dst_files = os.path.join(vdir, 'exiftool_files')
        if os.path.isdir(src_files):
            if os.path.isdir(dst_files):
                shutil.rmtree(dst_files)
            shutil.copytree(src_files, dst_files)
            self.log.emit(f"Copied exiftool_files/ folder to vendor/")
        else:
            raise RuntimeError(f"exiftool_files/ non trovata in {subdir}")

        # 3. Rimuovi la sottocartella temporanea
        shutil.rmtree(subdir)
        self.log.emit(f"Removed temporary subfolder")

        # Final verification
        if os.path.isfile(dst_exe) and os.path.isdir(dst_files):
            self.log.emit(
                f"OK: {final_name} ({os.path.getsize(dst_exe):,} bytes) "
                f"+ exiftool_files/ pronti in vendor/"
            )
        else:
            raise RuntimeError("Verifica finale fallita: file mancanti in vendor/")

    def _extract_unix(self, tgz_path, vdir, final_name, version):
        import tarfile
        with tarfile.open(tgz_path, 'r:gz') as tf:
            script = f'Image-ExifTool-{version}/exiftool'
            try:
                m = tf.getmember(script)
                m.name = final_name
                tf.extract(m, vdir)
            except KeyError:
                for m in tf.getmembers():
                    if m.name.endswith('/exiftool') or m.name == 'exiftool':
                        m.name = final_name
                        tf.extract(m, vdir)
                        break
        dst = os.path.join(vdir, final_name)
        if os.path.isfile(dst):
            st = os.stat(dst)
            os.chmod(dst, st.st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
            self.log.emit("Executable permissions set.")


# ---------------------------------------------------------------------------
#  Wizard dialog
# ---------------------------------------------------------------------------

class ExifToolWizard(QDialog):
    """Dialog per scaricare e installare ExifTool nella cartella vendor/."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("GeotagManager — ExifTool Setup")
        self.setMinimumWidth(540)
        self.setMinimumHeight(360)
        self._worker = None
        self._thread = None
        self._build_ui()

    def _build_ui(self):
        lay = QVBoxLayout(self)
        lay.setSpacing(10)

        # Titolo
        lbl_title = QLabel("<b>Installazione ExifTool (bundled)</b>")
        lbl_title.setStyleSheet("font-size:13px;")
        lay.addWidget(lbl_title)

        # Stato corrente
        current = bundled_version()
        if current:
            status_text = (
                f"<b style='color:#27ae60;'>ExifTool {current}</b> "
                f"è già installato nella cartella vendor/ del plugin."
            )
        else:
            status_text = (
                "<b style='color:#e67e22;'>ExifTool non installato.</b><br>"
                "Clicca <b>Scarica ExifTool</b> per installarlo automaticamente "
                "nella cartella vendor/ del plugin — non richiede privilegi amministrativi."
            )
        self.lbl_status = QLabel(status_text)
        self.lbl_status.setWordWrap(True)
        lay.addWidget(self.lbl_status)

        # Info piattaforma e percorso
        plat = "Windows 64-bit" if sys.platform.startswith('win') else platform.system()
        lbl_plat = QLabel(
            f"Platform: <b>{plat}</b><br>"
            f"Vendor folder: <code>{vendor_dir()}</code>"
        )
        lbl_plat.setWordWrap(True)
        lbl_plat.setStyleSheet("font-size:10px; color:#555;")
        lay.addWidget(lbl_plat)

        # Progress bar
        self.progress = QProgressBar()
        self.progress.setVisible(False)
        lay.addWidget(self.progress)

        # Log
        self.log_view = QTextEdit()
        self.log_view.setReadOnly(True)
        self.log_view.setMinimumHeight(120)
        self.log_view.setStyleSheet(
            "background:#1e1e1e; color:#d4d4d4; font-family:monospace; font-size:9px;"
        )
        lay.addWidget(self.log_view)

        # Bottoni
        btn_row = QHBoxLayout()

        self.btn_download = QPushButton("⬇  Download ExifTool")
        self.btn_download.setFixedHeight(30)
        self.btn_download.clicked.connect(self._start_download)
        btn_row.addWidget(self.btn_download)

        self.btn_cancel = QPushButton("Cancel")
        self.btn_cancel.setEnabled(False)
        self.btn_cancel.clicked.connect(self._cancel)
        btn_row.addWidget(self.btn_cancel)

        if current:
            self.btn_reinstall = QPushButton("🔄 Reinstall")
            self.btn_reinstall.setToolTip("Download and reinstall the latest version")
            self.btn_reinstall.clicked.connect(self._start_download)
            btn_row.addWidget(self.btn_reinstall)

        btn_row.addStretch()

        self.btn_close = QPushButton("Close")
        self.btn_close.clicked.connect(self.accept)
        btn_row.addWidget(self.btn_close)

        lay.addLayout(btn_row)

        # Nota legale
        note = QLabel(
            "ExifTool è software libero di Phil Harvey (exiftool.org), "
            "licenza Artistic License / GPL. "
            "Il binario viene installato nella cartella <code>vendor/</code> del plugin "
            "e viene usato automaticamente senza modificare il sistema."
        )
        note.setWordWrap(True)
        note.setStyleSheet("font-size:9px; color:#888;")
        lay.addWidget(note)

    def _log(self, msg):
        self.log_view.append(msg)

    def _start_download(self):
        self.btn_download.setEnabled(False)
        self.btn_cancel.setEnabled(True)
        self.progress.setVisible(True)
        self.progress.setValue(0)
        self.log_view.clear()

        self._worker = DownloadWorker()
        self._thread = QThread()
        self._worker.moveToThread(self._thread)
        self._thread.started.connect(self._worker.run)
        self._worker.log.connect(self._log)
        self._worker.progress.connect(self.progress.setValue)
        self._worker.finished.connect(self._on_finished)
        self._thread.start()

    def _cancel(self):
        if self._worker:
            self._worker.cancel()
        self.btn_cancel.setEnabled(False)
        self._log("Cancelling...")

    def _on_finished(self, success, message):
        self._thread.quit()
        self._thread.wait()
        self.btn_download.setEnabled(True)
        self.btn_cancel.setEnabled(False)

        if success:
            # Invalida cache e rilegge
            from .exif_handler import find_exiftool
            et = find_exiftool(force_rescan=True)
            ver = bundled_version() or "?"
            if et:
                self.lbl_status.setText(
                    f"<b style='color:#27ae60;'>ExifTool {ver}</b> "
                    f"installed and working."
                )
                QgsMessageLog.logMessage(
                    f"GeotagManager: ExifTool {ver} installato in vendor/",
                    "GeotagManager", Qgis.Info
                )
            else:
                self.lbl_status.setText(
                    f"<b style='color:#e74c3c;'>Installazione completata ma "
                    f"ExifTool non risponde. Controlla il log.</b>"
                )
                message = f"{message}\n\nATTENZIONE: il binario non risponde a -ver.\nControlla il log per dettagli."
        else:
            self._log(f"ERRORE: {message}")

        QMessageBox.information(self, "ExifTool Setup", message)
