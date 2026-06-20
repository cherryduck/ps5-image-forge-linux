"""Runtime hook: ensure lazy_mkpfs vendor path is in sys.path before any imports."""
import sys
from pathlib import Path

if getattr(sys, "_MEIPASS", None):
    vendor_path = Path(sys._MEIPASS) / "vendor" / "lazy_mkpfs"
    if vendor_path.is_dir() and str(vendor_path) not in sys.path:
        sys.path.insert(0, str(vendor_path))
