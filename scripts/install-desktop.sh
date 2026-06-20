#!/bin/bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
DIST_DIR="$PROJECT_DIR/dist/ps5-image-forge-linux"

if [ ! -f "$DIST_DIR/ps5-image-forge-linux" ]; then
    echo "ERROR: Build not found at $DIST_DIR/ps5-image-forge-linux"
    echo "Run 'bash scripts/build.sh' first."
    exit 1
fi

# Create desktop file
DESKTOP_DIR="$HOME/.local/share/applications"
mkdir -p "$DESKTOP_DIR"

cat > "$DESKTOP_DIR/ps5-image-forge-linux.desktop" <<EOF
[Desktop Entry]
Name=PS5 Image Forge Linux
Comment=Create PS5 game dump images (FFPKG, EXFAT, FFPFSC)
Exec=${DIST_DIR}/ps5-image-forge-linux
Icon=ps5-image-forge-linux
Terminal=false
Type=Application
Categories=Utility;
StartupWMClass=ps5-image-forge-linux
EOF

echo "Installed desktop entry: $DESKTOP_DIR/ps5-image-forge-linux.desktop"

# Install icon
ICON_DIR="$HOME/.local/share/icons/hicolor/256x256/apps"
mkdir -p "$ICON_DIR"

# Convert SVG to PNG for desktop icon (using rsvg-convert or inkscape)
if command -v rsvg-convert &>/dev/null; then
    rsvg-convert -w 256 -h 256 "$PROJECT_DIR/ps5_image_forge_linux/resources/icon.svg" > "$ICON_DIR/ps5-image-forge-linux.png"
elif command -v inkscape &>/dev/null; then
    inkscape "$PROJECT_DIR/ps5_image_forge_linux/resources/icon.svg" --export-type=png --export-filename="$ICON_DIR/ps5-image-forge-linux.png" --export-width=256 --export-height=256
else
    # Fallback: copy SVG (some desktop environments support SVG icons)
    cp "$PROJECT_DIR/ps5_image_forge_linux/resources/icon.svg" "$ICON_DIR/ps5-image-forge-linux.svg"
    echo "Note: Installed SVG icon (rsvg-convert or inkscape not found for PNG conversion)"
fi

echo "Installed icon to: $ICON_DIR/"

# Refresh desktop database
update-desktop-database "$DESKTOP_DIR" 2>/dev/null || true

echo ""
echo "Done! 'PS5 Image Forge Linux' should now appear in your application launcher."
