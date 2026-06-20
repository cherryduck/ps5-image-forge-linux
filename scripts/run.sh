#!/bin/bash
set -euo pipefail
cd "$(dirname "$0")/.."

echo "=== Running PS5 Image Forge Linux (dev) ==="

# Ensure vendor submodule is initialized
if [ ! -d "vendor/lazy_mkpfs/lazy_mkpfs" ]; then
    echo "Initializing vendor submodule..."
    git submodule update --init --recursive
fi

uv run python -m ps5_image_forge_linux
