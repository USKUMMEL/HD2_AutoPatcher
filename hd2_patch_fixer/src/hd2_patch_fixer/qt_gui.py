"""PySide6 desktop UI with the PM ParticleModder visual language."""

from __future__ import annotations

import os
import sys
import threading
from pathlib import Path

from PySide6.QtCore import QObject, Signal, Qt, QTimer
from PySide6.QtGui import QIcon
from PySide6.QtWidgets import (
    QApplication, QCheckBox, QFileDialog, QFrame, QGridLayout,
    QDialog, QHBoxLayout, QLabel, QLineEdit, QListWidget, QListWidgetItem,
    QMainWindow, QMessageBox, QPushButton,
    QScrollArea, QSizePolicy, QStackedWidget, QTextEdit, QVBoxLayout, QWidget,
)

from .archive import create_fixed_mod_archive, create_fixed_patch, normalize_archive_selection
from .archive_catalog import installed_archive_ids, load_archive_catalog
from .constants import TYPE_LABELS
from .settings import load_preferences, save_preferences


THEME = """
QMainWindow, QWidget#root { background: #171A1D; }
QWidget { color: #E4E9E5; font-family: 'Segoe UI'; font-size: 12px; background: transparent; }
QFrame#header, QFrame#sidebar, QFrame#card, QTextEdit { background: #202428; border: 1px solid #3B4449; }
QFrame#header { border-width: 0 0 1px 0; } QFrame#sidebar { border-width: 0 1px 0 0; }
QFrame#card { border-radius: 6px; } QLabel { background: transparent; } QLabel#eyebrow { color: #9DA8A2; font-size: 10px; font-weight: 600; }
QLabel#title { font-size: 23px; font-weight: 600; } QLabel#muted { color: #9DA8A2; }
QLineEdit, QTextEdit { background: #171A1D; border: 1px solid #3B4449; border-radius: 4px; padding: 8px; selection-background-color: #7EAC90; selection-color: #102018; }
QLineEdit:focus { border: 2px solid #86AFC0; }
QPushButton { background: #292E33; border: 1px solid #3B4449; border-radius: 5px; padding: 8px 12px; color: #E4E9E5; font-weight: 500; }
QPushButton:hover { background: #31383D; } QPushButton:checked { background: #31383D; border-color: #7EAC90; } QPushButton#accent { background: #7EAC90; color: #102018; border-color: #9AC2A7; font-weight: 600; }
QPushButton#accent:hover { background: #9AC2A7; } QPushButton:disabled { color: #66706A; background: #202428; }
QFrame#archiveToken { background: #292E33; border: 1px solid #526058; border-radius: 4px; }
QPushButton#tokenRemove { min-width: 18px; max-width: 18px; min-height: 18px; max-height: 18px; padding: 0; border: 0; background: transparent; color: #B8C2BC; font-size: 15px; }
QPushButton#tokenRemove:hover { color: #FFFFFF; background: #4A3434; }
QListWidget { background: #171A1D; border: 1px solid #3B4449; border-radius: 4px; padding: 3px; }
QListWidget::item { padding: 7px; border-radius: 3px; } QListWidget::item:hover, QListWidget::item:selected { background: #31383D; }
QCheckBox { spacing: 7px; color: #E4E9E5; background: transparent; } QCheckBox::indicator { width: 15px; height: 15px; border: 1px solid #526058; border-radius: 3px; background: #171A1D; }
QCheckBox::indicator:checked { background: #7EAC90; border-color: #9AC2A7; } QScrollArea { border: 0; } QScrollBar:vertical { width: 10px; background: #202428; } QScrollBar::handle:vertical { background: #3B4449; border-radius: 4px; min-height: 24px; }
"""


def app_icon() -> QIcon:
    """Return the development or PyInstaller-bundled application icon."""
    project_root = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parents[2]))
    return QIcon(str(project_root / "icon" / "icon.ico"))


class WorkerSignals(QObject):
    log = Signal(str)
    complete = Signal(dict)
    failed = Signal(str)


