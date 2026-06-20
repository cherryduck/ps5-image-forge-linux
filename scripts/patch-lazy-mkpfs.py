#!/usr/bin/env python3
"""Patch lazy_mkpfs for PyInstaller compatibility.

Applies patches to the lazy_mkpfs copy in site-packages so it works
correctly when bundled with PyInstaller (skips auto-install loops).
"""
import re
import sys
from pathlib import Path

def patch_init(site_packages: Path) -> None:
    """Patch __init__.py to skip auto-install when frozen."""
    init_file = site_packages / "lazy_mkpfs" / "__init__.py"
    content = init_file.read_text()

    if 'getattr(sys, "frozen", False)' in content:
        print("  __init__.py already patched")
        return

    # Add frozen check at the start of _ensure_dependencies
    old = 'def _ensure_dependencies():\n    """Check for required packages and auto-install them if missing."""'
    new = ('def _ensure_dependencies():\n    """Check for required packages and auto-install them if missing."""\n    # Skip auto-install in PyInstaller bundles\n    if getattr(sys, "frozen", False):\n        return')

    content = content.replace(old, new)
    init_file.write_text(content)
    print("  Patched __init__.py")


def patch_compression(site_packages: Path) -> None:
    """Patch compression.py to skip auto-install when frozen."""
    comp_file = site_packages / "lazy_mkpfs" / "compression.py"
    content = comp_file.read_text()

    if 'getattr(sys, "frozen", False)' in content:
        print("  compression.py already patched")
        return

    # Add frozen check at the start of _auto_install
    old = 'def _auto_install(package_name: str) -> bool:\n    """Attempt to auto-install a missing pip package."""'
    new = ('def _auto_install(package_name: str) -> bool:\n    """Attempt to auto-install a missing pip package."""\n    # Skip in PyInstaller bundles\n    if getattr(sys, "frozen", False):\n        return False')

    content = content.replace(old, new)
    comp_file.write_text(content)
    print("  Patched compression.py")


def patch_create_exfat(site_packages: Path) -> None:
    """Patch create_exfat.py to try pkexec before sudo (GUI-friendly auth)."""
    exfat_file = site_packages / "lazy_mkpfs" / "create_exfat.py"
    content = exfat_file.read_text()

    if "pkexec" in content:
        print("  create_exfat.py already patched")
        return

    # Replace all sudo loops to try pkexec first
    replacements = [
        # losetup attach
        ('    # Attach loop device — try without sudo first, fall back to sudo\n'
         '    for cmd in (\n'
         '        ["losetup", "--find", "--show", str(output_abs)],\n'
         '        ["sudo", "losetup", "--find", "--show", str(output_abs)],\n'
         '    ):',
         '    # Attach loop device — try without sudo first, then pkexec, then sudo\n'
         '    for cmd in (\n'
         '        ["losetup", "--find", "--show", str(output_abs)],\n'
         '        ["pkexec", "losetup", "--find", "--show", str(output_abs)],\n'
         '        ["sudo", "losetup", "--find", "--show", str(output_abs)],\n'
         '    ):'),
        # mount
        ('    # Mount — try without sudo first, fall back to sudo\n'
         '    mounted = False\n'
         '    for cmd in (\n'
         '        ["mount", "-o", f"uid={uid},gid={gid}", loop_dev, mount_point],\n'
         '        ["sudo", "mount", "-o", f"uid={uid},gid={gid}", loop_dev, mount_point],\n'
         '    ):',
         '    # Mount — try without sudo first, then pkexec, then sudo\n'
         '    mounted = False\n'
         '    for cmd in (\n'
         '        ["mount", "-o", f"uid={uid},gid={gid}", loop_dev, mount_point],\n'
         '        ["pkexec", "mount", "-o", f"uid={uid},gid={gid}", loop_dev, mount_point],\n'
         '        ["sudo", "mount", "-o", f"uid={uid},gid={gid}", loop_dev, mount_point],\n'
         '    ):'),
        # cleanup
        ('        subprocess.run(["umount", mount_point],         capture_output=True)\n'
         '        subprocess.run(["sudo", "umount", mount_point], capture_output=True)\n'
         '        if loop_dev:\n'
         '            subprocess.run(["losetup", "-d", loop_dev],         capture_output=True)\n'
         '            subprocess.run(["sudo", "losetup", "-d", loop_dev], capture_output=True)',
         '        for cmd in (["umount", mount_point], ["pkexec", "umount", mount_point], ["sudo", "umount", mount_point]):\n'
         '            subprocess.run(cmd, capture_output=True)\n'
         '        if loop_dev:\n'
         '            for cmd in (["losetup", "-d", loop_dev], ["pkexec", "losetup", "-d", loop_dev], ["sudo", "losetup", "-d", loop_dev]):\n'
         '                subprocess.run(cmd, capture_output=True)'),
    ]

    for old, new in replacements:
        if old in content:
            content = content.replace(old, new)

    exfat_file.write_text(content)
    print("  Patched create_exfat.py")


def main() -> None:
    # Find site-packages in the active venv
    import site
    # getsitepackages() can return system path; prefer getusersitepackages() or
    # walk sys.path to find the venv site-packages containing lazy_mkpfs
    import sys as _sys
    sp = None
    for p in _sys.path:
        candidate = Path(p) / "lazy_mkpfs"
        if candidate.is_dir():
            sp = Path(p)
            break
    if sp is None:
        sp = Path(site.getsitepackages()[0])

    lazy_mkpfs = sp / "lazy_mkpfs"
    if not lazy_mkpfs.is_dir():
        print(f"ERROR: lazy_mkpfs not found in {sp}")
        sys.exit(1)

    print(f"Patching lazy_mkpfs in {sp}...")
    patch_init(sp)
    patch_compression(sp)
    patch_create_exfat(sp)
    print("Done.")


if __name__ == "__main__":
    main()
