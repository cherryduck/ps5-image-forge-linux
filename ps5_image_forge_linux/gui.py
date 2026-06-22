"""Main GUI window for PS5 Image Forge Linux."""

from __future__ import annotations

import ctypes
import dataclasses
import os
import subprocess
import sys
import threading
from pathlib import Path
from typing import Optional

from PyQt6.QtWidgets import (
    QApplication,
    QFileDialog,
    QHeaderView,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QMessageBox,
    QProgressBar,
    QRadioButton,
    QScrollArea,
    QSizePolicy,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)
from PyQt6.QtCore import Qt, QThread, pyqtSignal, pyqtSlot

from ps5_image_forge_linux.builder import CancelledBuild, OutputFormat, build
from ps5_image_forge_linux.widgets.log_view import LogView


@dataclasses.dataclass
class BuildQueueItem:
    """A single item in the build queue."""
    source: Path
    output_format: str  # OutputFormat.*
    is_file_input: bool


class BatchBuildWorker(QThread):
    """Background thread for batch build operations."""

    log = pyqtSignal(str)
    progress = pyqtSignal(int)
    batch_item_start = pyqtSignal(int, int, str, str)  # idx, total, source_name, fmt
    batch_item_done = pyqtSignal(int, str)  # idx, output_path
    batch_item_failed = pyqtSignal(int, str)  # idx, error_msg
    batch_finished = pyqtSignal(int, int)  # succeeded, failed  (renamed to avoid shadowing QThread.finished)
    cancelled = pyqtSignal()

    def __init__(
        self,
        queue: list[BuildQueueItem],
        output_dir: Path,
    ) -> None:
        super().__init__()
        self.queue = queue
        self.output_dir = output_dir
        self._cancel_event = threading.Event()
        self._proc: subprocess.Popen | None = None

    def run(self) -> None:
        """Execute all queued builds sequentially."""
        total = len(self.queue)
        succeeded = 0
        failed = 0

        for idx, item in enumerate(self.queue):
            if self._cancel_event.is_set():
                break

            source_name = item.source.name
            self.batch_item_start.emit(idx, total, source_name, item.output_format)

            def _track_proc(proc: subprocess.Popen) -> None:
                self._proc = proc

            try:
                result = build(
                    source=item.source,
                    output_dir=self.output_dir,
                    output_format=item.output_format,
                    is_file_input=item.is_file_input,
                    log_callback=self._log,
                    progress_callback=self._progress_for(idx, total),
                    cancel_event=self._cancel_event,
                    proc_callback=_track_proc,
                )
                self.batch_item_done.emit(idx, str(result))
                succeeded += 1
            except CancelledBuild:
                self.cancelled.emit()
                return
            except KeyboardInterrupt:
                self.cancelled.emit()
                return
            except Exception as e:
                self.batch_item_failed.emit(idx, str(e))
                failed += 1

        self.batch_finished.emit(succeeded, failed)

    def _progress_for(self, idx: int, total: int):
        """Return a progress callback that scales within this item's slice."""
        def _callback(pct: int) -> None:
            # Each item gets 100/total percent of the bar
            base = (idx / total) * 100
            slice_size = 100 / total
            overall = base + (pct / 100) * slice_size
            self.progress.emit(int(overall))
        return _callback

    def _log(self, message: str) -> None:
        self.log.emit(message)

    def stop(self) -> None:
        """Cancel the batch build."""
        self._cancel_event.set()

        proc = self._proc
        if proc is not None and proc.poll() is None:
            try:
                proc.terminate()
            except OSError:
                pass
            try:
                proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                try:
                    proc.kill()
                    proc.wait(timeout=2)
                except OSError:
                    pass

        try:
            thread_id = int(self.currentThreadId())
            res = ctypes.pythonapi.PyThreadState_SetAsyncExc(
                thread_id, ctypes.py_object(KeyboardInterrupt)
            )
            if res == 0:
                raise RuntimeError("Invalid thread ID")
            elif res > 1:
                ctypes.pythonapi.PyThreadState_SetAsyncExc(thread_id, 0)
        except Exception:
            pass