class ArchiveSourcePicker(QDialog):
    """Search the installed named-archive catalog and return one archive ID."""

    catalog_loaded = Signal(object, str)
    archive_selected = Signal(str, str)

    def __init__(self, game_data_folder, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Choose ID Swap Source Archive")
        self.setModal(True)
        self.resize(720, 500)
        self.selected_archive_id = None
        self._installed_ids = installed_archive_ids(game_data_folder)
        self._entries = []

        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(9)
        layout.addWidget(QLabel("FOUND ARCHIVES", objectName="eyebrow"))
        layout.addWidget(QLabel(
            "Search an installed armor or helmet archive by name, then click it to add its ID.",
            objectName="muted",
        ))
        self.search = QLineEdit()
        self.search.setPlaceholderText("Search archive name or ID…")
        self.search.textChanged.connect(self.refresh_entries)
        layout.addWidget(self.search)
        self.results = QListWidget()
        self.results.itemClicked.connect(self.choose_archive)
        layout.addWidget(self.results, 1)
        self.status = QLabel("Loading archive catalog…", objectName="muted")
        layout.addWidget(self.status)
        actions = QHBoxLayout()
        actions.addStretch()
        close_button = QPushButton("Cancel")
        close_button.clicked.connect(self.reject)
        actions.addWidget(close_button)
        layout.addLayout(actions)

        self.catalog_loaded.connect(self.set_catalog)
        threading.Thread(target=self.load_catalog, daemon=True).start()

    def load_catalog(self):
        catalog = load_archive_catalog()
        entries = [
            (archive_id, display_name)
            for archive_id, display_name in catalog.items()
            if archive_id in self._installed_ids
        ]
        entries.sort(key=lambda item: item[1].casefold())
        if entries:
            message = f"{len(entries)} named archives installed. Click one to add it."
        elif self._installed_ids:
            message = "No named archives were available. Check your internet connection and try again."
        else:
            message = "No game archives were found in the selected Data folder."
        self.catalog_loaded.emit(entries, message)

    def set_catalog(self, entries, message):
        self._entries = entries
        self.status.setText(message)
        self.refresh_entries()

    def refresh_entries(self):
        query = self.search.text().strip().casefold()
        self.results.clear()
        for archive_id, display_name in self._entries:
            if query and query not in display_name.casefold() and query not in archive_id:
                continue
            item = QListWidgetItem(f"{display_name}\n{archive_id}")
            item.setData(Qt.UserRole, archive_id)
            item.setData(Qt.UserRole + 1, display_name)
            self.results.addItem(item)
        if self.results.count() == 0 and self._entries:
            self.status.setText("No installed archive matches this search.")

    def choose_archive(self, item):
        self.selected_archive_id = item.data(Qt.UserRole)
        display_name = item.data(Qt.UserRole + 1)
        self.archive_selected.emit(self.selected_archive_id, display_name)
        self.status.setText(f"Added: {display_name}. Click another archive or close this window.")


class PatchFixerWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("HD2 Patch Fixer")
        self.setWindowIcon(app_icon())
        self.resize(1180, 780)
        self.setMinimumSize(920, 640)
        self.setStyleSheet(THEME)
        self.signals = WorkerSignals()
        self.signals.log.connect(self.append_log)
        self.signals.complete.connect(self.finish)
        self.signals.failed.connect(self.fail)
        self.type_checks = {}
        self.idswap_source_archive_ids = []
        self.idswap_source_archive_labels = {}
        self.preferences = load_preferences()
        self.build_ui()
        self.restore_preferences()

    def build_ui(self):
        root = QWidget(objectName="root"); self.setCentralWidget(root)
        layout = QVBoxLayout(root); layout.setContentsMargins(0, 0, 0, 0); layout.setSpacing(0)
        header = QFrame(objectName="header"); header_row = QHBoxLayout(header); header_row.setContentsMargins(18, 10, 18, 10)
        brand = QLabel("HD2 PATCH FIXER"); brand.setStyleSheet("font-size: 16px; font-weight: 600;")
        self.status = QLabel("READY"); self.status.setStyleSheet("color: #9DA8A2; font-size: 10px; font-weight: 600;")
        header_row.addWidget(brand); header_row.addWidget(QLabel("  |  Version 0.1.0", objectName="muted")); header_row.addStretch(); header_row.addWidget(self.status); layout.addWidget(header)
        body = QHBoxLayout(); body.setContentsMargins(0, 0, 0, 0); body.setSpacing(0); layout.addLayout(body)
        sidebar = QFrame(objectName="sidebar"); sidebar.setFixedWidth(212); side = QVBoxLayout(sidebar); side.setContentsMargins(14, 18, 14, 14); side.setSpacing(6)
        nav_label = QLabel("WORKSPACE", objectName="eyebrow"); side.addWidget(nav_label)
        self.single_nav = self.nav_button("Single Patch", True)
        self.archive_nav = self.nav_button("Compressed Mods")
        self.single_nav.clicked.connect(lambda: self.set_mode(0))
        self.archive_nav.clicked.connect(lambda: self.set_mode(1))
        side.addWidget(self.single_nav); side.addWidget(self.archive_nav); side.addSpacing(18)
        side.addWidget(QLabel("MIGRATION", objectName="eyebrow"))
        thanks = QLabel(
            "Thanks to Eve and Box, as well as everyone who contributed to the Helldivers 2 modding community.",
            objectName="muted",
        )
        thanks.setWordWrap(True)
        side.addWidget(thanks)
        side.addStretch(); side.addWidget(QLabel("Select the game Data folder first.", objectName="muted")); body.addWidget(sidebar)
        main_scroll = QScrollArea()
        main_scroll.setWidgetResizable(True)
        main = QWidget()
        main_scroll.setWidget(main)
        main_layout = QVBoxLayout(main)
        main_layout.setContentsMargins(22, 20, 22, 16)
        main_layout.setSpacing(12)
        body.addWidget(main_scroll, 1)
        main_layout.addWidget(QLabel("PATCH MIGRATION", objectName="eyebrow")); main_layout.addWidget(QLabel("Rebuild compatible mod patches", objectName="title")); main_layout.addWidget(QLabel("Preserve mod content while updating package structure for the installed Helldivers 2 build.", objectName="muted"))
        self.game_path = self.path_field(main_layout, "GAME DATA FOLDER", self.browse_game)
        self.idswap_source_archive = self.idswap_source_field(
            main_layout,
            "ID SWAP SOURCE ARCHIVE(S) — OPTIONAL",
            "Enter an archive ID, then press Enter",
            "Only using for id swap Armor/Helmet mods, if your mods patch doesnt have any id swap Armor/Helmet leave it blank. If so, enter the source archive, Example: You modded armor/helmet A then use ID swap it to armor/helmet B, so enter armor/helmet A source archive.",
        )
        self.stack = QStackedWidget()
        self.stack.addWidget(self.single_page())
        self.stack.addWidget(self.archive_page())
        main_layout.addWidget(self.stack)
        main_layout.addWidget(self.options_card())
        lower = QHBoxLayout(); lower.setSpacing(12); lower.addWidget(self.log_card(), 1); actions = QVBoxLayout(); self.run_button = QPushButton("Run Patcher", objectName="accent"); self.run_button.clicked.connect(self.run_fix); actions.addWidget(self.run_button); actions.addWidget(self.parallel_patches_card()); actions.addStretch(); lower.addLayout(actions); main_layout.addLayout(lower, 1)
        self.set_mode(0)
        # The first layout pass can report an expanded page hint before Qt has
        # calculated the Browse rows. Refresh it once the window is shown.
        QTimer.singleShot(0, self.refresh_input_stack_height)

    def nav_button(self, text, checked=False):
        button = QPushButton(text); button.setCheckable(True); button.setChecked(checked); return button

    def restore_preferences(self):
        game_data_folder = self.preferences.get("gameDataFolder")
        if isinstance(game_data_folder, str) and Path(game_data_folder).is_dir():
            self.game_path.setText(game_data_folder)

    def save_game_data_folder(self, game_data_folder):
        self.preferences["gameDataFolder"] = game_data_folder
        save_preferences(self.preferences)

    def card(self):
        card = QFrame(objectName="card"); card.setContentsMargins(0, 0, 0, 0); return card

    def path_field(self, parent_layout, label, browse):
        card = self.card()
        # Browse cards always follow their content height. This prevents the
        # two input/output cards expanding vertically on the first show.
        card.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        layout = QVBoxLayout(card); layout.setContentsMargins(14, 12, 14, 14); layout.setSpacing(7); layout.addWidget(QLabel(label, objectName="eyebrow")); row = QHBoxLayout(); field = QLineEdit(); field.setPlaceholderText("Choose a folder or file…"); browse_button = QPushButton("Browse"); browse_button.clicked.connect(browse); row.addWidget(field, 1); row.addWidget(browse_button); layout.addLayout(row); parent_layout.addWidget(card); return field

    def text_field(self, parent_layout, label, placeholder, hint=None):
        card = self.card()
        layout = QVBoxLayout(card)
        layout.setContentsMargins(14, 12, 14, 14)
        layout.setSpacing(7)
        layout.addWidget(QLabel(label, objectName="eyebrow"))
        field = QLineEdit()
        field.setPlaceholderText(placeholder)
        layout.addWidget(field)
        if hint:
            layout.addWidget(QLabel(hint, objectName="muted"))
        parent_layout.addWidget(card)
        return field

    def idswap_source_field(self, parent_layout, label, placeholder, hint=None):
        """Build a token editor for optional ID-swap source archives."""
        card = self.card()
        layout = QVBoxLayout(card)
        layout.setContentsMargins(14, 12, 14, 14)
        layout.setSpacing(7)
        layout.addWidget(QLabel(label, objectName="eyebrow"))
        field = QLineEdit()
        field.setPlaceholderText(placeholder)
        field.returnPressed.connect(self.add_idswap_source_tokens)
        input_row = QHBoxLayout()
        input_row.setSpacing(6)
        input_row.addWidget(field, 1)
        picker_button = QPushButton("+")
        picker_button.setFixedWidth(34)
        picker_button.setToolTip("Find an installed source archive by name")
        picker_button.clicked.connect(self.open_idswap_source_picker)
        input_row.addWidget(picker_button)
        layout.addLayout(input_row)

        self.idswap_token_host = QWidget()
        self.idswap_token_layout = QHBoxLayout(self.idswap_token_host)
        self.idswap_token_layout.setContentsMargins(0, 0, 0, 0)
        self.idswap_token_layout.setSpacing(6)
        self.idswap_token_layout.addStretch()
        self.idswap_token_host.setVisible(False)
        layout.addWidget(self.idswap_token_host)
        if hint:
            hint_label = QLabel(hint, objectName="muted")
            hint_label.setWordWrap(True)
            layout.addWidget(hint_label)
        parent_layout.addWidget(card)
        return field

    @staticmethod
    def normalize_idswap_source_token(value):
        value = value.strip()
        hex_value = value[2:] if value.lower().startswith("0x") else value
        if hex_value and all(char in "0123456789abcdefABCDEF" for char in hex_value):
            return hex_value.lower()
        return value

    def add_idswap_source_tokens(self):
        raw_value = self.idswap_source_archive.text().strip()
        if not raw_value:
            return
        for raw_token in raw_value.split(","):
            self.add_idswap_source_token(raw_token)
        self.idswap_source_archive.clear()
        self.refresh_idswap_source_tokens()

    def add_idswap_source_token(self, value, display_name=None):
        token = self.normalize_idswap_source_token(value)
        if token and token not in self.idswap_source_archive_ids:
            self.idswap_source_archive_ids.append(token)
        if token and display_name:
            self.idswap_source_archive_labels[token] = display_name

    def open_idswap_source_picker(self):
        game_data_folder = self.game_path.text().strip()
        if not Path(game_data_folder).is_dir():
            QMessageBox.warning(
                self,
                "Game Data folder required",
                "Choose a valid Helldivers 2 Data folder before searching found archives.",
            )
            return
        dialog = ArchiveSourcePicker(game_data_folder, self)
        dialog.archive_selected.connect(self.add_found_idswap_source_archive)
        dialog.exec()

    def add_found_idswap_source_archive(self, archive_id, display_name):
        self.add_idswap_source_token(archive_id, display_name)
        self.refresh_idswap_source_tokens()

    def remove_idswap_source_token(self, token):
        self.idswap_source_archive_ids.remove(token)
        self.idswap_source_archive_labels.pop(token, None)
        self.refresh_idswap_source_tokens()

    def refresh_idswap_source_tokens(self):
        while self.idswap_token_layout.count() > 1:
            item = self.idswap_token_layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()
        for token in self.idswap_source_archive_ids:
            chip = QFrame(objectName="archiveToken")
            chip_layout = QHBoxLayout(chip)
            chip_layout.setContentsMargins(8, 3, 4, 3)
            chip_layout.setSpacing(5)
            display_name = self.idswap_source_archive_labels.get(token, token)
            label = QLabel(display_name)
            label.setToolTip(f"{display_name}\nArchive ID: {token}")
            chip_layout.addWidget(label)
            remove = QPushButton("×", objectName="tokenRemove")
            remove.setToolTip(f"Remove {display_name}")
            remove.clicked.connect(
                lambda _checked=False, value=token: self.remove_idswap_source_token(value)
            )
            chip_layout.addWidget(remove)
            self.idswap_token_layout.insertWidget(self.idswap_token_layout.count() - 1, chip)
        self.idswap_token_host.setVisible(bool(self.idswap_source_archive_ids))

    def idswap_source_archive_value(self):
        # Include text still being typed when Run is pressed, so a missing
        # final Enter never silently discards an archive ID.
        self.add_idswap_source_tokens()
        return ",".join(self.idswap_source_archive_ids) or None

    def single_page(self):
        page = QWidget(); page.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed); layout = QVBoxLayout(page); layout.setContentsMargins(0, 0, 0, 0); layout.setSpacing(10); self.patch_path = self.path_field(layout, "BROKEN PATCH FILE", self.browse_patch); self.export_path = self.path_field(layout, "EXPORT FOLDER", self.browse_export); layout.setAlignment(Qt.AlignTop); return page

    def archive_page(self):
        page = QWidget(); page.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed); layout = QVBoxLayout(page); layout.setContentsMargins(0, 0, 0, 0); layout.setSpacing(10); self.mod_path = self.path_field(layout, "COMPRESSED MOD FILE", self.browse_mod); self.zip_path = self.path_field(layout, "EXPORT ZIP FILE", self.browse_zip); layout.setAlignment(Qt.AlignTop); return page

    def options_card(self):
        card = self.card()
        layout = QVBoxLayout(card)
        layout.setContentsMargins(14, 12, 14, 14)
        layout.setSpacing(8)
        layout.addWidget(QLabel("DATA TYPES TO KEEP", objectName="eyebrow"))
        tools = QHBoxLayout()
        select_all = QPushButton("Select all")
        clear_all = QPushButton("Clear all")
        select_all.clicked.connect(lambda: self.set_all_types(True))
        clear_all.clicked.connect(lambda: self.set_all_types(False))
        self.keep_unknown = QCheckBox("Keep unknown types")
        self.keep_unknown.setChecked(True)
        tools.addWidget(select_all)
        tools.addWidget(clear_all)
        tools.addStretch()
        tools.addWidget(self.keep_unknown)
        layout.addLayout(tools)
        grid = QGridLayout()
        grid.setHorizontalSpacing(20)
        grid.setVerticalSpacing(5)
        for index, (type_id, label) in enumerate(TYPE_LABELS):
            check = QCheckBox(label)
            check.setChecked(True)
            self.type_checks[type_id] = check
            grid.addWidget(check, index % 3, index // 3)
        layout.addLayout(grid)
        self.raw_fallback = QCheckBox("Preserve unknown or unsupported payloads when rebuilding")
        self.raw_fallback.setChecked(True)
        self.weapon_swap_mode = QCheckBox("Safe automatic Weapon ID Swap migration")
        self.weapon_swap_mode.setChecked(True)
        layout.addWidget(self.raw_fallback)
        layout.addWidget(self.weapon_swap_mode)
        layout.addWidget(QLabel(
            "Option for weapon id swap patching, Only verified rigged weapon swaps use this mode",
            objectName="muted",
        ))
        return card

    def log_card(self):
        card = self.card(); layout = QVBoxLayout(card); layout.setContentsMargins(14, 12, 14, 14); layout.addWidget(QLabel("MIGRATION LOG", objectName="eyebrow")); self.log = QTextEdit(); self.log.setReadOnly(True); self.log.setPlainText("Ready. Choose a game folder and mod patch."); layout.addWidget(self.log); return card

    def parallel_patches_card(self):
        """Small, explicit concurrency control for multi-patch mod archives."""
        card = self.card()
        layout = QVBoxLayout(card)
        layout.setContentsMargins(12, 10, 12, 10)
        layout.setSpacing(6)
        layout.addWidget(QLabel("PARALLEL PATCHES", objectName="eyebrow"))
        row = QHBoxLayout()
        self.parallel_minus = QPushButton("−")
        self.parallel_minus.setFixedWidth(34)
        self.parallel_value = QLabel("1")
        self.parallel_value.setAlignment(Qt.AlignCenter)
        self.parallel_value.setFixedWidth(32)
        self.parallel_plus = QPushButton("+")
        self.parallel_plus.setFixedWidth(34)
        self.parallel_limit = max(1, min(8, os.cpu_count() or 4))
        self.parallel_patch_count = 1
        self.parallel_minus.clicked.connect(lambda: self.change_parallel_patch_count(-1))
        self.parallel_plus.clicked.connect(lambda: self.change_parallel_patch_count(1))
        row.addWidget(self.parallel_minus)
        row.addWidget(self.parallel_value)
        row.addWidget(self.parallel_plus)
        row.addStretch()
        layout.addLayout(row)
        self.parallel_hint = QLabel(
            "How much patch it will fix at the same time, more number fix faster but also use more system resource but not much.",
            objectName="muted",
        )
        self.parallel_hint.setWordWrap(True)
        layout.addWidget(self.parallel_hint)
        return card

    def set_mode(self, mode):
        self.stack.setCurrentIndex(mode)
        self.refresh_input_stack_height()
        self.single_nav.setChecked(mode == 0)
        self.archive_nav.setChecked(mode == 1)
        self.run_button.setEnabled(True)
        self.status.setText("READY")
        if hasattr(self, "parallel_minus"):
            parallel_available = mode == 1
            self.parallel_minus.setEnabled(parallel_available and self.parallel_patch_count > 1)
            self.parallel_plus.setEnabled(parallel_available and self.parallel_patch_count < self.parallel_limit)
            self.parallel_value.setEnabled(parallel_available)
            self.parallel_hint.setText(
                "How much patch it will fix at the same time, more number fix faster but also use more system resource but not much."
                if parallel_available
                else "Single Patch processes one patch file at a time."
            )

    def refresh_input_stack_height(self):
        current_page = self.stack.currentWidget()
        if current_page is None:
            return
        current_page.layout().activate()
        self.stack.setFixedHeight(current_page.layout().sizeHint().height())

    def change_parallel_patch_count(self, delta):
        self.parallel_patch_count = max(
            1,
            min(self.parallel_limit, self.parallel_patch_count + delta),
        )
        self.parallel_value.setText(str(self.parallel_patch_count))
        self.set_mode(self.stack.currentIndex())

    def set_all_types(self, checked):
        for check in self.type_checks.values(): check.setChecked(checked)

    def browse_game(self):
        value = QFileDialog.getExistingDirectory(self, "Select Helldivers 2 data folder", self.game_path.text());
        if value:
            self.game_path.setText(value)
            self.save_game_data_folder(value)

    def browse_patch(self):
        value, _ = QFileDialog.getOpenFileName(self, "Select patch", self.patch_path.text(), "Patch files (*)")
        if value: self.patch_path.setText(normalize_archive_selection(value))

    def browse_export(self):
        value = QFileDialog.getExistingDirectory(self, "Select export folder", self.export_path.text());
        if value: self.export_path.setText(value)

    def browse_mod(self):
        value, _ = QFileDialog.getOpenFileName(self, "Select compressed mod", self.mod_path.text(), "Mod archives (*.zip *.7z *.rar)")
        if value:
            self.mod_path.setText(value)
            if not self.zip_path.text(): self.zip_path.setText(str(Path(value).with_name(f"{Path(value).stem}_fixed.zip")))

    def browse_zip(self):
        value, _ = QFileDialog.getSaveFileName(self, "Select output zip", self.zip_path.text() or "fixed_mod.zip", "Zip archive (*.zip)")
        if value: self.zip_path.setText(value if value.lower().endswith(".zip") else f"{value}.zip")

    def append_log(self, message):
        self.log.append(message); self.log.verticalScrollBar().setValue(self.log.verticalScrollBar().maximum())

    def run_fix(self):
        game = self.game_path.text().strip(); keep = {type_id for type_id, check in self.type_checks.items() if check.isChecked()}
        if not Path(game).is_dir() or (not keep and not self.keep_unknown.isChecked()):
            return self.fail("Choose a valid game folder and at least one type (or keep unknown types).")
        self.save_game_data_folder(game)
        archive_mode = self.stack.currentIndex() == 1
        source = self.mod_path.text().strip() if archive_mode else self.patch_path.text().strip(); target = self.zip_path.text().strip() if archive_mode else self.export_path.text().strip()
        if not Path(source).is_file() or (archive_mode and not target) or (not archive_mode and not Path(target).is_dir()):
            return self.fail("Choose valid input and output paths.")
        keep_unknown = self.keep_unknown.isChecked()
        raw_fallback = self.raw_fallback.isChecked()
        # Audio migration is mandatory: broken Wwise patches require the
        # current-game Bank/Stream merge and should never silently raw-copy.
        migrate_audio = True
        weapon_swap_mode = self.weapon_swap_mode.isChecked()
        idswap_source_archive = self.idswap_source_archive_value()
        self.run_button.setEnabled(False); self.status.setText("MIGRATING"); self.append_log("Starting patch migration…")
        def worker():
            try:
                args = dict(game_data_folder=game, keep_type_ids=keep, keep_unknown_types=keep_unknown, raw_fallback_for_unsupported=raw_fallback, migrate_audio=migrate_audio, weapon_swap_mode=weapon_swap_mode, idswap_source_archive=idswap_source_archive, log=lambda text: self.signals.log.emit(text))
                if archive_mode:
                    args["max_workers"] = self.parallel_patch_count
                result = create_fixed_mod_archive(input_archive_path=source, output_zip_path=target, **args) if archive_mode else create_fixed_patch(broken_patch_path=source, export_dir=target, **args)
                result["archive_mode"] = archive_mode; self.signals.complete.emit(result)
            except Exception as exc: self.signals.failed.emit(str(exc))
        threading.Thread(target=worker, daemon=True).start()

    def finish(self, result):
        self.run_button.setEnabled(True); self.status.setText("COMPLETED"); message = f"Created:\n{result['output_path']}"; self.append_log("Migration completed successfully."); QMessageBox.information(self, "Migration completed", message)

    def fail(self, message):
        self.run_button.setEnabled(True); self.status.setText("FAILED"); self.append_log(f"ERROR: {message}"); QMessageBox.critical(self, "Migration failed", message)


def run():
    app = QApplication(sys.argv); app.setStyle("Fusion"); app.setWindowIcon(app_icon()); window = PatchFixerWindow(); window.showMaximized(); return app.exec()
