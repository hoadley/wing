#!/usr/bin/env python3
"""Qt editor for arranging WING strips and allocating 24 local inputs.

On startup the app scans the network for a WING console and connects to
it. Editing, loading and saving also work offline. Stereo sources consume
odd/even pairs; moving a strip reallocates its source inputs on Apply.
Configurations are compatible with channels.py.
Names have a 12-character limit. The 48V toggle defaults off and applies
to both inputs of a stereo source when the setup is applied.
"""

import asyncio
import sys
import threading
from pathlib import Path

from PySide6.QtCore import QObject, QSize, Qt, Signal
from PySide6.QtGui import QColor, QPixmap, QIcon
from PySide6.QtWidgets import (
    QApplication, QCheckBox, QComboBox, QFileDialog, QGridLayout, QHBoxLayout, QLabel,
    QLineEdit, QMainWindow, QMessageBox, QPushButton, QScrollArea,
    QVBoxLayout, QWidget,
)

from channels import (
    ChannelConfig, DEFAULT_CONFIG_FILE, MAX_NAME_LENGTH, NUM_CHANNELS, NUM_INPUTS,
    allocate_inputs, apply_channel_aesthetics, load_config,
    read_channel_aesthetics, save_config, validate_capacity,
)
from wing import WingConsole
from wing_tables import WING_COLORS, WING_ICONS

ICONS_DIR = Path(__file__).parent / "icons"

# Approximate RGB swatches for the WING color palette (for the combo boxes)
COLOR_SWATCHES = {
    "Gray Blue": (122, 139, 166),
    "Medium Blue": (70, 110, 220),
    "Dark Blue": (40, 60, 140),
    "Turquoise": (64, 224, 208),
    "Green": (80, 200, 80),
    "Olive Green": (128, 150, 60),
    "Yellow": (230, 220, 70),
    "Brown": (150, 100, 60),
    "Red": (220, 60, 60),
    "Coral": (240, 128, 128),
    "Magenta": (220, 70, 200),
    "Purple": (150, 70, 220),
    "Orange": (240, 150, 50),
    "Light Blue": (120, 180, 240),
    "Salmon": (250, 160, 140),
    "Teal": (0, 150, 150),
    "Dark Gray": (90, 90, 90),
    "Light Gray": (180, 180, 180),
}


def build_icon_entries() -> list[tuple[str, QIcon]]:
    """Pair each WING icon name with its image from the icons/ folder."""
    entries = []
    for name, icon_id in WING_ICONS.items():
        path = ICONS_DIR / f"{icon_id}.png"
        entries.append((name, QIcon(str(path)) if path.exists() else QIcon()))
    return entries


