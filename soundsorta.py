import sys
import os
import struct
import io
import pygame

try:
    from send2trash import send2trash
    HAS_SEND2TRASH = True
except ImportError:
    HAS_SEND2TRASH = False

try:
    import soundfile as sf
    HAS_SOUNDFILE = True
except ImportError:
    HAS_SOUNDFILE = False

from PyQt6.QtCore import Qt, QUrl, QMimeData, QDir, QSize, QThread, pyqtSignal, QModelIndex
from PyQt6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QHBoxLayout, QVBoxLayout,
    QListWidget, QListWidgetItem, QPushButton, QLineEdit, QLabel,
    QFileDialog, QAbstractItemView, QTreeView, QSplitter, QMenu,
    QCheckBox, QMessageBox, QProgressDialog, QDialog, QComboBox
)
from PyQt6.QtGui import QDrag, QFileSystemModel, QPainter, QColor, QPen, QPainterPath, QPixmap, QIcon

from database import (
    init_db, find_audio_files, index_files, delete_sample_from_db, delete_samples_from_db,
    reset_database, add_library_root, get_library_roots, remove_library_root
)
import tags as tag_engine

PEAK_CACHE = {}
INFO_CACHE = {}

# How many sample cards get built at once. The full result set (after tag/folder/
# filename filtering) has no cap; this only controls how many widgets exist at a
# time, so scrolling near the bottom loads the next page instead of freezing the
# UI by building thousands of cards up front.
PAGE_SIZE = 150

SPECTRUM_COLORS = [
    ("#C8375A", "Crimson"),
    ("#B83A85", "Magenta"),
    ("#9B30B0", "Purple"),
    ("#5D38D0", "Indigo"),
    ("#2B52DD", "Royal Blue"),
    ("#2A82D7", "Sky Blue"),
    ("#22A2B2", "Teal"),
    ("#27A868", "Emerald"),
    ("#5BB335", "Lime"),
    ("#C0A822", "Gold"),
    ("#C87A28", "Amber"),
    ("#C54E30", "Coral Orange")
]


def play_audio(file_path):
    """Robust playback function with built-in AIFF / 24-bit codec fallback."""
    if not file_path or not os.path.exists(file_path):
        return

    try:
        pygame.mixer.stop()
        # Direct playback attempt
        sound = pygame.mixer.Sound(file_path)
        sound.play()
    except Exception:
        # Fallback for 24-bit / 32-bit AIFF/AIFC or uncommon encodings using soundfile
        if HAS_SOUNDFILE:
            try:
                data, samplerate = sf.read(file_path, dtype='int16')
                buffer = io.BytesIO()
                sf.write(buffer, data, samplerate, format='WAV', subtype='PCM_16')
                buffer.seek(0)
                sound = pygame.mixer.Sound(buffer)
                sound.play()
            except Exception as err:
                print(f"Error rendering playback for {file_path}: {err}")
        else:
            print(f"Cannot play {file_path}. Install soundfile (`pip install soundfile`) for full AIFF support.")


def create_color_icon(hex_color, size=16):
    pixmap = QPixmap(size, size)
    pixmap.fill(Qt.GlobalColor.transparent)

    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.setBrush(QColor(hex_color))
    painter.setPen(QPen(QColor("#ffffff"), 1))
    painter.drawRoundedRect(0, 0, size - 1, size - 1, 4, 4)
    painter.end()

    return QIcon(pixmap)


class ScanWorker(QThread):
    progress = pyqtSignal(int, int, str)  # (current_count, total_count, current_filename)
    finished_scan = pyqtSignal(int)

    def __init__(self, folder_path):
        super().__init__()
        self.folder_path = folder_path
        self.is_cancelled = False

    def cancel(self):
        self.is_cancelled = True

    def run(self):
        all_files = find_audio_files(self.folder_path, should_cancel=lambda: self.is_cancelled)
        total_files = len(all_files)

        if total_files == 0 or self.is_cancelled:
            self.finished_scan.emit(0)
            return

        def update_cb(count, current_file):
            if not self.is_cancelled:
                self.progress.emit(count, total_files, current_file)

        total = index_files(
            all_files, progress_callback=update_cb, should_cancel=lambda: self.is_cancelled
        )
        self.finished_scan.emit(total)


class TagWorker(QThread):
    """
    Applies and/or removes a batch of tags on a batch of samples in a single
    transaction each (see tags.py), off the UI thread so a very large checked
    set doesn't stall the interface.
    """
    finished_tagging = pyqtSignal()

    def __init__(self, paths, apply_tag_names, remove_tag_names):
        super().__init__()
        self.paths = paths
        self.apply_tag_names = apply_tag_names
        self.remove_tag_names = remove_tag_names

    def run(self):
        if self.apply_tag_names:
            tag_engine.apply_tags_to_samples(self.paths, self.apply_tag_names)
        if self.remove_tag_names:
            tag_engine.remove_tags_from_samples(self.paths, self.remove_tag_names)
        self.finished_tagging.emit()