class BuildWorker(QThread):
    """Background thread for single build operations."""

    log = pyqtSignal(str)
    progress = pyqtSignal(int)
    build_finished = pyqtSignal(str)  # output path  (renamed to avoid shadowing QThread.finished)
    error = pyqtSignal(str)  # error message
    cancelled = pyqtSignal()  # emitted when user cancels

    def __init__(
        self,
        source: Path,
        output_dir: Path,
        output_format: str,
        is_file_input: bool,
    ) -> None:
        super().__init__()
        self.source = source
        self.output_dir = output_dir
        self.output_format = output_format
        self.is_file_input = is_file_input
        self._cancel_event = threading.Event()
        self._proc: subprocess.Popen | None = None

    def run(self) -> None:
        """Execute the build in a background thread."""
        def _track_proc(proc: subprocess.Popen) -> None:
            self._proc = proc

        try:
            result = build(
                source=self.source,
                output_dir=self.output_dir,
                output_format=self.output_format,
                is_file_input=self.is_file_input,
                log_callback=self._log,
                progress_callback=self._progress,
                cancel_event=self._cancel_event,
                proc_callback=_track_proc,
            )
            self.build_finished.emit(str(result))
        except CancelledBuild:
            self.cancelled.emit()
        except KeyboardInterrupt:
            # Fallback: if KeyboardInterrupt escapes, treat as cancellation
            self.cancelled.emit()
        except Exception as e:
            self.error.emit(str(e))

    def _log(self, message: str) -> None:
        self.log.emit(message)

    def _progress(self, value: int) -> None:
        self.progress.emit(value)

    def stop(self) -> None:
        """Cancel the build: signal event, kill subprocess, interrupt thread."""
        self._cancel_event.set()

        # Terminate tracked subprocess
        proc = self._proc
        if proc is not None and proc.poll() is None:
            try:
                proc.terminate()
            except OSError:
                pass
            # Wait briefly for graceful exit, then force-kill
            try:
                proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                try:
                    proc.kill()
                    proc.wait(timeout=2)
                except OSError:
                    pass

        # Inject KeyboardInterrupt into this thread to unblock pure-Python operations
        # (e.g., PFS compression loops, file I/O)
        try:
            thread_id = int(self.currentThreadId())
            res = ctypes.pythonapi.PyThreadState_SetAsyncExc(
                thread_id, ctypes.py_object(KeyboardInterrupt)
            )
            if res == 0:
                raise RuntimeError("Invalid thread ID")
            elif res > 1:
                # Wind down: release the async exc
                ctypes.pythonapi.PyThreadState_SetAsyncExc(thread_id, 0)
        except Exception:
            pass  # Best effort — cancellation still works via the event


