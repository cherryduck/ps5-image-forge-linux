"""PyInstaller hook for lazy_mkpfs - ensures vendor path is in sys.path."""
import sys
from pathlib import Path

# In PyInstaller bundle, _MEIPASS is the extraction directory (_internal/)
if getattr(sys, "_MEIPASS", None):
    meipass = Path(sys._MEIPASS)
    vendor_path = meipass / "vendor" / "lazy_mkpfs"
    if vendor_path.is_dir():
        if str(vendor_path) not in sys.path:
            sys.path.insert(0, str(vendor_path))