class WaveformWidget(QWidget):
    def __init__(self, file_path, parent=None):
        super().__init__(parent)
        self.file_path = file_path
        self.setFixedHeight(60)
        self.peaks = []
        self._extract_peaks()

    def _extract_peaks(self):
        if self.file_path in PEAK_CACHE:
            self.peaks = PEAK_CACHE[self.file_path]
            return

        if not os.path.exists(self.file_path):
            return

        ext = self.file_path.lower()
        supported_exts = ('.wav', '.flac', '.mp3', '.ogg', '.aif', '.aiff')
        if not ext.endswith(supported_exts):
            return

        try:
            peaks = []
            if HAS_SOUNDFILE:
                # Use soundfile to read audio data universally (WAV, FLAC, MP3, OGG, AIFF)
                data, samplerate = sf.read(self.file_path, always_2d=True)
                samples = data.mean(axis=1)

                if len(samples) > 0:
                    resolution = 250
                    chunk_size = max(1, len(samples) // resolution)
                    for i in range(0, len(samples), chunk_size):
                        chunk = samples[i:i + chunk_size]
                        if len(chunk) > 0:
                            max_val = max(abs(s) for s in chunk)
                            peaks.append(float(max_val))
            else:
                # Fallback to standard wave library if soundfile isn't installed
                if ext.endswith('.wav'):
                    import wave
                    with wave.open(self.file_path, 'rb') as wf:
                        n_channels = wf.getnchannels()
                        sampwidth = wf.getsampwidth()
                        n_frames = wf.getnframes()

                        if n_frames > 0 and sampwidth == 2:
                            raw_data = wf.readframes(n_frames)
                            total_samples = len(raw_data) // 2
                            fmt = f"<{total_samples}h"
                            samples = struct.unpack(fmt, raw_data)

                            if n_channels > 1:
                                samples = samples[::n_channels]

                            resolution = 250
                            chunk_size = max(1, len(samples) // resolution)
                            for i in range(0, len(samples), chunk_size):
                                chunk = samples[i:i + chunk_size]
                                if chunk:
                                    max_val = max(abs(s) for s in chunk)
                                    peaks.append(max_val / 32768.0)

            PEAK_CACHE[self.file_path] = peaks
            self.peaks = peaks
        except Exception as e:
            print(f"Waveform generation error for {self.file_path}: {e}")
            self.peaks = []

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)

        rect = self.rect()
        painter.setBrush(QColor("#141a21"))
        painter.setPen(QPen(QColor("#232d38"), 1))
        painter.drawRoundedRect(rect, 6, 6)

        if not self.peaks:
            painter.end()
            return

        path = QPainterPath()
        width = self.width() - 16
        height = self.height() - 12
        center_y = self.height() / 2
        step = width / max(1, len(self.peaks))

        first = True
        for i, amp in enumerate(self.peaks):
            x = 8 + (i * step)
            y = center_y - (max(0.05, amp) * (height / 2))
            if first:
                path.moveTo(x, y)
                first = False
            else:
                path.lineTo(x, y)

        for i in reversed(range(len(self.peaks))):
            amp = self.peaks[i]
            x = 8 + (i * step)
            y = center_y + (max(0.05, amp) * (height / 2))
            path.lineTo(x, y)

        path.closeSubpath()

        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor("#007ACC"))
        painter.drawPath(path)
        painter.end()


class SampleCardWidget(QWidget):
    def __init__(self, sample_data, show_waveform=True, main_window=None, parent=None):
        super().__init__(parent)
        self.sample_data = sample_data
        self.main_window = main_window

        file_path = sample_data['file_path']
        file_name = sample_data['file_name']
        tags = sample_data.get('tags', [])
        tag_colors = sample_data.get('tag_colors', {})

        card_layout = QVBoxLayout(self)
        card_layout.setContentsMargins(10, 8, 10, 8)
        card_layout.setSpacing(4)

        header_layout = QHBoxLayout()
        header_layout.setSpacing(8)

        self.chk_select = QCheckBox()
        self.chk_select.setToolTip("Check for tagging or deletion (does not affect drag-out)")
        if self.main_window is not None:
            self.chk_select.setChecked(file_path in self.main_window.checked_paths)
        self.chk_select.stateChanged.connect(self._on_checked_changed)
        header_layout.addWidget(self.chk_select)

        btn_play = QPushButton("►")
        btn_play.setFixedSize(24, 24)
        btn_play.setToolTip("Play / Replay Sample")
        btn_play.setStyleSheet("""
            QPushButton {
                background: transparent;
                border: none;
                color: #00aaff;
                font-size: 16px;
                font-weight: bold;
                padding: 0px;
            }
            QPushButton:hover {
                color: #ffffff;
            }
        """)
        btn_play.clicked.connect(self.play_self)
        header_layout.addWidget(btn_play)

        lbl_title = QLabel(f"► {file_name}")
        lbl_title.setStyleSheet("font-size: 15px; font-weight: bold; color: #ffffff;")
        header_layout.addWidget(lbl_title, 1)

        btn_delete = QPushButton("X")
        btn_delete.setFixedSize(24, 24)
        btn_delete.setToolTip("Move file to Recycle Bin")
        btn_delete.setStyleSheet("""
            QPushButton {
                background: transparent;
                border: none;
                color: #ff5555;
                font-size: 15px;
                font-weight: bold;
                padding: 0px;
            }
            QPushButton:hover {
                color: #ff8888;
            }
        """)
        btn_delete.clicked.connect(self.delete_self)
        header_layout.addWidget(btn_delete)

        card_layout.addLayout(header_layout)

        if tags:
            tag_box = QHBoxLayout()
            tag_box.setSpacing(6)
            tag_box.setAlignment(Qt.AlignmentFlag.AlignLeft)

            for t in tags:
                bg_color = tag_colors.get(t, "#255d8f")
                lbl_tag = QLabel(f"[ {t} ]")
                lbl_tag.setStyleSheet(f"""
                    background-color: {bg_color};
                    color: #ffffff;
                    font-size: 11px;
                    font-weight: bold;
                    padding: 2px 6px;
                    border-radius: 4px;
                """)
                tag_box.addWidget(lbl_tag)

            card_layout.addLayout(tag_box)

        if show_waveform:
            self.waveform = WaveformWidget(file_path)
            card_layout.addWidget(self.waveform)

        file_size_mb = 0.0
        duration_str = "-.-"

        if file_path in INFO_CACHE:
            file_size_mb, duration_str = INFO_CACHE[file_path]
        elif os.path.exists(file_path):
            file_size_mb = round(os.path.getsize(file_path) / (1024 * 1024), 2)
            try:
                if HAS_SOUNDFILE:
                    info = sf.info(file_path)
                    if info.duration > 0:
                        duration_str = f"{round(info.duration, 1)}s"
                else:
                    import wave
                    with wave.open(file_path, 'rb') as wf:
                        frames = wf.getnframes()
                        rate = wf.getframerate()
                        if rate > 0:
                            duration_str = f"{round(frames / float(rate), 1)}s"
            except Exception:
                pass
            INFO_CACHE[file_path] = (file_size_mb, duration_str)

        lbl_meta = QLabel(f"Size: {file_size_mb} MB | Dur: {duration_str} | Path: {file_path}")
        lbl_meta.setStyleSheet("font-size: 11px; color: #8a9bb0; padding-top: 2px;")
        card_layout.addWidget(lbl_meta)

    def _on_checked_changed(self, state):
        if self.main_window is not None:
            self.main_window.on_sample_checked_changed(
                self.sample_data['file_path'], self.chk_select.isChecked()
            )

    def play_self(self):
        file_path = self.sample_data['file_path']
        play_audio(file_path)

    def delete_self(self):
        file_path = self.sample_data['file_path']
        confirm = QMessageBox.question(
            self,
            "Confirm Safe Delete",
            f"Are you sure you want to move this file to the Recycle Bin?\n\n{file_path}",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No
        )
        if confirm == QMessageBox.StandardButton.Yes:
            try:
                if HAS_SEND2TRASH:
                    send2trash(file_path)
                else:
                    os.remove(file_path)
                delete_sample_from_db(file_path)
                PEAK_CACHE.pop(file_path, None)
                INFO_CACHE.pop(file_path, None)
                if self.main_window:
                    self.main_window.checked_paths.discard(file_path)
                    self.main_window.refresh_tags()
                    self.main_window.refresh_samples()
            except Exception as e:
                QMessageBox.critical(self, "Delete Error", f"Could not delete file:\n{e}")


