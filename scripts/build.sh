#!/bin/bash
set -euo pipefail
cd "$(dirname "$0")/.."

echo "=== Building PS5 Image Forge Linux ==="

# Ensure vendor submodule is initialized
if [ ! -d "vendor/lazy_mkpfs/lazy_mkpfs" ]; then
    echo "Initializing vendor submodule..."
    git submodule update --init --recursive
fi

# Use venv Python (create if missing)
if [ ! -d ".venv" ]; then
    echo "Creating virtual environment..."
    python3 -m venv .venv
fi
source .venv/bin/activate

# Copy lazy_mkpfs into venv site-packages for PyInstaller to detect
VENV_SP=$(python3 -c "import site; print(site.getsitepackages()[0])")
if [ ! -d "$VENV_SP/lazy_mkpfs" ]; then
    echo "Installing lazy_mkpfs into venv..."
    cp -r vendor/lazy_mkpfs/lazy_mkpfs "$VENV_SP/lazy_mkpfs"
fi

# Install runtime + build dependencies
pip install -e . --quiet 2>/dev/null
pip install pyinstaller --quiet 2>/dev/null

# Patch lazy_mkpfs for PyInstaller compatibility (skip auto-install loops)
echo "Patching lazy_mkpfs..."
python3 scripts/patch-lazy-mkpfs.py

# Clean previous build
rm -rf dist/ build/

# Build from spec file
pyinstaller --clean ps5-image-forge-linux.spec

echo ""
echo "=== Build complete ==="
echo "Output: dist/ps5-image-forge-linux/"
echo ""
echo "To install for desktop use:"
echo "  bash scripts/install-desktop.sh"
