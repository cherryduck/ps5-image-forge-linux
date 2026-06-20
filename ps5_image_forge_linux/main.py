"""App initialization and entry point."""

from __future__ import annotations

import os
import sys
import traceback
from pathlib import Path


def _ensure_lazy_mkpfs() -> None:
    """Ensure lazy_mkpfs is importable (dev mode: add vendor path)."""
    # Try direct import first (works in PyInstaller bundle)
    try:
        import lazy_mkpfs.compression  # noqa: F401
        return
    except ImportError:
        pass

    # Dev mode: add vendor/lazy_mkpfs to sys.path
    base_path = Path(__file__).resolve().parent.parent
    vendor_path = base_path / "vendor" / "lazy_mkpfs"
    if vendor_path.is_dir() and str(vendor_path) not in sys.path:
        sys.path.insert(0, str(vendor_path))


# Set up lazy_mkpfs import path
_ensure_lazy_mkpfs()

try:
    from lazy_mkpfs.compression import set_zlib_backend
except ImportError:
    set_zlib_backend = None  # type: ignore[assignment]


def _detect_zlib_backend() -> str:
    """Detect the best available zlib backend."""
    if set_zlib_backend is None:
        return "zlib (library unavailable)"

    # Try isa-l first
    try:
        set_zlib_backend("isa-l")
        return "Intel ISA-L"
    except Exception:
        pass

    # Try zlib-ng
    try:
        set_zlib_backend("zlib-ng")
        return "zlib-ng"
    except Exception:
        pass

    # Fallback to standard zlib
    set_zlib_backend("zlib")
    return "standard zlib"


def main() -> None:
    """Initialize and run the PS5 Image Forge Linux application."""
    # Set up exception hook
    sys.excepthook = _global_exception_handler

    # Detect compression backend early
    backend_name = _detect_zlib_backend()

    # Set up Qt application
    from PyQt6.QtWidgets import QApplication
    from PyQt6.QtGui import QIcon

    app = QApplication(sys.argv)
    app.setApplicationName("PS5 Image Forge Linux")
    app.setApplicationVersion("0.1.0")
    app.setStyle("Fusion")

    # Load icon
    icon_path = Path(__file__).resolve().parent / "resources" / "icon.svg"
    if not icon_path.exists():
        # PyInstaller mode: resources in _internal
        meipass = getattr(sys, "_MEIPASS", "")
        if meipass:
            icon_path = Path(meipass) / "ps5_image_forge_linux" / "resources" / "icon.svg"
    if icon_path.exists():
        app.setWindowIcon(QIcon(str(icon_path)))

    # Import and show main window
    from ps5_image_forge_linux.gui import MainWindow

    window = MainWindow(backend_name=backend_name)
    window.show()

    sys.exit(app.exec())


def _global_exception_handler(exc_type, exc_value, exc_tb) -> None:
    """Handle uncaught exceptions gracefully."""
    from PyQt6.QtWidgets import QMessageBox, QApplication

    traceback_str = "".join(traceback.format_exception(exc_type, exc_value, exc_tb))
    message = f"An unexpected error occurred:\n\n{traceback_str[:1000]}"

    app = QApplication.instance()
    if app:
        QMessageBox.critical(None, "Fatal Error", message)
    else:
        print(message, file=sys.stderr)