class DraggableSampleList(QListWidget):
    """
    Native list selection here is the app's 'selected' concept — used only for
    keyboard/mouse navigation, click-to-play, and dragging files out to a DAW.
    It is intentionally separate from the per-card checkboxes ('checked'), which
    drive tagging and deletion. Don't wire selection into batch tag/delete actions.
    """
    def __init__(self, main_window=None, parent=None):
        super().__init__(parent)
        self.main_window = main_window
        self.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.setDragEnabled(True)

    def keyPressEvent(self, event):
        super().keyPressEvent(event)
        if event.key() in (Qt.Key.Key_Up, Qt.Key.Key_Down, Qt.Key.Key_Space):
            if self.main_window:
                self.main_window.play_selected_sample()

    def startDrag(self, supportedActions):
        selected_items = self.selectedItems()
        if not selected_items:
            return

        urls = []
        for item in selected_items:
            file_path = item.data(Qt.ItemDataRole.UserRole)
            if file_path and os.path.exists(file_path):
                urls.append(QUrl.fromLocalFile(file_path))

        if urls:
            mime_data = QMimeData()
            mime_data.setUrls(urls)

            drag = QDrag(self)
            drag.setMimeData(mime_data)
            drag.exec(Qt.DropAction.CopyAction)


class StageCheckBox(QCheckBox):
    """
    A checkbox that can be SET to a partially-checked state programmatically
    (to show 'some but not all checked samples have this tag') but that only
    ever toggles between Checked and Unchecked when the user actually clicks it.
    """
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setTristate(True)

    def nextCheckState(self):
        if self.checkState() == Qt.CheckState.Checked:
            self.setCheckState(Qt.CheckState.Unchecked)
        else:
            self.setCheckState(Qt.CheckState.Checked)


class TagRowWidget(QWidget):
    """
    One row in the tag panel. The stage checkbox on the left stages this tag for
    apply/remove against whatever samples are currently checked. Clicking
    anywhere else on the row toggles this tag as a view filter. These are two
    separate actions on purpose — see amnesia cure notes on selection vs filtering.
    """
    def __init__(self, tag_name, count, color, main_window, parent=None):
        super().__init__(parent)
        self.tag_name = tag_name
        self.main_window = main_window

        layout = QHBoxLayout(self)
        layout.setContentsMargins(8, 4, 8, 4)
        layout.setSpacing(8)

        self.stage_checkbox = StageCheckBox()
        self.stage_checkbox.setToolTip("Stage this tag to apply/remove on checked samples")
        self.stage_checkbox.stateChanged.connect(self._on_stage_changed)
        layout.addWidget(self.stage_checkbox)

        self.icon_label = QLabel()
        self.icon_label.setPixmap(create_color_icon(color, 14).pixmap(14, 14))
        layout.addWidget(self.icon_label)

        self.text_label = QLabel()
        self.text_label.setStyleSheet("font-size: 14px; font-weight: bold; color: #ffffff;")
        layout.addWidget(self.text_label, 1)

        self.set_display(tag_name, count, is_filter_active=False)

    def set_display(self, tag_name, count, is_filter_active):
        prefix = "✓ " if is_filter_active else ""
        self.text_label.setText(f"{prefix}{tag_name} ({count})")
        border = "1px solid #007acc" if is_filter_active else "1px solid transparent"
        self.setStyleSheet(f"background-color: #212833; border-radius: 6px; border: {border};")

    def set_stage_state(self, qt_check_state):
        self.stage_checkbox.blockSignals(True)
        self.stage_checkbox.setCheckState(qt_check_state)
        self.stage_checkbox.blockSignals(False)

    def mousePressEvent(self, event):
        pos = event.position().toPoint() if hasattr(event, 'position') else event.pos()
        child = self.childAt(pos)
        if child is not self.stage_checkbox:
            self.main_window.toggle_tag_filter_by_name(self.tag_name)
        super().mousePressEvent(event)

    def _on_stage_changed(self, state):
        self.main_window.on_tag_stage_changed(self.tag_name, Qt.CheckState(state))