class MainWindow(QMainWindow):
    """Main application window."""

    def __init__(self, backend_name: str = "unknown") -> None:
        super().__init__()
        self.backend_name = backend_name

        self.source_path: Optional[Path] = None
        self.is_file_input = False
        self.output_dir: Optional[Path] = None
        self.selected_format = OutputFormat.FFPFSC

        # Build workers
        self.build_worker: Optional[BuildWorker] = None
        self.batch_worker: Optional[BatchBuildWorker] = None

        # Queue
        self.build_queue: list[BuildQueueItem] = []

        self._init_ui()

    def _init_ui(self) -> None:
        """Initialize the user interface."""
        self.setWindowTitle("PS5 Image Forge Linux")
        self.resize(750, 600)

        # Central widget
        central = QWidget()
        self.setCentralWidget(central)
        main_layout = QVBoxLayout(central)
        main_layout.setSpacing(12)
        main_layout.setContentsMargins(16, 16, 16, 16)

        # --- Source Section ---
        source_label = QLabel("Source")
        source_label.setStyleSheet("font-weight: bold; font-size: 13px;")
        main_layout.addWidget(source_label)

        # Input type toggle
        input_type_row = QHBoxLayout()
        input_type_row.addWidget(QLabel("Type:"))
        self.rdo_folder_input = QRadioButton("Folder")
        self.rdo_folder_input.setChecked(True)
        self.rdo_folder_input.clicked.connect(lambda: setattr(self, "is_file_input", False))
        input_type_row.addWidget(self.rdo_folder_input)
        self.rdo_file_input = QRadioButton("File (.exfat / .ffpkg)")
        self.rdo_file_input.clicked.connect(lambda: setattr(self, "is_file_input", True))
        input_type_row.addWidget(self.rdo_file_input)
        input_type_row.addStretch()
        main_layout.addLayout(input_type_row)

        source_row = QHBoxLayout()
        self.source_edit = QLabel("No source selected")
        self.source_edit.setStyleSheet("background: #2d2d2d; color: #cccccc; padding: 6px; border-radius: 4px;")
        self.source_edit.setWordWrap(True)
        self.source_edit.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        source_browse = self._styled_button("Browse...")
        source_browse.clicked.connect(self._browse_source)
        source_row.addWidget(self.source_edit, 1)
        source_row.addWidget(source_browse)

        main_layout.addLayout(source_row)

        # Source info
        self.source_info = QLabel("")
        self.source_info.setStyleSheet("color: #888888; font-size: 11px;")
        main_layout.addWidget(self.source_info)

        # --- Output Format Section ---
        format_label = QLabel("Output Format")
        format_label.setStyleSheet("font-weight: bold; font-size: 13px;")
        main_layout.addWidget(format_label)

        self.folder_formats = QWidget()
        folder_layout = QVBoxLayout(self.folder_formats)
        folder_layout.setContentsMargins(0, 0, 0, 0)
        folder_layout.setSpacing(6)

        self.rdo_ffpkg = QRadioButton("FFPKG  (UFS2 via UFS2Tool)")
        self.rdo_ffpkg.clicked.connect(lambda: self._select_format(OutputFormat.FFPKG))
        folder_layout.addWidget(self.rdo_ffpkg)

        self.rdo_exfat = QRadioButton("EXFAT  (raw EXFAT image)")
        self.rdo_exfat.clicked.connect(lambda: self._select_format(OutputFormat.EXFAT))
        folder_layout.addWidget(self.rdo_exfat)

        self.rdo_ffpfsc = QRadioButton("FFPFSC  (EXFAT wrapper + compressed PFS)")
        self.rdo_ffpfsc.setChecked(True)
        self.rdo_ffpfsc.clicked.connect(lambda: self._select_format(OutputFormat.FFPFSC))
        folder_layout.addWidget(self.rdo_ffpfsc)

        main_layout.addWidget(self.folder_formats)

        self.file_formats = QWidget()
        file_layout = QVBoxLayout(self.file_formats)
        file_layout.setContentsMargins(0, 0, 0, 0)
        file_layout.setSpacing(6)

        self.rdo_file_ffpfsc = QRadioButton("FFPFSC  (compress to PFS)")
        self.rdo_file_ffpfsc.setChecked(True)
        file_layout.addWidget(self.rdo_file_ffpfsc)

        main_layout.addWidget(self.file_formats)
        self.file_formats.hide()

        # --- Add to Queue button (placed after format so it's already selected) ---
        self.add_queue_btn = self._styled_button("Add to Queue")
        self.add_queue_btn.setEnabled(False)
        self.add_queue_btn.clicked.connect(self._add_to_queue)
        add_queue_row = QHBoxLayout()
        add_queue_row.addWidget(self.add_queue_btn)
        add_queue_row.addStretch()
        main_layout.addLayout(add_queue_row)

        # --- Queue Section ---
        queue_section = QWidget()
        queue_section.hide()
        queue_layout = QVBoxLayout(queue_section)
        queue_layout.setContentsMargins(0, 0, 0, 0)
        queue_layout.setSpacing(6)

        queue_header = QHBoxLayout()
        queue_label = QLabel("Build Queue")
        queue_label.setStyleSheet("font-weight: bold; font-size: 13px;")
        queue_header.addWidget(queue_label)
        queue_header.addStretch()
        self.queue_count_label = QLabel("")
        self.queue_count_label.setStyleSheet("color: #888888; font-size: 11px;")
        queue_header.addWidget(self.queue_count_label)
        self.clear_queue_btn = self._styled_button("Clear Queue")
        self.clear_queue_btn.clicked.connect(self._clear_queue)
        queue_header.addWidget(self.clear_queue_btn)
        queue_layout.addLayout(queue_header)

        self.queue_table = QTableWidget()
        self.queue_table.setColumnCount(4)
        self.queue_table.setHorizontalHeaderLabels(["#", "Source", "Format", "Status"])
        header = self.queue_table.horizontalHeader()
        if header:
            header.setSectionResizeMode(0, QHeaderView.ResizeMode.Fixed)
            header.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
            header.setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
            header.setSectionResizeMode(3, QHeaderView.ResizeMode.ResizeToContents)
        self.queue_table.setColumnWidth(0, 40)
        self.queue_table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.queue_table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        vheader = self.queue_table.verticalHeader()
        if vheader:
            vheader.hide()
        self.queue_table.setStyleSheet("""
            QTableWidget {
                background: #2d2d2d;
                border: 1px solid #3c3c3c;
                border-radius: 4px;
                gridline-color: #3c3c3c;
            }
            QTableWidget::item {
                padding: 4px;
                color: #d4d4d4;
            }
            QHeaderView::section {
                background: #3c3c3c;
                color: #d4d4d4;
                border: none;
                padding: 4px;
            }
        """)
        queue_layout.addWidget(self.queue_table)

        main_layout.addWidget(queue_section)
        self._queue_section = queue_section

        # --- Output Directory Section ---
        output_label = QLabel("Output Directory")
        output_label.setStyleSheet("font-weight: bold; font-size: 13px;")
        main_layout.addWidget(output_label)

        output_row = QHBoxLayout()
        self.output_edit = QLabel("Not selected")
        self.output_edit.setStyleSheet("background: #2d2d2d; color: #cccccc; padding: 6px; border-radius: 4px;")
        output_browse = self._styled_button("Browse...")
        output_browse.clicked.connect(self._browse_output)
        output_row.addWidget(self.output_edit, 1)
        output_row.addWidget(output_browse)
        main_layout.addLayout(output_row)

        # Output filename preview
        self.output_filename = QLabel("")
        self.output_filename.setStyleSheet("color: #888888; font-size: 11px;")
        main_layout.addWidget(self.output_filename)

        # --- Buttons ---
        button_row = QHBoxLayout()
        self.build_btn = self._styled_button("BUILD")
        self.build_btn.setStyleSheet("background: #0078d4; color: white; font-weight: bold; padding: 10px;")
        self.build_btn.setEnabled(False)
        self.build_btn.clicked.connect(self._start_build)
        button_row.addWidget(self.build_btn)

        self.cancel_btn = self._styled_button("Cancel")
        self.cancel_btn.setEnabled(False)
        self.cancel_btn.hide()
        self.cancel_btn.clicked.connect(self._cancel_build)
        button_row.addWidget(self.cancel_btn)

        main_layout.addLayout(button_row)

        # --- Progress ---
        self.progress_bar = QProgressBar()
        self.progress_bar.setRange(0, 100)
        self.progress_bar.setValue(0)
        self.progress_bar.setTextVisible(True)
        self.progress_bar.setStyleSheet("""
            QProgressBar {
                background: #2d2d2d;
                border: 1px solid #3c3c3c;
                border-radius: 4px;
                text-align: center;
                height: 24px;
            }
            QProgressBar::chunk {
                background: #0078d4;
                border-radius: 3px;
            }
        """)
        main_layout.addWidget(self.progress_bar)

        # --- Log ---
        log_label = QLabel("Log")
        log_label.setStyleSheet("font-weight: bold; font-size: 13px;")
        main_layout.addWidget(log_label)

        self.log_view = LogView()
        self.log_view.setMinimumHeight(150)
        main_layout.addWidget(self.log_view, 1)

        # Initial log
        self.log_view.log(f"[INFO] Compression backend: {self.backend_name}")
        self.log_view.log("[INFO] Ready.")

        # Apply global stylesheet
        self.setStyleSheet("""
            QMainWindow { background: #1e1e1e; }
            QLabel { color: #d4d4d4; }
            QRadioButton { color: #d4d4d4; spacing: 8px; }
            QPushButton {
                background: #3c3c3c;
                color: #d4d4d4;
                border: 1px solid #555555;
                border-radius: 4px;
                padding: 6px 16px;
            }
            QPushButton:hover { background: #4c4c4c; }
            QPushButton:disabled { background: #2a2a2a; color: #666666; }
        """)

    def _styled_button(self, text: str):
        from PyQt6.QtWidgets import QPushButton
        btn = QPushButton(text)
        return btn

    # ------------------------------------------------------------------
    # Source / Output browsing
    # ------------------------------------------------------------------

    def _browse_source(self) -> None:
        """Browse for source — folder or file based on toggle."""
        if self.is_file_input:
            file_path, _ = QFileDialog.getOpenFileName(
                self, "Select Source File", "",
                "Image Files (*.exfat *.ffpkg);;All Files (*)"
            )
            if file_path:
                self.source_path = Path(file_path)
                self._update_source_display()
        else:
            folder = QFileDialog.getExistingDirectory(self, "Select Source Folder")
            if folder:
                self.source_path = Path(folder)
                self._update_source_display()

    def _browse_output(self) -> None:
        """Open folder picker for output directory."""
        folder = QFileDialog.getExistingDirectory(self, "Select Output Directory")
        if folder:
            self.output_dir = Path(folder)
            self.output_edit.setText(str(self.output_dir))
            self._update_output_filename()
            self._check_ready()

    def _update_source_display(self) -> None:
        """Update source display and format options."""
        if self.source_path is None:
            return

        self.source_edit.setText(str(self.source_path))

        # Calculate size
        if self.is_file_input:
            try:
                size = self.source_path.stat().st_size
                self.source_info.setText(f"File ({self._human_size(size)})")
            except OSError:
                self.source_info.setText("File")
            # Show only file format options
            self.folder_formats.hide()
            self.file_formats.show()
            self.selected_format = OutputFormat.FFPFSC
            self.rdo_file_input.setChecked(True)
            self.rdo_folder_input.setChecked(False)
        else:
            try:
                total = 0
                count = 0
                for p in self.source_path.rglob("*"):
                    if p.is_file():
                        total += p.stat().st_size
                        count += 1
                self.source_info.setText(f"Folder ({count} files, {self._human_size(total)})")
            except OSError:
                self.source_info.setText("Folder")
            # Show folder format options
            self.folder_formats.show()
            self.file_formats.hide()
            self.rdo_folder_input.setChecked(True)
            self.rdo_file_input.setChecked(False)

        # Enable "Add to Queue" button
        self.add_queue_btn.setEnabled(True)

        self._update_output_filename()
        self._check_ready()

    # ------------------------------------------------------------------
    # Queue management
    # ------------------------------------------------------------------

    def _current_format(self) -> str:
        """Return the currently selected output format."""
        if self.is_file_input:
            return OutputFormat.FFPFSC
        return self.selected_format

    def _add_to_queue(self) -> None:
        """Add the current source + format to the build queue."""
        if self.source_path is None:
            return

        fmt = self._current_format()
        item = BuildQueueItem(
            source=self.source_path,
            output_format=fmt,
            is_file_input=self.is_file_input,
        )
        self.build_queue.append(item)

        self._refresh_queue_table()
        self.log_view.log(f"[INFO] Added to queue ({len(self.build_queue)}): "
                          f"{self.source_path.name} → {fmt}")
        self._check_ready()

    def _clear_queue(self) -> None:
        """Clear the build queue."""
        self.build_queue.clear()
        self._refresh_queue_table()
        self._check_ready()

    def _refresh_queue_table(self) -> None:
        """Refresh the queue table widget to match self.build_queue."""
        self.queue_table.setRowCount(len(self.build_queue))
        for idx, item in enumerate(self.build_queue):
            # Index (1-based)
            idx_item = QTableWidgetItem(str(idx + 1))
            idx_item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
            self.queue_table.setItem(idx, 0, idx_item)

            # Source name (full path in tooltip)
            name_item = QTableWidgetItem(item.source.name)
            name_item.setToolTip(str(item.source))
            self.queue_table.setItem(idx, 1, name_item)

            # Format
            fmt_item = QTableWidgetItem(item.output_format)
            self.queue_table.setItem(idx, 2, fmt_item)

            # Status
            status_item = QTableWidgetItem("Queued")
            status_item.setForeground(Qt.GlobalColor.gray)
            self.queue_table.setItem(idx, 3, status_item)

        # Show/hide queue section
        if self.build_queue:
            self._queue_section.show()
            self.queue_count_label.setText(f"{len(self.build_queue)} item(s)")
        else:
            self._queue_section.hide()
            self.queue_count_label.setText("")

    def _set_queue_status(self, idx: int, status: str) -> None:
        """Update the status column for a queue item."""
        row_item = self.queue_table.item(idx, 3)
        if row_item:
            row_item.setText(status)
            if status == "Done":
                row_item.setForeground(Qt.GlobalColor.green)
            elif status == "Failed":
                row_item.setForeground(Qt.GlobalColor.red)

    # ------------------------------------------------------------------
    # Format / output display
    # ------------------------------------------------------------------

    def _update_output_filename(self) -> None:
        """Update the output filename preview."""
        if self.build_queue:
            self.output_filename.setText(f"{len(self.build_queue)} item(s) in queue")
        elif self.source_path is None or self.output_dir is None:
            self.output_filename.setText("")
            return
        else:
            if self.is_file_input:
                stem = self.source_path.stem
                filename = f"{stem}.ffpfsc"
            else:
                # Try to get title ID
                title_id = None
                param_path = self.source_path / "param.json"
                if param_path.is_file():
                    try:
                        import json
                        with param_path.open("r") as f:
                            data = json.load(f)
                        title_id = data.get("titleId")
                    except Exception:
                        pass

                if not title_id:
                    import re
                    match = re.search(r'(CUSA|PPSA|PCAS|PCJS|PCES|NPXS)\d{5}', str(self.source_path), re.IGNORECASE)
                    if match:
                        title_id = match.group(0).upper()

                stem = title_id if title_id else self.source_path.name

                ext_map = {
                    OutputFormat.FFPKG: "ffpkg",
                    OutputFormat.EXFAT: "exfat",
                    OutputFormat.FFPFSC: "ffpfsc",
                }
                ext = ext_map.get(self.selected_format, "ffpfsc")
                filename = f"{stem}.{ext}"

            self.output_filename.setText(f"Output filename: {filename}")

    def _select_format(self, fmt: str) -> None:
        """Select an output format."""
        self.selected_format = fmt
        self._update_output_filename()

    def _check_ready(self) -> None:
        """Check if the build button should be enabled."""
        ready = self.output_dir is not None and (
            self.source_path is not None or len(self.build_queue) > 0
        )
        self.build_btn.setEnabled(ready)

    # ------------------------------------------------------------------
    # Build orchestration
    # ------------------------------------------------------------------

    def _restore_ui(self) -> None:
        """Restore UI to pre-build state."""
        self.build_btn.setEnabled(True)
        self.cancel_btn.setEnabled(False)
        self.cancel_btn.hide()
        self._check_ready()

    def _start_build(self) -> None:
        """Start the build process — single or batch."""
        if self.output_dir is None:
            return

        # If there's an existing worker still running, stop it first
        for worker in [self.build_worker, self.batch_worker]:
            if worker and worker.isRunning():
                worker.stop()
                worker.wait(5000)

        # Decide: batch mode or single mode?
        if self.build_queue:
            self._start_batch_build()
        elif self.source_path is not None:
            self._start_single_build()
        else:
            return

    def _start_single_build(self) -> None:
        """Start a single-item build."""
        assert self.source_path is not None
        assert self.output_dir is not None
        fmt = self._current_format()

        # Disable UI
        self.build_btn.setEnabled(False)
        self.cancel_btn.setEnabled(True)
        self.cancel_btn.show()
        self.progress_bar.setValue(0)
        self.progress_bar.setRange(0, 100)

        self.log_view.log(f"\n[INFO] Starting build...")
        self.log_view.log(f"  Source: {self.source_path}")
        self.log_view.log(f"  Output: {self.output_dir}")
        self.log_view.log(f"  Format: {fmt}")

        # Start worker thread
        self.build_worker = BuildWorker(
            source=self.source_path,
            output_dir=self.output_dir,
            output_format=fmt,
            is_file_input=self.is_file_input,
        )
        self.build_worker.log.connect(self._on_log)
        self.build_worker.progress.connect(self._on_progress)
        self.build_worker.build_finished.connect(self._on_build_finished)
        self.build_worker.error.connect(self._on_build_error)
        self.build_worker.cancelled.connect(self._on_build_cancelled)
        self.build_worker.start()

    def _start_batch_build(self) -> None:
        """Start a batch build from the queue."""
        assert self.output_dir is not None
        total = len(self.build_queue)

        # Disable UI
        self.build_btn.setEnabled(False)
        self.cancel_btn.setEnabled(True)
        self.cancel_btn.show()
        self.progress_bar.setValue(0)
        self.progress_bar.setRange(0, 100)

        # Reset queue statuses
        for idx in range(total):
            self._set_queue_status(idx, "Queued")

        self.log_view.log(f"\n[INFO] Starting batch build ({total} item(s))...")
        self.log_view.log(f"  Output: {self.output_dir}")

        # Start batch worker
        self.batch_worker = BatchBuildWorker(
            queue=self.build_queue,
            output_dir=self.output_dir,
        )
        self.batch_worker.log.connect(self._on_log)
        self.batch_worker.progress.connect(self._on_progress)
        self.batch_worker.batch_item_start.connect(self._on_batch_item_start)
        self.batch_worker.batch_item_done.connect(self._on_batch_item_done)
        self.batch_worker.batch_item_failed.connect(self._on_batch_item_failed)
        self.batch_worker.batch_finished.connect(self._on_batch_finished)
        self.batch_worker.cancelled.connect(self._on_build_cancelled)
        self.batch_worker.start()

    def _cancel_build(self) -> None:
        """Cancel the current build (single or batch)."""
        worker = self.build_worker or self.batch_worker
        if worker and worker.isRunning():
            self.log_view.log("[INFO] Cancelling build...")
            worker.stop()
            worker.wait(10000)

    # ------------------------------------------------------------------
    # Signal handlers — single build
    # ------------------------------------------------------------------

    @pyqtSlot(str)
    def _on_log(self, message: str) -> None:
        """Handle log message from worker."""
        self.log_view.log(message)

    @pyqtSlot(int)
    def _on_progress(self, value: int) -> None:
        """Handle progress update from worker."""
        self.progress_bar.setValue(value)

    @pyqtSlot(str)
    def _on_build_finished(self, output_path: str) -> None:
        """Handle single build completion."""
        self._restore_ui()
        self.progress_bar.setValue(100)

        self.log_view.log(f"\n[DONE] Build completed successfully!")
        self.log_view.log(f"  Output: {output_path}")

        # Schedule worker cleanup via Qt event loop to avoid race with
        # QThread.finished signal delivery (worker may still have pending
        # signals in the queue when this slot returns)
        worker = self.build_worker
        self.build_worker = None
        if worker is not None:
            worker.deleteLater()

        QMessageBox.information(
            self,
            "Build Complete",
            f"Image created successfully:\n\n{output_path}",
        )

    @pyqtSlot(str)
    def _on_build_error(self, error_msg: str) -> None:
        """Handle single build error."""
        self._restore_ui()
        self.progress_bar.setValue(0)

        # Let Qt manage worker lifetime to avoid GC racing with QThread.finished
        worker = self.build_worker
        self.build_worker = None
        if worker is not None:
            worker.deleteLater()

        self.log_view.log(f"\n[ERROR] Build failed: {error_msg}")

        QMessageBox.critical(
            self,
            "Build Failed",
            f"Build failed:\n\n{error_msg}",
        )

    @pyqtSlot()
    def _on_build_cancelled(self) -> None:
        """Handle build cancellation."""
        self._restore_ui()
        self.progress_bar.setValue(0)

        # Let Qt manage worker lifetime to avoid GC racing with QThread.finished
        worker = self.build_worker or self.batch_worker
        self.build_worker = None
        self.batch_worker = None
        if worker is not None:
            worker.deleteLater()

        self.log_view.log("\n[INFO] Build cancelled.")
        self.log_view.log("[INFO] Ready.")

    # ------------------------------------------------------------------
    # Signal handlers — batch build
    # ------------------------------------------------------------------

    @pyqtSlot(int, int, str, str)
    def _on_batch_item_start(self, idx: int, total: int, source_name: str, fmt: str) -> None:
        """Handle batch item start."""
        self._set_queue_status(idx, "Building")
        self.log_view.log(f"\n[INFO] Building {idx + 1}/{total}: {source_name} → {fmt}")

    @pyqtSlot(int, str)
    def _on_batch_item_done(self, idx: int, output_path: str) -> None:
        """Handle batch item completion."""
        self._set_queue_status(idx, "Done")
        self.log_view.log(f"[DONE] {output_path}")

    @pyqtSlot(int, str)
    def _on_batch_item_failed(self, idx: int, error_msg: str) -> None:
        """Handle batch item failure."""
        self._set_queue_status(idx, "Failed")
        self.log_view.log(f"[ERROR] Item {idx + 1} failed: {error_msg}")

    @pyqtSlot(int, int)
    def _on_batch_finished(self, succeeded: int, failed: int) -> None:
        """Handle batch build completion."""
        self._restore_ui()
        self.progress_bar.setValue(100)

        # Let Qt manage worker lifetime to avoid GC racing with QThread.finished
        worker = self.batch_worker
        self.batch_worker = None
        if worker is not None:
            worker.deleteLater()

        total = succeeded + failed
        self.log_view.log(f"\n[DONE] Batch build complete: {succeeded}/{total} succeeded, {failed}/{total} failed.")

        if failed == 0:
            QMessageBox.information(
                self,
                "Batch Complete",
                f"All {succeeded} item(s) built successfully.",
            )
        elif succeeded == 0:
            QMessageBox.critical(
                self,
                "Batch Failed",
                f"All {failed} item(s) failed. Check the log for details.",
            )
        else:
            QMessageBox.warning(
                self,
                "Batch Complete with Errors",
                f"{succeeded} succeeded, {failed} failed. Check the log for details.",
            )

    # ------------------------------------------------------------------
    # Utilities
    # ------------------------------------------------------------------

    @staticmethod
    def _human_size(size: int) -> str:
        """Convert bytes to human-readable size."""
        s: float = float(size)
        for unit in ["B", "KB", "MB", "GB", "TB"]:
            if s < 1024:
                return f"{s:.1f} {unit}"
            s /= 1024
        return f"{s:.1f} PB"
