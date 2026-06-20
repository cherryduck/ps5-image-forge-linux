"""Build orchestration - dispatches to the correct engine per format."""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import Callable

from ps5_image_forge_linux.ufs2_engine import build_ffpkg


class CancelledBuild(Exception):
    """Raised when the user cancels a build operation."""


class _no_op_context:
    """No-op context manager."""
    def __enter__(self): return None
    def __exit__(self, *a): pass


class ProgressWatcher:
    """Monitor an output file's size in a background thread and report progress.

    Uses a two-phase approach:
    1. **Size-based**: tracks file growth, reports progress proportional to bytes written.
    2. **Time-based fallback**: when file growth stalls (likely due to compression/buffering),
       ramps up linearly over time so progress actually reaches end_pct before the build finishes.

    Use as a context manager around a long-running build step::

        with ProgressWatcher(output_path, progress_callback, start_pct=5, end_pct=95) as w:
            build_pfs_stream_single_file(...)  # blocks
        progress_callback(100)  # done
    """

    def __init__(
        self,
        output_path: Path,
        callback: Callable[[int], None],
        start_pct: int = 5,
        end_pct: int = 95,
        interval: float = 0.5,
        stall_threshold: float = 3.0,
        cancel_event: threading.Event | None = None,
    ) -> None:
        self.output_path = output_path
        self.callback = callback
        self.start_pct = start_pct
        self.end_pct = end_pct
        self.interval = interval
        self.stall_threshold = stall_threshold  # seconds of no growth before fallback kicks in
        self.cancel_event = cancel_event
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()

    def __enter__(self) -> "ProgressWatcher":
        self._stop.clear()
        self._thread = threading.Thread(target=self._watch, daemon=True)
        self._thread.start()
        return self

    def __exit__(self, *args) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=2)

    def _is_cancelled(self) -> bool:
        return self.cancel_event is not None and self.cancel_event.is_set()

    def _watch(self) -> None:
        # Wait for the file to appear and get initial size
        baseline = 0
        while not self._stop.is_set() and not self._is_cancelled():
            size = self._file_size()
            if size > 0:
                baseline = size
                break
            time.sleep(self.interval)

        last_size = baseline
        last_reported = self.start_pct
        last_growth_time = time.monotonic()

        while not self._stop.is_set() and not self._is_cancelled():
            time.sleep(self.interval)

            new_size = self._file_size()
            now = time.monotonic()

            if new_size > last_size:
                # File is growing — size-based progress
                last_growth_time = now
                growth = new_size - baseline
                # Estimate final growth as current growth * 3 (conservative for compressed output)
                estimated_final = baseline + max(growth * 3, baseline * 2)
                ratio = min(growth / max(estimated_final - baseline, 1), 1.0)
                pct = int(self.start_pct + ratio * (self.end_pct - self.start_pct))
                pct = min(pct, self.end_pct)
                if pct > last_reported:
                    self.callback(pct)
                    last_reported = pct
                last_size = new_size
            else:
                # File stalled — time-based fallback ramp
                stalled_for = now - last_growth_time
                if stalled_for > self.stall_threshold:
                    # Assume build will finish in ~30s after stall starts
                    ramp_duration = 30.0
                    ramp_elapsed = stalled_for - self.stall_threshold
                    ramp_ratio = min(ramp_elapsed / ramp_duration, 1.0)
                    pct = int(last_reported + ramp_ratio * (self.end_pct - last_reported))
                    pct = min(pct, self.end_pct)
                    if pct > last_reported:
                        self.callback(pct)
                        last_reported = pct

    def _file_size(self) -> int:
        try:
            return self.output_path.stat().st_size
        except (OSError, FileNotFoundError):
            return 0


# Output format enum
class OutputFormat:
    FFPKG = "ffpkg"
    EXFAT = "exfat"
    FFPFSC = "ffpfsc"


def _get_resource_path(name: str) -> Path | None:
    """Find a resource file in the app's directory."""
    import sys as _sys
    if getattr(_sys, "frozen", False):
        base = Path(getattr(_sys, "_MEIPASS", ""))
    else:
        base = Path(__file__).resolve().parent
    # Check resources/ first, then app root
    for subdir in ("resources", ""):
        p = base / "ps5_image_forge_linux" / subdir / name
        if p.is_file():
            return p
        p = base / subdir / name
        if p.is_file():
            return p
    return None