class TagPanelList(QListWidget):
    def __init__(self, main_window, parent=None):
        super().__init__(parent)
        self.main_window = main_window
        self.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.customContextMenuRequested.connect(self.open_swatch_menu)

    def open_swatch_menu(self, position):
        item = self.itemAt(position)
        if not item:
            return

        widget = self.itemWidget(item)
        tag_name = widget.tag_name if widget else None
        if not tag_name:
            return

        menu = QMenu(self)
        menu.setTitle(f"Tag Options: {tag_name}")

        for hex_code, color_name in SPECTRUM_COLORS:
            icon = create_color_icon(hex_code, 16)
            action = menu.addAction(icon, f"Set Color: {color_name}")
            action.setData(('color', hex_code))

        menu.addSeparator()
        act_delete_tag = menu.addAction("X Delete Tag")
        act_delete_tag.setData(('delete', None))

        selected_action = menu.exec(self.mapToGlobal(position))
        if selected_action:
            action_data = selected_action.data()
            if action_data:
                act_type, payload = action_data
                if act_type == 'color':
                    tag_engine.update_tag_color(tag_name, payload)
                elif act_type == 'delete':
                    tag_engine.delete_tag(tag_name)
                    self.main_window.active_tag_filters.discard(tag_name)
                    self.main_window.staged_apply_tags.discard(tag_name)
                    self.main_window.staged_remove_tags.discard(tag_name)

                self.main_window.refresh_tags()
                self.main_window.refresh_samples()


class LibraryRootsDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Library Root Paths Manager")
        self.resize(500, 350)

        layout = QVBoxLayout(self)

        self.list_roots = QListWidget()
        layout.addWidget(self.list_roots)

        btn_box = QHBoxLayout()
        btn_add = QPushButton("Add Path")
        btn_add.clicked.connect(self.add_path)
        btn_remove = QPushButton("Remove Path")
        btn_remove.clicked.connect(self.remove_path)

        btn_box.addWidget(btn_add)
        btn_box.addWidget(btn_remove)
        layout.addLayout(btn_box)

        self.refresh_list()

    def refresh_list(self):
        self.list_roots.clear()
        roots = get_library_roots()
        for r in roots:
            self.list_roots.addItem(r)

    def add_path(self):
        folder = QFileDialog.getExistingDirectory(self, "Select Root Library Folder")
        if folder:
            add_library_root(folder)
            self.refresh_list()

    def remove_path(self):
        item = self.list_roots.currentItem()
        if item:
            remove_library_root(item.text())
            self.refresh_list()


