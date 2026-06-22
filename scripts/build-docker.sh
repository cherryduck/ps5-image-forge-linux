#!/bin/bash
set -euo pipefail
cd "$(dirname "$0")/.."

IMAGE_NAME="ps5-image-forge-builder"
CONTAINER_NAME="ps5-image-forge-build-$(date +%s)"
DOCKER_BUILDKIT=1
export DOCKER_BUILDKIT

echo "=== Building PS5 Image Forge Linux (Docker, x86-64-v2 compatible) ==="

# Ensure vendor submodule is initialized (needed for COPY in Dockerfile)
if [ ! -d "vendor/lazy_mkpfs/lazy_mkpfs" ]; then
    echo "[0/4] Initializing vendor submodule..."
    git submodule update --init --recursive
fi

# Build the Docker image
echo "[1/4] Building Docker image..."
docker build -f Dockerfile.build -t "$IMAGE_NAME" .

# Run container to extract output (build already ran during docker build)
echo "[2/4] Extracting build output..."
docker create --name "$CONTAINER_NAME" "$IMAGE_NAME"
# Remove old dist first to avoid nested ./dist/dist/ from docker cp
rm -rf ./dist
docker cp "$CONTAINER_NAME":/src/dist ./dist
docker rm "$CONTAINER_NAME" > /dev/null

# Clean up
rm -rf build/

# Verify the build
if [ -d "dist/ps5-image-forge-linux" ]; then
    echo "[3/4] Verifying no AVX-512 instructions in output..."

    FAIL=0
    CHECKED=0
    for f in $(find dist/ps5-image-forge-linux/_internal \( -name '*.so' -o -name 'libpython*' \) 2>/dev/null); do
        CHECKED=$((CHECKED + 1))
        if objdump -d "$f" 2>/dev/null | grep -qP '(vmovdqu8|vmovdqa64|vmovdqu64|vmovdqa32|vbroadcasti32x8|vbroadcasti64x2|vshufi32x4|vshufi64x2|vpgatherdq|vpgatherqq|vpermpd|vpermps|vpdp\.|vpmuludq|vpclmulqdq|vaesenc|vaesdec|zmm|k[0-7](\s|,))'; then
            echo "  ⚠ FAIL: $f contains AVX-512 instructions"
            FAIL=1
        fi
    done

    if [ "$FAIL" -eq 0 ]; then
        echo "  ✓ All $CHECKED shared libraries are clean — x86-64-v2 compatible"
    else
        echo "  ⚠ Some binaries still contain AVX-512 instructions"
        echo "    Inspect: objdump -d <file> | grep -iE 'vmovdqu8|vbroadcasti32x8|zmm'"
    fi

    echo ""
    echo "[4/4] Build complete"
    echo "Output: dist/ps5-image-forge-linux/"
    echo ""
    echo "To install for desktop use:"
    echo "  bash scripts/install-desktop.sh"
else
    echo "ERROR: Build failed — dist/ps5-image-forge-linux/ not found"
    docker rm "$CONTAINER_NAME" > /dev/null 2>&1 || true
    exit 1
fi