def _extract_title_id(source: Path) -> str | None:
    """Extract title ID from source path or param.json."""
    # Try param.json first
    param_path = source / "param.json"
    if param_path.is_file():
        try:
            with param_path.open("r", encoding="utf-8") as f:
                data = json.load(f)
            title_id = data.get("titleId")
            if title_id:
                return str(title_id)
        except (json.JSONDecodeError, OSError):
            pass

    # Try path-based extraction (CUSA/PPSA/etc pattern)
    import re
    path_str = str(source)
    match = re.search(r'(CUSA|PPSA|PCAS|PCJS|PCES|NPXS)\d{5}', path_str, re.IGNORECASE)
    if match:
        return match.group(0).upper()

    return None


def _auto_game_root(source: Path) -> Path:
    """Auto-detect game root by finding eboot.bin."""
    # Check if current dir has eboot.bin
    for item in source.iterdir():
        if item.is_file() and item.name.lower() == "eboot.bin":
            return source

    # Search subdirectories
    for item in source.iterdir():
        if item.is_dir():
            result = _auto_game_root(item)
            if result:
                return result

    return source  # Fallback to original


def _generate_output_name(source: Path, output_dir: Path, fmt: str) -> Path:
    """Generate output filename based on source and format."""
    title_id = _extract_title_id(source)
    if title_id:
        stem = title_id
    else:
        stem = source.name

    return output_dir / f"{stem}.{fmt}"


# --- Size-based time estimation helpers ---

def _input_size(source: Path, is_file: bool) -> int:
    """Return total input size in bytes for a file or folder."""
    if is_file:
        return source.stat().st_size
    total = 0
    for p in source.rglob("*"):
        if p.is_file():
            total += p.stat().st_size
    return total


def _estimated_compress_time(input_bytes: int) -> float:
    """Estimate PFS compression time in seconds.

    Based on ~2 minutes per 100 GB of input data.
    Minimum 10 s (small inputs), maximum 7200 s (2 h safety cap).
    """
    gb = input_bytes / (1024 ** 3)
    estimated = (gb / 100) * 120  # 2 min per 100 GB
    return max(10.0, min(estimated, 7200.0))