class WingWorker(QObject):
    """Runs an asyncio event loop in a background thread for OSC I/O."""

    connected = Signal(str)
    connect_failed = Signal(str)
    applied = Signal(list)
    apply_failed = Signal(str)
    read_done = Signal(object)  # dict with int keys; dict signals need str keys
    read_failed = Signal(str)

    def __init__(self):
        super().__init__()
        self.wing: WingConsole | None = None
        self.loop = asyncio.new_event_loop()
        threading.Thread(target=self.loop.run_forever, daemon=True).start()

    def scan_and_connect(self):
        asyncio.run_coroutine_threadsafe(self._connect(), self.loop)

    async def _connect(self):
        try:
            if self.wing is not None:
                self.wing.close()
                self.wing = None
            self.wing = await WingConsole.find()
            self.connected.emit(str(self.wing.info))
        except Exception as exc:
            self.connect_failed.emit(str(exc))

    def apply(self, config: dict):
        asyncio.run_coroutine_threadsafe(self._apply(config), self.loop)

    async def _apply(self, config: dict):
        try:
            results = await apply_channel_aesthetics(self.wing, config)
            self.applied.emit(results)
        except Exception as exc:
            self.apply_failed.emit(str(exc))

    def read(self):
        asyncio.run_coroutine_threadsafe(self._read(), self.loop)

    async def _read(self):
        try:
            config = await read_channel_aesthetics(
                self.wing, range(1, NUM_CHANNELS + 1))
            self.read_done.emit(config)
        except Exception as exc:
            self.read_failed.emit(str(exc))


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("WING Channel Editor")
        self.rows: dict[
            int, tuple[QLineEdit, QComboBox, QComboBox, QComboBox, QCheckBox]
        ] = {}
        self.input_labels: dict[int, QLabel] = {}
        self.conn_info = ""
        self.busy = False

        self.worker = WingWorker()
        self.worker.connected.connect(self.on_connected)
        self.worker.connect_failed.connect(self.on_connect_failed)
        self.worker.applied.connect(self.on_applied)
        self.worker.apply_failed.connect(self.on_apply_failed)
        self.worker.read_done.connect(self.on_read_done)
        self.worker.read_failed.connect(self.on_read_failed)

        central = QWidget()
        layout = QVBoxLayout(central)

        # --- connection bar ---
        conn_bar = QHBoxLayout()
        self.status_label = QLabel("Scanning for WING console ...")
        self.rescan_btn = QPushButton("Rescan")
        self.rescan_btn.clicked.connect(self.start_scan)
        conn_bar.addWidget(self.status_label, stretch=1)
        conn_bar.addWidget(self.rescan_btn)
        self.input_count = QLabel()
        self.input_count.setToolTip(
            "Remaining local input slots, including odd/even alignment gaps")
        conn_bar.addWidget(self.input_count)
        layout.addLayout(conn_bar)

        # --- channel grid ---
        self.grid_widget = QWidget()
        grid = QGridLayout(self.grid_widget)
        grid.setHorizontalSpacing(8)
        for col, title in enumerate(
                ("Ch", "Name", "Color", "Icon", "Source", "Local input", "48V", "Move")):
            header = QLabel(f"<b>{title}</b>")
            grid.addWidget(header, 0, col)

        icon_entries = build_icon_entries()
        for ch in range(1, NUM_CHANNELS + 1):
            name_edit = QLineEdit()
            name_edit.setMaxLength(MAX_NAME_LENGTH)
            name_edit.setToolTip("Channel name (maximum 12 characters)")
            color_combo = QComboBox()
            for cname in WING_COLORS:
                pix = QPixmap(14, 14)
                pix.fill(QColor(*COLOR_SWATCHES[cname]))
                color_combo.addItem(QIcon(pix), cname)
            icon_combo = QComboBox()
            icon_combo.setIconSize(QSize(22, 22))
            for iname, qicon in icon_entries:
                icon_combo.addItem(qicon, iname)
            mode_combo = QComboBox()
            mode_combo.addItems(["Mono", "Stereo"])
            input_label = QLabel("-")
            phantom_toggle = QCheckBox()
            phantom_toggle.setToolTip(
                "Enable 48V on Apply (both local inputs for stereo)")
            move = QWidget()
            move_layout = QHBoxLayout(move)
            move_layout.setContentsMargins(0, 0, 0, 0)
            for offset, text in ((-1, "Up"), (1, "Down")):
                button = QPushButton(text)
                button.setEnabled(1 <= ch + offset <= NUM_CHANNELS)
                button.clicked.connect(
                    lambda checked=False, row=ch, delta=offset:
                    self.move_channel(row, delta))
                move_layout.addWidget(button)
            grid.addWidget(QLabel(str(ch)), ch, 0)
            grid.addWidget(name_edit, ch, 1)
            grid.addWidget(color_combo, ch, 2)
            grid.addWidget(icon_combo, ch, 3)
            grid.addWidget(mode_combo, ch, 4)
            grid.addWidget(input_label, ch, 5)
            grid.addWidget(phantom_toggle, ch, 6)
            grid.addWidget(move, ch, 7)
            self.rows[ch] = (
                name_edit, color_combo, icon_combo, mode_combo, phantom_toggle)
            self.input_labels[ch] = input_label
            name_edit.textChanged.connect(self.update_inputs)
            mode_combo.currentIndexChanged.connect(self.update_inputs)

        scroll = QScrollArea()
        scroll.setWidget(self.grid_widget)
        scroll.setWidgetResizable(True)
        layout.addWidget(scroll, stretch=1)

        # --- action buttons ---
        buttons = QHBoxLayout()
        self.load_btn = QPushButton("Load…")
        self.load_btn.clicked.connect(self.load_file)
        save_btn = QPushButton("Save…")
        save_btn.clicked.connect(self.save_file)
        self.read_btn = QPushButton("Read from WING")
        self.read_btn.setEnabled(False)
        self.read_btn.clicked.connect(self.read_from_wing)
        self.apply_btn = QPushButton("Apply to WING")
        self.apply_btn.setToolTip(
            "Replace local input routing; unnamed strips are disconnected. "
            "Names on unused local inputs 1-24 are cleared. "
            "Processing, faders and sends are not moved.")
        self.apply_btn.setEnabled(False)
        self.apply_btn.clicked.connect(self.apply_to_wing)
        buttons.addWidget(self.load_btn)
        buttons.addWidget(save_btn)
        buttons.addStretch(1)
        buttons.addWidget(self.read_btn)
        buttons.addWidget(self.apply_btn)
        layout.addLayout(buttons)

        self.setCentralWidget(central)
        self.resize(1000, 760)

        self.update_inputs()
        self.start_scan()

    # ------------------------------------------------------------------
    # Connection handling
    # ------------------------------------------------------------------

    def start_scan(self):
        self.conn_info = ""
        self.status_label.setText("Scanning for WING console ...")
        self.rescan_btn.setEnabled(False)
        self.read_btn.setEnabled(False)
        self.apply_btn.setEnabled(False)
        self.worker.scan_and_connect()

    def on_connected(self, info: str):
        self.conn_info = info
        self.rescan_btn.setEnabled(True)
        # Pull the console's current channel values into the grid
        self.status_label.setText(f"Connected: {info} — reading channels ...")
        self.read_from_wing()

    def on_connect_failed(self, error: str):
        self.status_label.setText(f"Not connected: {error}")
        self.rescan_btn.setEnabled(True)

    def read_from_wing(self):
        self.set_busy(True)
        self.status_label.setText(f"Connected: {self.conn_info} — "
                                  "reading channels ...")
        self.worker.read()

    def on_read_done(self, config: dict):
        try:
            self.set_config(config)
        except ValueError as exc:
            self.on_read_failed(str(exc))
            return
        self.set_busy(False)
        self.status_label.setText(f"Connected: {self.conn_info} — "
                                  f"read {len(config)} channels from console")

    def on_read_failed(self, error: str):
        self.set_busy(False)
        self.status_label.setText(f"Connected: {self.conn_info} — "
                                  f"read failed: {error}")

    # ------------------------------------------------------------------
    # Config handling
    # ------------------------------------------------------------------

    def get_config(self) -> dict[int, ChannelConfig]:
        """Collect rows with a non-empty name into a config dict."""
        config = {}
        for ch, (name_edit, color_combo, icon_combo, mode_combo,
                 phantom_toggle) in self.rows.items():
            name = name_edit.text().strip()
            if name:
                config[ch] = ChannelConfig(
                    name, color_combo.currentText(), icon_combo.currentText(),
                    mode_combo.currentText().lower(), phantom_toggle.isChecked())
        return config

    def set_config(self, config: dict):
        validate_capacity(config)
        for ch in self.rows:
            if ch in config:
                self.set_row(ch, config[ch])
            else:
                self.set_row(ch, ChannelConfig(""))
        self.update_inputs()

    def set_row(self, ch: int, entry: ChannelConfig):
        name, color, icon, mode, phantom = self.rows[ch]
        for widget in self.rows[ch]:
            widget.blockSignals(True)
        name.setText(entry.name)
        color.setCurrentText(entry.color)
        icon.setCurrentText(entry.icon)
        mode.setCurrentText(entry.mode.title())
        phantom.setChecked(entry.phantom)
        for widget in self.rows[ch]:
            widget.blockSignals(False)

    def move_channel(self, ch: int, offset: int):
        target = ch + offset
        if not 1 <= target <= NUM_CHANNELS:
            raise ValueError("Cannot move beyond the first or last channel")
        entries = []
        for row in (ch, target):
            name, color, icon, mode, phantom = self.rows[row]
            entries.append(ChannelConfig(
                name.text(), color.currentText(), icon.currentText(),
                mode.currentText().lower(), phantom.isChecked()))
        self.set_row(ch, entries[1])
        self.set_row(target, entries[0])
        self.rows[target][0].setFocus()
        self.update_inputs()

    def update_inputs(self):
        sources = allocate_inputs(self.get_config())
        used = max((inputs[-1] for inputs in sources.values()), default=0)
        remaining = max(0, NUM_INPUTS - used)
        self.input_count.setText(f"Available inputs: {remaining}/{NUM_INPUTS}")
        overflow = used > NUM_INPUTS
        if overflow:
            self.input_count.setText(
                f"Available inputs: 0/{NUM_INPUTS} (over by {used - NUM_INPUTS})")
        self.input_count.setStyleSheet("color: red;" if overflow else "")
        for ch, label in self.input_labels.items():
            inputs = sources.get(ch, ())
            label.setText(" + ".join(map(str, inputs)) if inputs else "-")
            label.setStyleSheet(
                "color: red;" if inputs and inputs[-1] > NUM_INPUTS else "")
        self.apply_btn.setEnabled(
            bool(self.conn_info) and not self.busy and not overflow)

    def set_busy(self, busy: bool):
        self.busy = busy
        self.grid_widget.setEnabled(not busy)
        self.load_btn.setEnabled(not busy)
        self.rescan_btn.setEnabled(not busy)
        self.read_btn.setEnabled(bool(self.conn_info) and not busy)
        self.update_inputs()

    def load_file(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "Load channel config", "", "JSON files (*.json)")
        if not path:
            return
        try:
            self.set_config(load_config(path))
        except Exception as exc:
            QMessageBox.warning(self, "Load failed", str(exc))

    def save_file(self):
        path, _ = QFileDialog.getSaveFileName(
            self, "Save channel config", DEFAULT_CONFIG_FILE,
            "JSON files (*.json)")
        if not path:
            return
        try:
            save_config(self.get_config(), path)
        except Exception as exc:
            QMessageBox.warning(self, "Save failed", str(exc))

    # ------------------------------------------------------------------
    # Apply
    # ------------------------------------------------------------------

    def apply_to_wing(self):
        config = self.get_config()
        try:
            validate_capacity(config)
        except ValueError as exc:
            QMessageBox.warning(self, "Invalid setup", str(exc))
            return
        answer = QMessageBox.question(
            self, "Replace input setup?",
            "Reassign local inputs in the displayed order and disconnect "
            "unnamed strips? Names on all unused local inputs 1-24 will be "
            "cleared, including when all rows are empty.\n\n"
            "Names, colors and icons will be written to "
            "the new sources. 48V will be set on each allocated input as "
            "selected (both inputs for stereo). Check connected equipment "
            "before enabling 48V. Processing, faders and sends will not move.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No)
        if answer != QMessageBox.StandardButton.Yes:
            return
        self.set_busy(True)
        self.status_label.setText(f"Applying {len(config)} channels ...")
        self.worker.apply(config)

    def on_applied(self, results: list):
        self.set_busy(False)
        ok = sum(1 for _, status in results if status.endswith("ok"))
        self.status_label.setText(
            f"Applied: {ok}/{len(results)} channels ok")
        problems = [f"Ch {ch}: {status}" for ch, status in results
                    if not status.endswith("ok")]
        if problems:
            QMessageBox.warning(self, "Apply finished with issues",
                                "\n".join(problems))

    def on_apply_failed(self, error: str):
        self.set_busy(False)
        self.status_label.setText(f"Apply failed: {error}")


def main():
    app = QApplication(sys.argv)
    window = MainWindow()
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
