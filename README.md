# PS5 Image Forge Linux

A self-contained Linux GUI application for creating PS5 game dump images in multiple formats: FFPKG (UFS2), EXFAT, and FFPFSC.

## Features

- **FFPKG**: UFS2 filesystem image via UFS2Tool
- **EXFAT**: Raw EXFAT filesystem image
- **FFPFSC**: Compressed PFS with EXFAT wrapper, or compressed from existing image file
- Auto-detect game root via `eboot.bin`
- Title ID extraction from `param.json`
- Parallel compression with Intel ISA-L / zlib-ng support
- Real-time progress and logging
- Size-based progress estimation for compression steps

## Requirements (standalone app)

- **`mkfs.exfat`** (for EXFAT format) — install via your package manager (`pacman -S exfatprogs`, `apt install exfatprogs`, etc.)
- **sudo access** (required for EXFAT wrapper formats: FFPFSC from folder, since it needs loop device access)
- **UFS2Tool** (for FFPKG format):
  1. Download `linux-x64-selfcontained.zip` from [SvenGDK/UFS2Tool releases](https://github.com/SvenGDK/UFS2Tool/releases)
  2. Extract into `UFS2Tool/` next to the app binary
  3. Final structure: `.../UFS2Tool/linux-x64-selfcontained/UFS2Tool` (plus supporting files)

> Python, PyQt6, and all other dependencies are bundled in the standalone build — no separate installation needed.

## Development

```bash
# Clone with submodules
git clone --recursive https://github.com/<user>/ps5-image-forge-linux.git
cd ps5-image-forge-linux

# Create virtual environment
python3 -m venv .venv
source .venv/bin/activate          # bash
# source .venv/bin/activate.fish   # fish

# Install dependencies
pip install -e .

# Run (dev mode)
python -m ps5_image_forge_linux
```

## Building standalone app

```bash
# Build with PyInstaller (produces dist/ps5-image-forge-linux/)
bash scripts/build.sh

# Install desktop entry + icon (appears in app launcher)
bash scripts/install-desktop.sh
```

The standalone app is self-contained — no Python or venv needed at runtime.

## Acknowledgements

This project builds on the work of:

- **[Lazy_MkPFS](https://github.com/Nazky/Lazy_MkPFS)** by **Nazky** — PFS compression engine, EXFAT creation, and folder packaging logic. Used as the core compression and filesystem backend.
- **[PSFFPKG](https://github.com/sinajet/PSFFPKG/)** by **sinajet** — UFS2Tool and FFPKG packaging tools. Used for building UFS2 filesystem images.

Both projects are used as submodules and integrated into this Linux GUI wrapper.

## License

GPL-3.0