def _format_duration(seconds: float) -> str:
    """Format seconds into a human-readable duration string."""
    if seconds < 60:
        return f"{seconds:.0f}s"
    m = int(seconds // 60)
    s = int(seconds % 60)
    if m < 60:
        return f"{m}m {s}s"
    h = m // 60
    m = m % 60
    return f"{h}h {m:02d}m"


def _time_based_progress(
    callback: Callable[[int], None],
    start_pct: int,
    end_pct: int,
    duration: float,
    done_event: threading.Event,
    cancel_event: threading.Event | None = None,
    interval: float = 0.5,
) -> None:
    """Background thread: ramp progress linearly from start_pct to end_pct over *duration* seconds."""
    start = time.monotonic()
    last_pct = start_pct
    while not done_event.is_set():
        if cancel_event and cancel_event.is_set():
            return
        elapsed = time.monotonic() - start
        ratio = min(elapsed / duration, 1.0)
        pct = int(start_pct + ratio * (end_pct - start_pct))
        pct = min(pct, end_pct)
        if pct > last_pct:
            callback(pct)
            last_pct = pct
        if ratio >= 1.0:
            break
        time.sleep(interval)


def build(
    source: Path,
    output_dir: Path,
    output_format: str,
    is_file_input: bool = False,
    log_callback: Callable[[str], None] | None = None,
    progress_callback: Callable[[int], None] | None = None,
    cancel_event: threading.Event | None = None,
    proc_callback: Callable[[subprocess.Popen], None] | None = None,
) -> Path:
    """Build an image in the specified format.

    Args:
        source: Source folder path or file path (for file input).
        output_dir: Output directory.
        output_format: One of OutputFormat constants.
        is_file_input: True if source is a file (.exfat/.ffpkg).
        log_callback: Optional callback for log messages.
        progress_callback: Optional callback for progress (0-100).
        cancel_event: If set, the build aborts with CancelledBuild.
        proc_callback: Called with subprocess Popen objects for tracking.

    Returns:
        Path to the created output file.

    Raises:
        CancelledBuild: If the user cancels the build.
        RuntimeError: If the build fails.
    """
    output_dir.mkdir(parents=True, exist_ok=True)

    # Import lazy_mkpfs functions
    try:
        from lazy_mkpfs.build import build_pfs_stream_single_file
        from lazy_mkpfs.create_exfat import create_exfat_image
        from lazy_mkpfs.pack_folder import find_game_root, extract_title_id
        from lazy_mkpfs.compression import set_zlib_backend
    except ImportError as e:
        raise RuntimeError(f"Failed to import lazy_mkpfs: {e}")

    if is_file_input:
        # File input -> FFPFSC only
        if output_format != OutputFormat.FFPFSC:
            raise ValueError(f"File input only supports FFPFSC format, got {output_format}")

        return _build_file_to_ffpfsc(
            source=source,
            output_dir=output_dir,
            log_callback=log_callback,
            progress_callback=progress_callback,
            cancel_event=cancel_event,
        )

    # Folder input - auto-detect game root
    game_root = source
    if find_game_root is not None:
        detected = find_game_root(source)
        if detected:
            game_root = detected
            if log_callback and detected != source:
                log_callback(f"[INFO] Auto-detected game root: {detected.name}")

    if output_format == OutputFormat.FFPKG:
        return _build_folder_to_ffpkg(
            source=game_root,
            output_dir=output_dir,
            log_callback=log_callback,
            progress_callback=progress_callback,
            cancel_event=cancel_event,
            proc_callback=proc_callback,
        )
    elif output_format == OutputFormat.EXFAT:
        return _build_folder_to_exfat(
            source=game_root,
            output_dir=output_dir,
            log_callback=log_callback,
            progress_callback=progress_callback,
            cancel_event=cancel_event,
        )
    elif output_format == OutputFormat.FFPFSC:
        return _build_folder_to_ffpfsc(
            source=game_root,
            output_dir=output_dir,
            log_callback=log_callback,
            progress_callback=progress_callback,
            cancel_event=cancel_event,
        )

    raise ValueError(f"Unknown output format: {output_format}")


def _build_folder_to_ffpkg(
    source: Path,
    output_dir: Path,
    log_callback: Callable[[str], None] | None = None,
    progress_callback: Callable[[int], None] | None = None,
    cancel_event: threading.Event | None = None,
    proc_callback: Callable[[subprocess.Popen], None] | None = None,
) -> Path:
    """Build FFPKG from folder."""
    output_path = _generate_output_name(source, output_dir, "ffpkg")
    if log_callback:
        log_callback(f"[INFO] Building FFPKG: {output_path}")

    try:
        with ProgressWatcher(output_path, progress_callback, start_pct=0, end_pct=95, cancel_event=cancel_event) if progress_callback else _no_op_context():
            result = build_ffpkg(source, output_path, log_callback=log_callback, progress_callback=progress_callback, cancel_event=cancel_event, proc_callback=proc_callback)

        if cancel_event and cancel_event.is_set():
            raise CancelledBuild()

        if progress_callback:
            progress_callback(100)

        if log_callback:
            log_callback(f"[SUCCESS] FFPKG created: {result}")

        return result

    except CancelledBuild:
        raise
    except Exception as e:
        if log_callback:
            log_callback(f"[ERROR] FFPKG build failed: {e}")
        raise


def _build_folder_to_exfat(
    source: Path,
    output_dir: Path,
    log_callback: Callable[[str], None] | None = None,
    progress_callback: Callable[[int], None] | None = None,
    cancel_event: threading.Event | None = None,
) -> Path:
    """Build EXFAT image from folder using a single pkexec call."""
    output_path = _generate_output_name(source, output_dir, "exfat")
    if log_callback:
        log_callback(f"[INFO] Building EXFAT image: {output_path}")

    try:
        result = _create_exfat_helper(source, output_path, log_callback=log_callback, progress_callback=progress_callback, cancel_event=cancel_event)

        if cancel_event and cancel_event.is_set():
            raise CancelledBuild()

        if progress_callback:
            progress_callback(100)

        if log_callback:
            log_callback(f"[SUCCESS] EXFAT image created: {result}")

        return result

    except CancelledBuild:
        raise
    except Exception as e:
        if log_callback:
            log_callback(f"[ERROR] EXFAT build failed: {e}")
        raise


def _create_exfat_helper(
    source: Path,
    output_path: Path,
    log_callback: Callable[[str], None] | None = None,
    progress_callback: Callable[[int], None] | None = None,
    cancel_event: threading.Event | None = None,
) -> Path:
    """Create exFAT image using a helper script for single-elevation.

    Uses a shell script run via pkexec so all privileged operations
    (losetup, mount, umount, losetup -d) happen in one auth prompt.
    """
    import os
    import shutil
    import subprocess
    import tempfile

    output_path = output_path.resolve()
    output_path.parent.mkdir(parents=True, exist_ok=True)

    # Find the helper script (must be absolute path for pkexec)
    helper_script = _get_resource_path("exfat_helper.sh")
    if not helper_script or not helper_script.is_file():
        raise FileNotFoundError("exfat_helper.sh not found. The app may be corrupted.")
    helper_script = helper_script.resolve()

    # Progress file for helper script to write to.
    # Place it in the output directory (not /tmp) because pkexec may run in a
    # different mount namespace where /tmp is inaccessible regardless of perms.
    progress_file = None
    if progress_callback:
        progress_file = tempfile.mktemp(
            prefix=".exfat_progress_", suffix=".pct",
            dir=str(output_path.parent),
        )

    try:
        # 1. Calculate image size (total size of source + 15% overhead, min 1GB)
        total_size = 0
        for dirpath, _, filenames in os.walk(source):
            for f in filenames:
                fp = Path(dirpath) / f
                if fp.is_file():
                    total_size += fp.stat().st_size

        # exFAT needs overhead; round up to nearest 1MB, min 1GB
        image_size = max(total_size * 1.15 + 100 * 1024 * 1024, 1 * 1024 * 1024 * 1024)
        image_size_mb = int(image_size / (1024 * 1024))

        if log_callback:
            log_callback(f"[INFO] Creating {image_size_mb}MB exFAT image ({total_size / (1024**3):.1f}GB source)...")

        # 2. Create empty image file in output directory
        fd, tmp_image = tempfile.mkstemp(
            dir=str(output_path.parent),
            prefix=".ps5_exfat_",
            suffix=".tmp",
        )
        os.close(fd)
        tmp_path = Path(tmp_image)

        try:
            # Create sparse image with truncate (works on all filesystems)
            image_size_bytes = image_size_mb * 1024 * 1024
            if log_callback:
                log_callback(f"[INFO] Creating {image_size_mb}MB exFAT image (sparse)...")

            subprocess.run(
                ["truncate", "-s", str(image_size_bytes), str(tmp_path)],
                check=True,
                capture_output=True,
                text=True,
            )

            if log_callback:
                log_callback("[INFO] Formatting exFAT...")

            # 3. Format with mkfs.exfat
            mkfs = shutil.which("mkfs.exfat") or shutil.which("mkfs.exfat-fs")
            if not mkfs:
                raise RuntimeError(
                    "mkfs.exfat not found. Install exfatprogs: "
                    "sudo pacman -S exfatprogs"
                )

            result = subprocess.run(
                [mkfs, "-f", str(tmp_path)],
                capture_output=True,
                text=True,
            )
            if result.returncode != 0:
                err = result.stderr or result.stdout or "unknown error"
                # errno 28 = No space left on device (sparse file issue)
                # Try fallocate to pre-allocate real blocks
                if "28" in err or "No space" in err:
                    if log_callback:
                        log_callback("[WARN] Sparse file failed, trying fallocate...")
                    subprocess.run(
                        ["fallocate", "-l", str(image_size_bytes), str(tmp_path)],
                        check=True,
                        capture_output=True,
                        text=True,
                    )
                    result = subprocess.run(
                        [mkfs, "-f", str(tmp_path)],
                        capture_output=True,
                        text=True,
                    )
                if result.returncode != 0:
                    raise RuntimeError(f"mkfs.exfat failed: {result.stderr or result.stdout}")

            # 4. Create mount point
            mount_point = tempfile.mkdtemp(prefix="exfat_mount_")

            # 5. Run helper script via pkexec (single auth prompt)
            if log_callback:
                log_callback("[INFO] Mounting and copying files (password prompt may appear)...")

            uid = os.getuid()
            gid = os.getgid()

            cmd = [
                "pkexec",
                "bash",
                str(helper_script),
                str(tmp_path),
                str(source),
                mount_point,
                str(uid),
                str(gid),
            ]
            if progress_file:
                cmd.append(str(progress_file))

            # Stream stdout/stderr and watch progress file
            proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
            )

            # Watch progress file in background thread
            progress_thread = None
            if progress_file and progress_callback:
                progress_thread = threading.Thread(
                    target=_watch_exfat_progress,
                    args=(Path(progress_file), progress_callback, cancel_event),
                    daemon=True,
                )
                progress_thread.start()

            # Stream output lines
            for line in proc.stdout:
                line = line.rstrip("\n\r")
                if log_callback and line:
                    log_callback(f"[exfat] {line}")

            proc.wait()

            # Stop progress watching
            if progress_thread:
                progress_thread.join(timeout=2)

            # Check for cancellation after subprocess completes
            if cancel_event and cancel_event.is_set():
                raise CancelledBuild()

            # Clean up mount point dir
            shutil.rmtree(mount_point, ignore_errors=True)

            if proc.returncode != 0:
                raise RuntimeError(f"exFAT build failed (exit code {proc.returncode})")

            # 6. Rename to final output
            if output_path.exists():
                output_path.unlink()
            tmp_path.rename(output_path)

            return output_path

        except Exception:
            tmp_path.unlink(missing_ok=True)
            mount_point = locals().get("mount_point")
            if mount_point:
                import shutil as _sh
                _sh.rmtree(mount_point, ignore_errors=True)
            raise

    finally:
        if progress_file:
            Path(progress_file).unlink(missing_ok=True)


