"""Custom progress bar widget with label."""

from __future__ import annotations

from PyQt6.QtWidgets import QProgressBar, QVBoxLayout, QWidget, QLabel
from PyQt6.QtCore import Qt, pyqtSignal


class ProgressWidget(QWidget):
    """Progress bar with a status label above it."""

    finished = pyqtSignal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(4)

        self.label = QLabel("Ready")
        self.label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.bar = QProgressBar(self)
        self.bar.setRange(0, 100)
        self.bar.setValue(0)
        self.bar.setTextVisible(True)

        layout.addWidget(self.label)
        layout.addWidget(self.bar)

    def set_status(self, text: str) -> None:
        """Update the status label."""
        self.label.setText(text)

    def set_progress(self, value: int) -> None:
        """Set progress percentage (0-100)."""
        self.bar.setValue(value)

    def reset(self) -> None:
        """Reset progress to 0."""
        self.bar.setValue(0)
        self.label.setText("Ready")

    def set_enabled(self, enabled: bool) -> None:
        """Enable/disable the progress bar."""
        if not enabled:
            self.bar.setRange(0, 100)
        else:
            self.bar.setRange(0, 0)  # Indeterminate
