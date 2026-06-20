"""UFS2Tool engine for FFPKG format creation."""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import threading
from pathlib import Path
from typing import Callable


def locate_ufs2tool() -> str | None:
    """Find UFS2Tool binary.

    Search order:
    1. Next to the application executable (dist/ folder)
    2. In UFS2Tool/linux-x64-selfcontained/ next to executable
    3. In _MEIPASS (PyInstaller extraction dir)
    4. In PATH

    Returns:
        Path to UFS2Tool binary, or None if not found.
    """
    import sys

    # Determine base directory
    if getattr(sys, "frozen", False):
        # PyInstaller: executable lives in dist/, resources in _internal/
        exe_path = Path(getattr(sys, "_MEIPASS", "")).parent  # dist/ps5-image-builder/
        meipass = Path(getattr(sys, "_MEIPASS", ""))
    else:
        # Dev mode: next to source
        exe_path = Path(__file__).resolve().parent.parent
        meipass = None

    candidates = [
        exe_path / "UFS2Tool",
        exe_path / "UFS2Tool" / "linux-x64-selfcontained" / "UFS2Tool",
    ]
    if meipass:
        candidates.extend([
            meipass / "UFS2Tool",
            meipass / "UFS2Tool" / "linux-x64-selfcontained" / "UFS2Tool",
        ])

    for candidate in candidates:
        if candidate.is_file():
            return str(candidate)

    # Check in PATH
    ufs2tool = shutil.which("UFS2Tool")
    if ufs2tool:
        return ufs2tool

    return None


def build_ffpkg(
    source_dir: Path,
    output_path: Path,
    log_callback: Callable[[str], None] | None = None,
    progress_callback: Callable[[int], None] | None = None,
    cancel_event: threading.Event | None = None,
    proc_callback: Callable[[subprocess.Popen], None] | None = None,
) -> Path:
    """Build an FFPKG image from a source directory using UFS2Tool.

    Args:
        source_dir: Path to the game dump folder.
        output_path: Desired output path (.ffpkg).
        log_callback: Optional callback for log messages.
        progress_callback: Optional callback for progress (0-100).
        cancel_event: If set, the build aborts with CancelledBuild.
        proc_callback: Called with the Popen object so the caller can track/kill it.

    Returns:
        Path to the created FFPKG file.

    Raises:
        FileNotFoundError: If UFS2Tool is not found.
        subprocess.CalledProcessError: If UFS2Tool fails.
    """
    import re

    tool_path = locate_ufs2tool()
    if tool_path is None:
        raise FileNotFoundError(
            "UFS2Tool not found. Please place it next to the application "
            "or add it to your PATH.\n"
            "Download: https://github.com/SvenGDK/UFS2Tool/releases"
        )

    if log_callback:
        log_callback(f"[INFO] UFS2Tool found at: {tool_path}")

    # Create temp file in the output directory
    output_path = output_path.resolve()
    output_path.parent.mkdir(parents=True, exist_ok=True)

    fd, temp_path_str = tempfile.mkstemp(
        dir=str(output_path.parent),
        prefix=f"{output_path.stem}_",
        suffix=".tmp",
    )
    os.close(fd)
    temp_path = Path(temp_path_str)

    # Regex to extract percentage from UFS2Tool output
    pct_re = re.compile(r'(\d+)%')

    try:
        cmd = [
            tool_path,
            "newfs",
            "-O", "2",
            "-b", "32768",
            "-f", "4096",
            "-D", str(source_dir),
            str(temp_path),
        ]

        if log_callback:
            log_callback(f"[INFO] Creating UFS2 image: {' '.join(cmd)}")
            log_callback("[INFO] This may take a while for large games...")

        # Run UFS2Tool, streaming output line by line
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,  # line-buffered
        )

        # Let the caller track this process for cancellation
        if proc_callback:
            proc_callback(proc)

        # Import here to avoid circular import at module level
        from ps5_image_forge_linux.builder import CancelledBuild

        last_pct = 0
        for line in proc.stdout:
            # Check for cancellation after each line
            if cancel_event and cancel_event.is_set():
                proc.terminate()
                proc.wait()
                raise CancelledBuild()

            line = line.rstrip("\n\r")
            if log_callback:
                log_callback(f"[UFS2Tool] {line}")
            # Only track progress from the "adding files to image" phase
            # (ignore "writing cylinder groups" which finishes instantly)
            if "adding files to image" in line.lower() and progress_callback:
                match = pct_re.search(line)
                if match:
                    pct = int(match.group(1))
                    if pct > last_pct:
                        last_pct = pct
                        progress_callback(min(pct, 95))  # cap at 95, builder sets 100 on done

        proc.wait()

        # Final cancel check after process completes
        if cancel_event and cancel_event.is_set():
            raise CancelledBuild()

        if proc.returncode != 0:
            raise subprocess.CalledProcessError(
                proc.returncode, cmd
            )

        # Rename temp to final
        if output_path.exists():
            output_path.unlink()
        temp_path.rename(output_path)

        if log_callback:
            log_callback(f"[INFO] FFPKG created: {output_path}")

        return output_path

    except Exception:
        if temp_path.exists():
            temp_path.unlink()
        raise
