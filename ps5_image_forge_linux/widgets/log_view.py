"""Scrollable log output widget."""

from __future__ import annotations

from PyQt6.QtWidgets import QTextEdit
from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtGui import QTextCursor, QFont


class LogView(QTextEdit):
    """Read-only text area for build log output."""

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setReadOnly(True)
        self.setFont(QFont("monospace", 9))
        self.setLineWrapMode(QTextEdit.LineWrapMode.NoWrap)
        self.setStyleSheet("background-color: #1e1e1e; color: #d4d4d4; border: 1px solid #3c3c3c;")

    def log(self, message: str) -> None:
        """Append a log message and auto-scroll."""
        self.append(message)
        cursor = self.textCursor()
        cursor.movePosition(QTextCursor.MoveOperation.End)
        self.setTextCursor(cursor)

    def clear_log(self) -> None:
        """Clear all log output."""
        self.clear()