def _watch_exfat_progress(
    progress_file: Path,
    callback: Callable[[int], None],
    cancel_event: threading.Event | None = None,
) -> None:
    """Watch the exFAT progress file and report progress."""
    last_pct = 0
    while not (cancel_event and cancel_event.is_set()):
        try:
            if progress_file.exists():
                with progress_file.open("r") as f:
                    content = f.read().strip()
                    if content:
                        pct = int(content)
                        if pct > last_pct:
                            callback(min(pct, 95))
                            last_pct = pct
        except (ValueError, OSError):
            pass
        time.sleep(0.5)


def _build_folder_to_ffpfsc(
    source: Path,
    output_dir: Path,
    log_callback: Callable[[str], None] | None = None,
    progress_callback: Callable[[int], None] | None = None,
    cancel_event: threading.Event | None = None,
) -> Path:
    """Build FFPFSC from folder (EXFAT wrapper + compressed PFS).

    Uses our own EXFAT helper for single-elevation, then wraps with PFS.
    """
    output_path = _generate_output_name(source, output_dir, "ffpfsc")
    if log_callback:
        log_callback(f"[INFO] Building FFPFSC: {output_path}")

    # Calculate input size upfront for time estimation
    total_size = _input_size(source, is_file=False)

    exfat_path: Path | None = None
    try:
        # Step 1: Create EXFAT image using our helper (single pkexec)
        import tempfile as _tf
        exfat_tmp = _tf.NamedTemporaryFile(
            dir=str(output_dir), prefix=".", suffix=".exfat", delete=False
        )
        exfat_tmp.close()
        exfat_path = Path(exfat_tmp.name)

        if log_callback:
            log_callback("[INFO] Step 1/2: Creating exFAT image...")

        # Scale inner exFAT progress (0-100%) to 0-50% of total build
        _exfat_cb = None
        if progress_callback:
            def _scale_exfat(pct: int) -> None:
                progress_callback(min(pct // 2, 50))
            _exfat_cb = _scale_exfat

        _create_exfat_helper(source, exfat_path, log_callback=log_callback, progress_callback=_exfat_cb, cancel_event=cancel_event)

        if cancel_event and cancel_event.is_set():
            raise CancelledBuild()

        if progress_callback:
            progress_callback(50)

        # Step 2: Wrap EXFAT in compressed PFS
        if log_callback:
            log_callback("[INFO] Step 2/2: Compressing to PFS...")

        from lazy_mkpfs.build import build_pfs_stream_single_file

        # Estimate compression time based on input size (~2 min per 100 GB)
        compress_time = _estimated_compress_time(total_size)
        if log_callback:
            log_callback(f"[INFO] Estimated compression time: ~{_format_duration(compress_time)}")

        done_event = threading.Event()
        if progress_callback:
            anim_thread = threading.Thread(
                target=_time_based_progress,
                args=(progress_callback, 50, 95, compress_time, done_event, cancel_event),
                daemon=True,
            )
            anim_thread.start()

        stats = build_pfs_stream_single_file(
            source_file=exfat_path,
            output_path=output_path,
            block_size=0x10000,
            pfs_version=2,
            case_insensitive=True,
            zlib_level=6,
            threshold_gain=1,
            min_file_gain=0,
            min_compress_size_mb=0.0,
            cpu_count=0,
            compress=True,
            encrypted=False,
            skip_executable_compression=False,
            dry_run=False,
            verbose=bool(log_callback),
            use_ram_if_possible=True,
            zlib_backend="zlib",
        )

        done_event.set()

        # Check for cancellation after compression
        if cancel_event and cancel_event.is_set():
            raise CancelledBuild()

        # Clean up temp EXFAT
        exfat_path.unlink(missing_ok=True)
        exfat_path = None  # Prevent double-cleanup in except block

        if progress_callback:
            progress_callback(100)

        if log_callback:
            log_callback(f"[SUCCESS] FFPFSC created: {output_path}")
            log_callback(f"  Files: {stats.total_files}")
            log_callback(f"  Uncompressed: {stats.uncompressed_total_size / (1024**3):.1f} GB")
            log_callback(f"  Stored: {stats.stored_total_size / (1024**3):.1f} GB")
            log_callback(f"  Gain: {stats.actual_gain_pct:.1f}%")
            log_callback(f"  Time: {stats.elapsed_seconds:.1f}s")

        return output_path

    except CancelledBuild:
        raise
    except Exception:
        if log_callback:
            log_callback(f"[ERROR] FFPFSC build failed: {sys.exc_info()[1]}")
        raise
    finally:
        # Clean up temp EXFAT on any exit
        if exfat_path and exfat_path.exists():
            exfat_path.unlink(missing_ok=True)
        # Clean up partial output on cancel/error
        if cancel_event and cancel_event.is_set() and output_path.exists():
            output_path.unlink(missing_ok=True)


def _build_file_to_ffpfsc(
    source: Path,
    output_dir: Path,
    log_callback: Callable[[str], None] | None = None,
    progress_callback: Callable[[int], None] | None = None,
    cancel_event: threading.Event | None = None,
) -> Path:
    """Build FFPFSC from an existing image file."""
    stem = source.stem
    output_path = output_dir / f"{stem}.ffpfsc"
    if log_callback:
        log_callback(f"[INFO] Building FFPFSC from file: {output_path}")

    # Get file size for time estimation
    file_size = _input_size(source, is_file=True)

    if progress_callback:
        progress_callback(5)

    try:
        from lazy_mkpfs.build import build_pfs_stream_single_file

        # Estimate compression time based on file size (~2 min per 100 GB)
        compress_time = _estimated_compress_time(file_size)
        if log_callback:
            log_callback(f"[INFO] Estimated compression time: ~{_format_duration(compress_time)}")

        done_event = threading.Event()
        if progress_callback:
            anim_thread = threading.Thread(
                target=_time_based_progress,
                args=(progress_callback, 5, 95, compress_time, done_event, cancel_event),
                daemon=True,
            )
            anim_thread.start()

        stats = build_pfs_stream_single_file(
            source_file=source,
            output_path=output_path,
            block_size=0x10000,
            pfs_version=2,
            case_insensitive=True,
            zlib_level=6,
            threshold_gain=1,
            min_file_gain=0,
            min_compress_size_mb=0.0,
            cpu_count=0,
            compress=True,
            encrypted=False,
            skip_executable_compression=False,
            dry_run=False,
            verbose=bool(log_callback),
            use_ram_if_possible=True,
            zlib_backend="zlib",
        )

        done_event.set()

        # Check for cancellation after compression
        if cancel_event and cancel_event.is_set():
            raise CancelledBuild()

        if progress_callback:
            progress_callback(100)

        if log_callback:
            log_callback(f"[SUCCESS] FFPFSC created: {output_path}")
            log_callback(f"  Input: {source.name}")
            log_callback(f"  Uncompressed: {stats.uncompressed_total_size / (1024**3):.1f} GB")
            log_callback(f"  Stored: {stats.stored_total_size / (1024**3):.1f} GB")
            log_callback(f"  Gain: {stats.actual_gain_pct:.1f}%")
            log_callback(f"  Time: {stats.elapsed_seconds:.1f}s")

        return output_path

    except CancelledBuild:
        raise
    except Exception as e:
        if log_callback:
            log_callback(f"[ERROR] FFPFSC build failed: {e}")
        raise
    finally:
        # Clean up partial output on cancel
        if cancel_event and cancel_event.is_set() and output_path.exists():
            output_path.unlink(missing_ok=True)