class SoundSortaApp(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("SoundSorta - Non-Destructive Audio Tag Engine")
        self.resize(1380, 840)

        pygame.mixer.pre_init(44100, -16, 2, 512)
        pygame.mixer.init()

        self.active_tag_filters = set()
        self.active_folder_filter = None
        self.show_waveforms = True

        # 'Checked' samples (tagging/deletion target) persist across list rebuilds
        # (filter changes, paging, search) independently of the widgets on screen.
        self.checked_paths = set()

        # Tags ticked in the panel but not yet written to the database.
        self.staged_apply_tags = set()
        self.staged_remove_tags = set()

        # The tag/folder-filtered result set (no cap) and how much of it has been
        # turned into on-screen cards so far.
        self.base_samples = []
        self.current_filtered_samples = []
        self.visible_count = 0

        self.init_ui()
        self.refresh_library_roots()
        self.refresh_tags()
        self.refresh_samples()

    def init_ui(self):
        self.setStyleSheet("""
            QMainWindow, QDialog, QMessageBox {
                background-color: #1a1e24;
                color: #e0e0e0;
            }
            QWidget {
                color: #e0e0e0;
                font-family: 'Segoe UI', sans-serif;
                font-size: 14px;
            }
            QMessageBox QLabel, QDialog QLabel {
                color: #ffffff;
                font-size: 13px;
            }
            QCheckBox::indicator {
                width: 18px;
                height: 18px;
                border: 2px solid #ffffff;
                border-radius: 4px;
                background-color: #12151a;
            }
            QCheckBox::indicator:hover {
                border-color: #007acc;
            }
            QCheckBox::indicator:checked {
                background-color: #ffffff;
                border-color: #ffffff;
                image: url("data:image/svg+xml;utf8,<svg xmlns='http://www.w3.org/2000/svg' width='16' height='16' viewBox='0 0 24 24' fill='none' stroke='%231b5e20' stroke-width='4' stroke-linecap='round' stroke-linejoin='round'><polyline points='20 6 9 17 4 12'/></svg>");
            }
            QCheckBox::indicator:indeterminate {
                background-color: #007acc;
                border-color: #007acc;
                image: url("data:image/svg+xml;utf8,<svg xmlns='http://www.w3.org/2000/svg' width='16' height='16' viewBox='0 0 24 24' fill='none' stroke='%23ffffff' stroke-width='4' stroke-linecap='round' stroke-linejoin='round'><line x1='5' y1='12' x2='19' y2='12'/></svg>");
            }
            QSplitter::handle {
                background-color: #28303d;
                width: 4px;
            }
            QSplitter::handle:hover {
                background-color: #007acc;
            }
            QScrollBar:vertical {
                background: #12151a;
                width: 12px;
                margin: 0px;
            }
            QScrollBar::handle:vertical {
                background: #3d4857;
                min-height: 24px;
                border-radius: 4px;
                margin: 2px;
            }
            QScrollBar::handle:vertical:hover {
                background: #007acc;
            }
            QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {
                height: 0px;
            }
            QMenu {
                background-color: #1c222a;
                color: #ffffff;
                border: 1px solid #3d4857;
                border-radius: 6px;
                padding: 4px;
            }
            QMenu::item {
                padding: 6px 20px 6px 10px;
                border-radius: 4px;
                background-color: transparent;
                color: #e0e0e0;
                font-weight: bold;
            }
            QMenu::item:selected {
                background-color: #007acc;
                color: #ffffff;
            }
            QComboBox {
                background-color: #12151a;
                border: 1px solid #3d4857;
                border-radius: 6px;
                padding: 6px;
                color: #ffffff;
                font-weight: bold;
            }
            QComboBox::drop-down { border: none; }
            QComboBox QAbstractItemView {
                background-color: #1c222a;
                color: #ffffff;
                selection-background-color: #007acc;
            }
            QProgressDialog {
                background-color: #1c222a;
            }
            QPushButton {
                background-color: #2a323d; border: 1px solid #3d4857;
                border-radius: 6px; padding: 7px 14px; font-weight: bold;
                color: #ffffff;
            }
            QPushButton:hover { background-color: #384352; }
            QPushButton:disabled { color: #5a6472; background-color: #22272e; }
            QPushButton:checked { background-color: #007acc; border-color: #005999; }
            QLineEdit {
                background-color: #12151a; border: 1px solid #3d4857;
                border-radius: 6px; padding: 7px; color: #fff;
            }
            QListWidget, QTreeView {
                background-color: #12151a; border: 1px solid #28303d;
                border-radius: 8px; padding: 6px;
            }
            QListWidget#sample_list_widget::item {
                margin-bottom: 8px; border-radius: 6px;
                background-color: #1d232c;
            }
            QListWidget#sample_list_widget::item:selected {
                background-color: #242e3d; border: 1px solid #007acc;
            }
            QListWidget#tag_list_widget::item {
                margin-bottom: 6px;
                border-radius: 6px;
            }
            QHeaderView::section {
                background-color: #2a323d; color: #aaa; padding: 4px; border: none;
            }
        """)

        main_layout = QVBoxLayout()

        top_bar = QHBoxLayout()
        top_bar.setContentsMargins(0, 0, 0, 0)

        self.btn_import = QPushButton("Scan Folder")
        self.btn_import.clicked.connect(self.import_folder_dialog)

        self.btn_toggle_wave = QPushButton("Waveforms")
        self.btn_toggle_wave.setCheckable(True)
        self.btn_toggle_wave.setChecked(True)
        self.btn_toggle_wave.clicked.connect(self.toggle_waveforms)

        self.btn_toggle_tree = QPushButton("Folder Tree")
        self.btn_toggle_tree.setCheckable(True)
        self.btn_toggle_tree.setChecked(True)
        self.btn_toggle_tree.clicked.connect(self.toggle_folder_tree)

        self.btn_reset_filters = QPushButton("Reset Filters")
        self.btn_reset_filters.clicked.connect(self.clear_all_filters)

        self.btn_manage_roots = QPushButton("Roots")
        self.btn_manage_roots.clicked.connect(self.open_roots_dialog)

        self.btn_reset_db = QPushButton("Reset DB")
        self.btn_reset_db.setStyleSheet("background-color: #3b1e24; border-color: #612732; color: #ff7777;")
        self.btn_reset_db.clicked.connect(self.confirm_reset_db)

        self.search_bar = QLineEdit()
        self.search_bar.setPlaceholderText("Filter by filename...")
        self.search_bar.textChanged.connect(self.filter_filename)

        self.lbl_status = QLabel("Ready")

        top_bar.addWidget(self.btn_import)
        top_bar.addWidget(self.btn_toggle_wave)
        top_bar.addWidget(self.btn_toggle_tree)
        top_bar.addWidget(self.btn_reset_filters)
        top_bar.addWidget(self.btn_manage_roots)
        top_bar.addWidget(self.btn_reset_db)
        top_bar.addWidget(self.search_bar, 2)
        top_bar.addWidget(self.lbl_status)

        main_layout.addLayout(top_bar, 0)

        batch_bar = QHBoxLayout()
        batch_bar.setContentsMargins(0, 4, 0, 4)

        btn_select_all = QPushButton("Check All")
        btn_select_all.setToolTip("Check every sample matching the current filters (for tagging/deletion)")
        btn_select_all.clicked.connect(lambda: self.set_all_checkboxes(True))
        btn_deselect_all = QPushButton("Uncheck All")
        btn_deselect_all.clicked.connect(lambda: self.set_all_checkboxes(False))

        btn_batch_delete = QPushButton("X Delete Checked")
        btn_batch_delete.setStyleSheet("background: #3d2f2f; border-color: #ff6666; color: #ffffff;")
        btn_batch_delete.clicked.connect(self.delete_checked_samples)

        self.lbl_checked_count = QLabel("0 checked")
        self.lbl_checked_count.setStyleSheet("color: #8a9bb0;")

        batch_bar.addWidget(btn_select_all)
        batch_bar.addWidget(btn_deselect_all)
        batch_bar.addWidget(btn_batch_delete)
        batch_bar.addWidget(self.lbl_checked_count)
        batch_bar.addStretch()

        main_layout.addLayout(batch_bar, 0)

        self.splitter = QSplitter(Qt.Orientation.Horizontal)

        left_widget = QWidget()
        left_box = QVBoxLayout(left_widget)
        left_box.setContentsMargins(0, 0, 0, 0)

        lbl_tags = QLabel("TAGS (checkbox stages tag · click name to filter)")
        lbl_tags.setStyleSheet("font-weight: bold; color: #8888a0; margin-bottom: 4px;")

        self.tag_list = TagPanelList(self)
        self.tag_list.setObjectName("tag_list_widget")

        tag_creator_box = QHBoxLayout()
        self.txt_new_tag = QLineEdit()
        self.txt_new_tag.setPlaceholderText("New tag name...")
        btn_add_tag = QPushButton("+")
        btn_add_tag.clicked.connect(self.add_custom_tag)
        tag_creator_box.addWidget(self.txt_new_tag)
        tag_creator_box.addWidget(btn_add_tag)

        stage_actions_box = QHBoxLayout()
        self.btn_apply_staged = QPushButton("Apply Staged Tags")
        self.btn_apply_staged.setToolTip("Apply/remove the staged tags on every checked sample")
        self.btn_apply_staged.clicked.connect(self.apply_staged_tags)
        self.btn_clear_staged = QPushButton("Clear Staged")
        self.btn_clear_staged.clicked.connect(self.clear_staged_tags)
        stage_actions_box.addWidget(self.btn_apply_staged)
        stage_actions_box.addWidget(self.btn_clear_staged)

        self.lbl_staged_status = QLabel("No tags staged")
        self.lbl_staged_status.setStyleSheet("color: #8a9bb0; font-size: 12px;")

        left_box.addWidget(lbl_tags)
        left_box.addWidget(self.tag_list, 1)
        left_box.addLayout(tag_creator_box)
        left_box.addLayout(stage_actions_box)
        left_box.addWidget(self.lbl_staged_status)

        center_widget = QWidget()
        center_box = QVBoxLayout(center_widget)
        center_box.setContentsMargins(0, 0, 0, 0)

        lbl_samples = QLabel("SAMPLES (Click to Play/Replay | Drag Card to DAW)")
        lbl_samples.setStyleSheet("font-weight: bold; color: #8888a0; margin-bottom: 4px;")

        self.sample_list = DraggableSampleList(main_window=self)
        self.sample_list.setObjectName("sample_list_widget")
        self.sample_list.itemClicked.connect(self.play_selected_sample_item)
        self.sample_list.verticalScrollBar().valueChanged.connect(self.on_sample_scroll)

        center_box.addWidget(lbl_samples)
        center_box.addWidget(self.sample_list, 1)

        self.right_widget = QWidget()
        right_box = QVBoxLayout(self.right_widget)
        right_box.setContentsMargins(0, 0, 0, 0)

        lbl_tree = QLabel("FOLDER EXPLORER")
        lbl_tree.setStyleSheet("font-weight: bold; color: #8888a0; margin-bottom: 4px;")

        self.combo_roots = QComboBox()
        self.combo_roots.currentIndexChanged.connect(self.on_root_combo_changed)

        self.dir_model = QFileSystemModel()
        self.dir_model.setRootPath(QDir.rootPath())
        self.dir_model.setFilter(QDir.Filter.AllDirs | QDir.Filter.NoDotAndDotDot | QDir.Filter.Drives)

        self.tree_view = QTreeView()
        self.tree_view.setModel(self.dir_model)
        self.tree_view.setColumnHidden(1, True)
        self.tree_view.setColumnHidden(2, True)
        self.tree_view.setColumnHidden(3, True)
        self.tree_view.setHeaderHidden(True)
        self.tree_view.clicked.connect(self.on_folder_selected)

        self.btn_clear_folder = QPushButton("Clear Folder Filter")
        self.btn_clear_folder.clicked.connect(self.clear_folder_filter)

        right_box.addWidget(lbl_tree)
        right_box.addWidget(self.combo_roots)
        right_box.addWidget(self.tree_view, 1)
        right_box.addWidget(self.btn_clear_folder)

        self.splitter.addWidget(left_widget)
        self.splitter.addWidget(center_widget)
        self.splitter.addWidget(self.right_widget)
        self.splitter.setSizes([280, 720, 250])

        main_layout.addWidget(self.splitter, 1)

        container = QWidget()
        container.setLayout(main_layout)
        self.setCentralWidget(container)

    # ---------------------------------------------------------------- roots --

    def refresh_library_roots(self):
        self.combo_roots.blockSignals(True)
        self.combo_roots.clear()
        roots = get_library_roots()

        if roots:
            for r in roots:
                folder_name = os.path.basename(r) or r
                self.combo_roots.addItem(f"📁 {folder_name}", r)
            self.combo_roots.setEnabled(True)

            first_path = roots[0]
            idx = self.dir_model.setRootPath(first_path)
            self.tree_view.setRootIndex(idx)
        else:
            self.combo_roots.addItem("📁 All Drives (No Roots Set)", QDir.rootPath())
            self.dir_model.setRootPath(QDir.rootPath())
            self.tree_view.setRootIndex(QModelIndex())

        self.combo_roots.blockSignals(False)

    def on_root_combo_changed(self, index):
        if index < 0:
            return
        path = self.combo_roots.itemData(index)
        if path:
            if path == QDir.rootPath():
                self.dir_model.setRootPath(QDir.rootPath())
                self.tree_view.setRootIndex(QModelIndex())
            else:
                idx = self.dir_model.setRootPath(path)
                self.tree_view.setRootIndex(idx)

    # --------------------------------------------------------------- toggles --

    def toggle_waveforms(self):
        self.show_waveforms = self.btn_toggle_wave.isChecked()
        self.refresh_samples()

    def toggle_folder_tree(self):
        is_visible = self.right_widget.isVisible()
        self.right_widget.setVisible(not is_visible)
        self.btn_toggle_tree.setChecked(not is_visible)

    # ----------------------------------------------------------------- scan --

    def import_folder_dialog(self):
        folder = QFileDialog.getExistingDirectory(self, "Select Sample Directory")
        if folder:
            self.start_threaded_scan(os.path.normpath(folder))

    def start_threaded_scan(self, norm_folder):
        self.progress_dialog = QProgressDialog("Analyzing directory...", "Cancel", 0, 100, self)
        self.progress_dialog.setWindowTitle("Scanning Samples")
        self.progress_dialog.setWindowModality(Qt.WindowModality.WindowModal)
        self.progress_dialog.setMinimumDuration(0)
        self.progress_dialog.show()

        self.scan_thread = ScanWorker(norm_folder)
        self.scan_thread.progress.connect(self.on_scan_progress)
        self.scan_thread.finished_scan.connect(self.on_scan_finished)
        self.progress_dialog.canceled.connect(self.scan_thread.cancel)
        self.scan_thread.start()

    def on_scan_progress(self, current, total, current_file):
        if total > 0:
            self.progress_dialog.setMaximum(total)
            self.progress_dialog.setValue(current)
            percent = int((current / total) * 100)
            self.progress_dialog.setLabelText(
                f"Scanning ({percent}%):\n{current} / {total} files\n{os.path.basename(current_file)}"
            )

    def on_scan_finished(self, total):
        self.progress_dialog.close()
        self.refresh_tags()
        self.refresh_samples()
        self.lbl_status.setText(f"Indexed {total} samples")

    # ---------------------------------------------------------- tag staging --

    def toggle_tag_filter_by_name(self, tag_name):
        if tag_name in self.active_tag_filters:
            self.active_tag_filters.remove(tag_name)
        else:
            self.active_tag_filters.add(tag_name)
        self.refresh_tags()
        self.refresh_samples()

    def on_tag_stage_changed(self, tag_name, qt_check_state):
        if qt_check_state == Qt.CheckState.Checked:
            self.staged_apply_tags.add(tag_name)
            self.staged_remove_tags.discard(tag_name)
        elif qt_check_state == Qt.CheckState.Unchecked:
            self.staged_remove_tags.add(tag_name)
            self.staged_apply_tags.discard(tag_name)
        # PartiallyChecked never arrives here from a real click (StageCheckBox
        # only toggles Checked/Unchecked) - it's display-only.
        self.update_staged_status_label()

    def update_staged_status_label(self):
        parts = []
        if self.staged_apply_tags:
            parts.append(f"+{len(self.staged_apply_tags)}")
        if self.staged_remove_tags:
            parts.append(f"-{len(self.staged_remove_tags)}")
        self.lbl_staged_status.setText(
            f"Staged: {' '.join(parts)}" if parts else "No tags staged"
        )

    def clear_staged_tags(self):
        self.staged_apply_tags.clear()
        self.staged_remove_tags.clear()
        self.refresh_tag_stage_states()
        self.update_staged_status_label()

    def apply_staged_tags(self):
        checked = list(self.checked_paths)
        if not checked:
            QMessageBox.information(self, "Nothing Checked", "Check one or more samples first.")
            return
        if not self.staged_apply_tags and not self.staged_remove_tags:
            QMessageBox.information(self, "Nothing Staged", "Tick a tag checkbox to stage it first.")
            return

        self.lbl_status.setText(f"Updating tags on {len(checked)} sample(s)...")
        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)

        self.tag_thread = TagWorker(
            checked, list(self.staged_apply_tags), list(self.staged_remove_tags)
        )
        self.tag_thread.finished_tagging.connect(self.on_tag_finished)
        self.tag_thread.start()

    def on_tag_finished(self):
        QApplication.restoreOverrideCursor()
        self.staged_apply_tags.clear()
        self.staged_remove_tags.clear()
        self.update_staged_status_label()
        self.refresh_tags()
        self.refresh_samples()
        self.lbl_status.setText("Tags updated.")

    # -------------------------------------------------------------- folders --

    def on_folder_selected(self, index):
        folder_path = self.dir_model.filePath(index)
        if os.path.isdir(folder_path):
            norm_folder = os.path.normpath(folder_path)
            self.active_folder_filter = norm_folder
            self.start_threaded_scan(norm_folder)

    def clear_folder_filter(self):
        self.active_folder_filter = None
        self.tree_view.clearSelection()
        self.refresh_samples()

    def clear_all_filters(self):
        self.active_tag_filters.clear()
        self.active_folder_filter = None
        self.search_bar.clear()
        self.tree_view.clearSelection()
        self.refresh_tags()
        self.refresh_samples()

    @staticmethod
    def _path_in_folder(file_path, folder_path):
        sample_dir = os.path.normpath(os.path.dirname(file_path))
        filter_dir = os.path.normpath(folder_path)
        try:
            return os.path.commonpath([sample_dir, filter_dir]) == filter_dir
        except ValueError:
            return False

    # ------------------------------------------------------ checked samples --

    def on_sample_checked_changed(self, file_path, is_checked):
        if is_checked:
            self.checked_paths.add(file_path)
        else:
            self.checked_paths.discard(file_path)
        self.update_checked_status_label()
        self.refresh_tag_stage_states()

    def update_checked_status_label(self):
        self.lbl_checked_count.setText(f"{len(self.checked_paths)} checked")

    def set_all_checkboxes(self, checked):
        if checked:
            self.checked_paths = {s['file_path'] for s in self.current_filtered_samples}
        else:
            self.checked_paths.clear()

        # Sync whatever cards are currently materialized to match.
        for i in range(self.sample_list.count()):
            item = self.sample_list.item(i)
            widget = self.sample_list.itemWidget(item)
            if widget and hasattr(widget, 'chk_select'):
                widget.chk_select.blockSignals(True)
                widget.chk_select.setChecked(checked)
                widget.chk_select.blockSignals(False)

        self.update_checked_status_label()
        self.refresh_tag_stage_states()

    def delete_checked_samples(self):
        checked_paths = list(self.checked_paths)

        if not checked_paths:
            QMessageBox.information(self, "No Selection", "No samples were checked for deletion.")
            return

        confirm = QMessageBox.question(
            self,
            "Confirm Batch Delete",
            f"Are you sure you want to move {len(checked_paths)} files to the Recycle Bin?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No
        )
        if confirm == QMessageBox.StandardButton.Yes:
            deleted_paths = []
            for path in checked_paths:
                try:
                    if HAS_SEND2TRASH:
                        send2trash(path)
                    else:
                        os.remove(path)
                    PEAK_CACHE.pop(path, None)
                    INFO_CACHE.pop(path, None)
                    deleted_paths.append(path)
                except Exception as e:
                    print(f"Error deleting {path}: {e}")

            delete_samples_from_db(deleted_paths)
            self.checked_paths.difference_update(deleted_paths)
            self.update_checked_status_label()
            self.refresh_tags()
            self.refresh_samples()

    # ------------------------------------------------------------------ db --

    def open_roots_dialog(self):
        dialog = LibraryRootsDialog(self)
        dialog.exec()
        self.refresh_library_roots()
        self.refresh_samples()

    def confirm_reset_db(self):
        confirm = QMessageBox.question(
            self,
            "Reset Database",
            "This will clear ALL indexed samples, custom tags, and associations.\nYour actual audio files will NOT be deleted.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No
        )
        if confirm == QMessageBox.StandardButton.Yes:
            reset_database()
            PEAK_CACHE.clear()
            INFO_CACHE.clear()
            self.checked_paths.clear()
            self.staged_apply_tags.clear()
            self.staged_remove_tags.clear()
            self.refresh_library_roots()
            self.refresh_tags()
            self.refresh_samples()
            self.lbl_status.setText("Database cleared.")

    # ---------------------------------------------------------------- tags --

    def refresh_tags(self):
        self.tag_list.clear()
        tags = tag_engine.get_all_tags()

        for tag_name, count, color in tags:
            row_widget = TagRowWidget(tag_name, count, color, self)
            row_widget.set_display(tag_name, count, tag_name in self.active_tag_filters)

            item = QListWidgetItem(self.tag_list)
            item.setSizeHint(QSize(0, 40))
            self.tag_list.addItem(item)
            self.tag_list.setItemWidget(item, row_widget)

        self.refresh_tag_stage_states()

    def refresh_tag_stage_states(self):
        states = tag_engine.get_tag_state_for_samples(list(self.checked_paths)) if self.checked_paths else {}

        for i in range(self.tag_list.count()):
            item = self.tag_list.item(i)
            widget = self.tag_list.itemWidget(item)
            if not widget:
                continue

            tag_name = widget.tag_name
            if tag_name in self.staged_remove_tags:
                widget.set_stage_state(Qt.CheckState.Unchecked)
            elif tag_name in self.staged_apply_tags:
                widget.set_stage_state(Qt.CheckState.Checked)
            else:
                db_state = states.get(tag_name)
                if db_state == 'all':
                    widget.set_stage_state(Qt.CheckState.Checked)
                elif db_state == 'some':
                    widget.set_stage_state(Qt.CheckState.PartiallyChecked)
                else:
                    widget.set_stage_state(Qt.CheckState.Unchecked)

            widget.stage_checkbox.setEnabled(bool(self.checked_paths))

    def add_custom_tag(self):
        tag_name = self.txt_new_tag.text().strip()
        if tag_name:
            tag_engine.create_tag(tag_name)
            self.txt_new_tag.clear()
            self.refresh_tags()

    # ------------------------------------------------------------- samples --

    def refresh_samples(self):
        """Re-queries the DB for the tag/folder-filtered set, then re-applies search."""
        self.base_samples = tag_engine.get_samples_by_tags(list(self.active_tag_filters))
        if self.active_folder_filter:
            self.base_samples = [
                s for s in self.base_samples if self._path_in_folder(s['file_path'], self.active_folder_filter)
            ]
        self.apply_search_filter()

    def filter_filename(self, text):
        """Search is applied to the already-fetched base_samples - no DB round trip."""
        self.apply_search_filter()

    def apply_search_filter(self):
        search_text = self.search_bar.text().strip().lower()
        if search_text:
            self.current_filtered_samples = [
                s for s in self.base_samples if search_text in s['file_name'].lower()
            ]
        else:
            self.current_filtered_samples = self.base_samples

        self.visible_count = 0
        self.sample_list.clear()
        self._load_more_samples()

    def on_sample_scroll(self, value):
        scrollbar = self.sample_list.verticalScrollBar()
        if scrollbar.maximum() > 0 and value >= scrollbar.maximum() - 4:
            self._load_more_samples()

    def _load_more_samples(self):
        start = self.visible_count
        end = min(start + PAGE_SIZE, len(self.current_filtered_samples))

        for s in self.current_filtered_samples[start:end]:
            card_widget = SampleCardWidget(s, show_waveform=self.show_waveforms, main_window=self)
            item = QListWidgetItem(self.sample_list)

            if self.show_waveforms:
                card_height = 165 if s.get('tags') else 140
            else:
                card_height = 100 if s.get('tags') else 80

            item.setSizeHint(QSize(0, card_height))
            item.setData(Qt.ItemDataRole.UserRole, s['file_path'])

            self.sample_list.addItem(item)
            self.sample_list.setItemWidget(item, card_widget)

        self.visible_count = end
        self._update_sample_status_label()

    def _update_sample_status_label(self):
        total = len(self.current_filtered_samples)
        status_text = f"Showing {self.visible_count} of {total} samples"
        if self.active_folder_filter:
            status_text += f" in [{os.path.basename(self.active_folder_filter)}]"
        self.lbl_status.setText(status_text)

    def play_selected_sample_item(self, item):
        if item:
            file_path = item.data(Qt.ItemDataRole.UserRole)
            if file_path and os.path.exists(file_path):
                play_audio(file_path)

    def play_selected_sample(self):
        items = self.sample_list.selectedItems()
        if items:
            file_path = items[0].data(Qt.ItemDataRole.UserRole)
            if file_path and os.path.exists(file_path):
                play_audio(file_path)


if __name__ == "__main__":
    init_db()
    app = QApplication(sys.argv)
    window = SoundSortaApp()
    window.show()
    sys.exit(app.exec())